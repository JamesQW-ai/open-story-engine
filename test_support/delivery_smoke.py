"""Bounded real API delivery smoke; disposable storage, no semantic auto-pass."""
import argparse
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.content import load_runtime_story_package
from open_story_engine.llm import OpenAICompatibleGateway, LlmError
from open_story_engine.prompts import catalog_version
from test_support.longform import ROOT, longform_cases

ACTIONS = (
    '留在原地，向守门弟子说明老人让我来问山门的灯。只说出这句话，然后等待，不靠近伤者，不移动物品。',
    '仍留在原地，把手中的引荐文书收好，不交给别人。然后停下，不开始其他行动。',
    '留在原地，告诉守门弟子我还没有把引荐文书交出去。只说出这句话，然后等待。',
)


def run(output, max_calls=30, max_seconds=600, turns=3):
    if not 1 <= max_calls <= 60 or not 30 <= max_seconds <= 600 or not 1 <= turns <= 3:
        raise ValueError('调用上限 1..60、总时限 30..600 秒、回合数 1..3')
    # These actions bind to Lu Zhaolin's official opening, not a generic book.
    case = next(c for c in longform_cases() if c['package_id'] == 'taixu-relics-part1')
    package = load_runtime_story_package(case['path'], lazy=True)
    person = next(c for c in package['characters'] if c['id'] == 'character_ae4cb42b9b49')
    output.mkdir(parents=True, exist_ok=False)
    report = dict(schemaVersion='delivery-smoke/1', status='running', promptVersion=catalog_version(),
                  packageId=case['package_id'], packageVersion=case['version'], novelSha256=case['sha256'],
                  novelCjk=case['cjk'], maxCalls=max_calls, maxSeconds=max_seconds, requestedTurns=turns,
                  providerCalls=0, turns=[], calls=[], formalSessionWrites=0, semanticAcceptance=False,
                  sourceHashes={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
                      'test_support/delivery_smoke.py', 'open_story_engine/dynamic_memory.py',
                      'open_story_engine/action_review_context.py', 'open_story_engine/api_narrative.py',
                      'open_story_engine/api_play.py', 'open_story_engine/api_turn_drafts.py')})
    lock = threading.RLock()
    started = time.monotonic()

    def save():
        with lock:
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    request, complete = OpenAICompatibleGateway._request, OpenAICompatibleGateway._complete

    def bounded(gateway, body, timeout_seconds=None):
        with lock:
            remaining = max_seconds - (time.monotonic() - started)
            if report['providerCalls'] >= max_calls or remaining < 1:
                raise LlmError('交付检查达到固定调用或时间上限', 'model_call_limit')
            report['providerCalls'] += 1
            save()
        return request(gateway, body, timeout_seconds=min(timeout_seconds or gateway.timeout_seconds, remaining))

    def traced(gateway, messages, *args, **kwargs):
        entry = dict(messages=messages)
        began = time.monotonic()
        try:
            result = complete(gateway, messages, *args, **kwargs)
            entry.update(content=result.content, rawResponse=result.raw_response, observations=result.observations)
            return result
        except Exception as error:
            entry.update(errorType=type(error).__name__, code=getattr(error, 'code', None),
                         message=str(error), rawResponse=getattr(error, 'raw_response', None))
            raise
        finally:
            entry['elapsedMs'] = round((time.monotonic() - began) * 1000)
            with lock:
                report['calls'].append(entry)
                save()

    save()
    try:
        with TemporaryDirectory(prefix='story-delivery-') as tmp, \
                patch.dict(os.environ, {'STORY_PLANNER': 'openai', 'JEV_RUNTIME_REVIEW_MODE': 'off'}), \
                patch.object(OpenAICompatibleGateway, '_request', bounded), \
                patch.object(OpenAICompatibleGateway, '_complete', traced):
            read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
            play = PlayService(read, ROOT)
            try:
                play._require_planner()
                report['model'] = play._planner.gateway.model
                opening = play.create_session(case['package_id'], case['version'], person['defaultEntryPointId'],
                                              person['id'], identity_opening=True)
                (output / 'opening.json').write_text(json.dumps(opening, ensure_ascii=False, indent=2))
                sid, parent = opening['session']['id'], opening['branch']['id']
                report['openingKind'] = 'official_authored_not_model_generated'
                for number, action in enumerate(ACTIONS[:turns], 1):
                    with read.store() as store:
                        before = store.branches(sid)
                    row = dict(number=number, action=action, status='failed')
                    report['turns'].append(row)
                    began = time.monotonic()
                    try:
                        deltas = []
                        result = play.continue_turn(sid, parent, text=action, request_id=f'delivery-{number}', stream=deltas.append)
                        if result.get('status') != 'written':
                            raise AssertionError('未返回已入账正文')
                        (output / f'turn-{number}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
                        assert not result['branch'].get('fallbackMode'), '审核失败的降级正文不计为通过'
                        assert ''.join(deltas) == result['branch']['narrativeText'], '展示内容与已提交正文不同'
                        count = report['providerCalls']
                        replay = play.continue_turn(sid, parent, text=action, request_id=f'delivery-{number}')
                        assert replay['branch']['id'] == result['branch']['id'] and count == report['providerCalls']
                        with read.store() as store:
                            assert len(store.branches(sid)) == len(before) + 1
                        row.update(status='committed', idempotent=True, replayMatchesCommitted=True,
                                   branchId=result['branch']['id'], prose=result['branch']['narrativeText'])
                        parent = result['branch']['id']
                    except Exception as error:
                        row.update(errorType=type(error).__name__, code=getattr(error, 'code', None), message=str(error))
                        with read.store() as store:
                            row['failedTurnLeavesBranchesUnchanged'] = before == store.branches(sid)
                        row['failedTurnEmittedCharacters'] = sum(len(s) for s in deltas)
                        break
                    finally:
                        row['elapsedMs'] = round((time.monotonic() - began) * 1000)
                        save()
                        print(json.dumps({k: v for k, v in row.items() if k != 'prose'}, ensure_ascii=False), flush=True)
                with play.drafts.condition:
                    jobs = [{k: v for k, v in job.items() if k not in ('snapshot', 'events', 'subscribers')}
                            for job in play.drafts.jobs.values()]
                (output / 'drafts.json').write_text(json.dumps(jobs, ensure_ascii=False, indent=2))
                report['status'] = ('runtime_passed_pending_prose_review' if len(report['turns']) == turns
                                    and all(r['status'] == 'committed' for r in report['turns']) else 'failed')
            finally:
                play.drafts.close()
                play._jev_shadow_executor.shutdown(wait=True)
                # Preserve the actual isolated session for audit/review; never
                # copy or mutate the application's normal sessions database.
                with sqlite3.connect(Path(tmp) / 'sessions.sqlite') as source:
                    with sqlite3.connect(output / 'sessions.sqlite') as target:
                        source.backup(target)
    except Exception as error:
        report.update(status='failed', errorType=type(error).__name__, message=str(error))
    finally:
        report['elapsedMs'] = round((time.monotonic() - started) * 1000)
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='必须是新的目录；旧证据不覆盖')
    parser.add_argument('--max-calls', type=int, default=30)
    parser.add_argument('--max-seconds', type=int, default=600)
    parser.add_argument('--turns', type=int, default=3)
    args = parser.parse_args()
    report = run(args.output, args.max_calls, args.max_seconds, args.turns)
    print(json.dumps({k: report[k] for k in ('status', 'providerCalls', 'elapsedMs', 'semanticAcceptance')}))
    raise SystemExit(0 if report['status'] == 'runtime_passed_pending_prose_review' else 1)


if __name__ == '__main__':
    main()
