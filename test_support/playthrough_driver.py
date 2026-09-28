"""Record real public-API play, without authored state or narrative injection."""
import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--base', default='http://127.0.0.1:8000/api/v1')
    parser.add_argument('--start', type=Path, help='开局请求 JSON；仅在没有历史时允许')
    parser.add_argument('--action', help='当前玩家自由输入')
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(parents=True, exist_ok=True)
    index_path = directory / 'playthrough.json'
    record = json.loads(index_path.read_text()) if index_path.exists() else dict(
        schemaVersion='public-api-playthrough/1', status='not_started', operations=[],
        acceptance='pending', userVisibleFailures=0, replayIsLiveGeneration=False)
    if record['status'] == 'failed':
        raise SystemExit('此轮记录已失败，保留原始结果；修复后请从新存档重新验收。')
    if args.start:
        if record['operations']:
            raise SystemExit('不得覆盖已开始的游玩记录')
        payload = json.loads(args.start.read_text())
        path = '/sessions/stream'
    elif args.action:
        if record['status'] != 'active' or len(record['operations']) > 60:
            raise SystemExit('仅允许继续进行中的路线，单次检查最多 60 回合')
        payload = dict(parent_branch_id=record['branchId'], text=args.action)
        path = f'/sessions/{record["sessionId"]}/branches/stream'
    else:
        raise SystemExit('需要 --start 或 --action')
    payload['request_id'] = 'playthrough-' + uuid.uuid4().hex
    operation = dict(number=len(record['operations']), path=path, payload=payload, status='running')
    record['operations'].append(operation)
    save(index_path, record)
    started = time.monotonic()
    frames, result = [], None
    try:
        with httpx.Client(timeout=600) as client:
            with client.stream('POST', args.base + path, json=payload, headers={'Accept': 'text/event-stream'}) as response:
                operation['httpStatus'] = response.status_code
                response.raise_for_status()
                event, data = '', []
                for line in response.iter_lines():
                    if line.startswith('event:'):
                        event = line[6:].strip()
                    elif line.startswith('data:'):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        value = json.loads('\n'.join(data))
                        frames.append(dict(event=event, data=value))
                        save(directory / f'operation-{operation["number"]:03d}.json', frames)
                        if event == 'error':
                            raise RuntimeError(json.dumps(value, ensure_ascii=False))
                        if event == 'done':
                            result = value
                        event, data = '', []
            if result is None or not result.get('branch') or result.get('status') == 'rejected':
                raise RuntimeError('未收到完整已确认正文：' + str(result))
            node = result['branch']
            if not node.get('narrativeText') or node.get('fallbackMode'):
                raise RuntimeError('正文为空或来自降级候选')
            record.update(sessionId=node['sessionId'], branchId=node['id'], status='active')
            operation.update(status='written', branchId=node['id'], prose=node['narrativeText'])
            journey = client.get(args.base + f'/sessions/{record["sessionId"]}/journey', params={'branch_id': node['id']})
            journey.raise_for_status()
            save(directory / f'journey-{operation["number"]:03d}.json', journey.json())
            if journey.json()['status'] != 'active':
                record['status'] = 'ending_pending_review'
                closure = client.get(args.base + f'/sessions/{record["sessionId"]}/route-closure', params={'branch_id': node['id']})
                closure.raise_for_status()
                save(directory / 'ending.json', closure.json())
            save(directory / f'result-{operation["number"]:03d}.json', result)
            print(node['narrativeText'])
            print(json.dumps(dict(status=record['status'], branchId=node['id'],
                                  choices=node.get('readerChoices', node.get('openingActions', node.get('nextDirections')))), ensure_ascii=False))
    except Exception as error:
        record['status'] = 'failed'
        record['userVisibleFailures'] += 1
        operation.update(status='failed', errorType=type(error).__name__, error=str(error))
        print(str(error))
    finally:
        operation['elapsedMs'] = round((time.monotonic() - started) * 1000)
        save(index_path, record)
    raise SystemExit(1 if record['status'] == 'failed' else 0)


if __name__ == '__main__':
    main()
