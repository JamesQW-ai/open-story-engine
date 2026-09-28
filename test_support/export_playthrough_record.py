"""Read-only export of one actual saved ancestry; never certifies delivery."""
import argparse
import hashlib
import html
import json
from pathlib import Path
import sqlite3


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def read_record(database, session_id, branch_id=None):
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        session = connection.execute('SELECT * FROM game_sessions WHERE id=?', (session_id,)).fetchone()
        if session is None:
            raise ValueError('存档不存在')
        rows = list(connection.execute('SELECT * FROM branch_nodes WHERE session_id=? ORDER BY sequence', (session_id,)))
        by_id = {row['id']: row for row in rows}
        if not by_id:
            raise ValueError('存档没有正文')
        parents = {row['parent_id'] for row in rows}
        leaves = [row['id'] for row in rows if row['id'] not in parents]
        if branch_id is None:
            if len(leaves) != 1:
                raise ValueError('存在多条路线，必须明确指定 --branch-id')
            branch_id = leaves[0]
        if branch_id not in by_id:
            raise ValueError('分支不属于此存档')
        ancestry, seen = [], set()
        current = branch_id
        while current is not None:
            if current in seen or current not in by_id:
                raise ValueError('路线祖先缺失或成环')
            seen.add(current)
            row = by_id[current]
            ancestry.append(row)
            current = row['parent_id']
        ancestry.reverse()
        contract = json.loads(connection.execute('SELECT contract_json FROM session_story_contracts WHERE session_id=?',
                                                 (session_id,)).fetchone()[0])
        lifecycle = {row['branch_id']: json.loads(row['record_json']) for row in connection.execute(
            'SELECT branch_id,record_json FROM route_lifecycle WHERE session_id=?', (session_id,))}
        chapters, receipt = [], None
        for index, row in enumerate(ancestry):
            node = json.loads(row['node_json'])
            body = node.get('narrativeText')
            if not isinstance(body, str) or not body.strip() or node.get('fallbackMode'):
                raise ValueError('正文为空或为降级内容，不能导出为真实记录')
            if index and row['sequence'] <= ancestry[index - 1]['sequence']:
                raise ValueError('回合顺序不连续')
            action = node.get('playerDirection') or node.get('actionIntent', {}).get('input') or ''
            chapters.append(dict(number=index, branchId=row['id'], parentBranchId=row['parent_id'],
                                 action=action, text=body, narrativeSha256=digest(body),
                                 stateSha256=digest(json.dumps(node['branchState'], sort_keys=True, ensure_ascii=False)),
                                 createdAt=row['created_at']))
            receipt = lifecycle.get(row['id'], {}).get('receipt') or receipt
        ended = bool(receipt and receipt.get('ending_written') is True and receipt.get('ending_type') != 'early')
        failures = []
        draft_database = database.with_suffix('.turn-drafts.sqlite')
        if draft_database.exists():
            with sqlite3.connect(draft_database.resolve().as_uri() + '?mode=ro', uri=True) as drafts:
                for (body,) in drafts.execute('SELECT body FROM turn_drafts ORDER BY updated'):
                    draft = json.loads(body)
                    if (draft.get('binding', {}).get('session_id') == session_id and draft.get('status') == 'failed'
                            and draft.get('metrics', {}).get('selection_count', 0) > 0):
                        failures.append(dict(requestId=draft.get('request_id'), input=draft.get('payload'),
                                             error=draft.get('error'), metrics=draft.get('metrics')))
        return dict(schemaVersion='saved-player-record/1', sessionId=session_id, branchId=branch_id,
                    packageId=session['story_package_id'], packageVersion=session['story_package_version'],
                    identity=contract['persona']['name'], sourceDatabase=str(database.resolve()),
                    status='failed' if failures else 'ending_recorded_pending_acceptance' if ended else 'incomplete',
                    deliveryAcceptance='not_assessed', endingReceipt=receipt, chapters=chapters,
                    generationFailures=failures,
                    note='这是该存档同一路线的已保存原文，不含未提交草稿；导出状态仅描述数据，连续游玩及纠偏结果见配套审查记录。')


def render_html(record):
    escape = html.escape
    sections = []
    for chapter in record['chapters']:
        title = '开局' if chapter['number'] == 0 else f"第 {chapter['number']} 回合"
        action = f"<blockquote>你的行动：{escape(chapter['action'])}</blockquote>" if chapter['action'] else ''
        prose = ''.join(f'<p>{escape(p)}</p>' for p in chapter['text'].split('\n\n'))
        sections.append(f'<section><h2>{title}</h2>{action}{prose}</section>')
    status = {'incomplete': '旅程进行中', 'failed': '本次尝试出现生成失败，未通过交付验收',
              'ending_recorded_pending_acceptance': '结局已有记录，交付验收待确认'}[record['status']]
    failures = ''.join('<li>' + escape(str(item.get('input'))) + '<br>'
                       + escape(str(item.get('error'))) + '</li>' for item in record.get('generationFailures', []))
    failure_section = '<section><h2>生成失败记录</h2><ul>' + failures + '</ul></section>' if failures else ''
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>实际游玩记录</title><style>body{max-width:820px;margin:40px auto;padding:0 24px;font:18px/1.9 system-ui;background:#f7f3eb;color:#282720}'
            'h1,h2,section p{font-family:"Songti SC",serif}section{border-top:1px solid #c5baaa;margin-top:36px;padding-top:12px}'
            'blockquote{margin-left:0;border-left:3px solid #987b52;padding-left:18px}small{color:#655e55}</style>'
            f'<main><h1>实际游玩记录 · {escape(record["identity"])}</h1><p><strong>{status}</strong></p>'
            f'<p>{escape(record["note"])}</p><small>存档：{escape(record["sessionId"])}</small>' + ''.join(sections) + failure_section + '</main></html>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--branch-id')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    record = read_record(args.database, args.session_id, args.branch_id)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, content in [('player-record.json', json.dumps(record, ensure_ascii=False, indent=2) + '\n'),
                          ('player-record.html', render_html(record))]:
        with (args.output / name).open('x') as output:
            output.write(content)
    print(json.dumps(dict(status=record['status'], turns=len(record['chapters']) - 1,
                          deliveryAcceptance=record['deliveryAcceptance']), ensure_ascii=False))


if __name__ == '__main__':
    main()
