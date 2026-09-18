"""One bounded live ending review per authored case, on temporary longform saves."""
import argparse
import copy
import json
import os
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from .longform import ROOT, longform_cases
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadError, ReadService
from open_story_engine.cocreation import MockPlanner
from open_story_engine.content import load_runtime_story_package
from open_story_engine.llm import LlmError, OpenAICompatibleGateway
from open_story_engine.prompts import catalog_version
from open_story_engine.reader_consequences import initial_goals
from open_story_engine.storage import SessionStore


def seed_branch(play, case, package, scenario):
    """Explicit fixture injection; never presented as accepted generated prose."""
    real = play._planner
    play._planner = MockPlanner()
    cid = package['story']['entryModel']['sourceCharacterIds'][0]
    person = next(c for c in package['characters'] if c['id'] == cid)
    try:
        opening = play.create_session(case['package_id'], case['version'], person['defaultEntryPointId'], cid,
                                      identity_opening=True)
    finally:
        play._planner = real
    sid, parent = opening['session']['id'], opening['branch']['id']
    bid = 'ending-live-' + scenario['id']
    body = scenario['body']
    with closing(SessionStore(str(play.database_path))) as store:
        old = store.branch(sid, parent)
        state = copy.deepcopy(old['branchState'])
        state.setdefault('goalLedger', initial_goals(package, store.contract(sid)))
        changes = dict(goalUpdates=[], threadUpdates=[], outcomes=[])
        for kind, field in [('goal', 'goalLedger'), ('thread', 'threadLedger')]:
            for item in state[field]:
                update = dict(id=item['id'], title=item['title'], status=scenario[kind + '_status'],
                              evidence=body, reason='审查对照夹具，不代表已验收的剧情事实')
                if kind == 'goal':
                    update['dependencies'] = []
                changes[kind + 'Updates'].append(update)
                item.update(update, source='player_branch', causeBranchId=bid)
        node = dict(id=bid, sourceNodeRef=old['sourceNodeRef'], narrativeText=body, summary=scenario['summary'],
                    branchState=state, consequenceUpdate=changes, nextDirections=[], selectedDirectionId='ending-fixture',
                    playerDirection='终局审查隔离夹具', selectedDirection=dict(id='ending-fixture', isFreeText=True, statePatch={}))
        store.append_branch(sid, parent, node)
    play.plan_route_closure(sid, bid, scenario['mode'])
    return sid, bid


def main():
    parser = argparse.ArgumentParser(description='全部达标长篇的终局审查专项；不生成正文、不写正式存档')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=7)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 30:
        parser.error('--max-calls 必须为 1 至 30')
    corpus = longform_cases()
    fixtures = {f['package_id']: f for path in (ROOT / 'test_support/fixtures').glob('ending-review-*.json')
                for f in [json.loads(path.read_text())]}
    missing = [c['package_id'] for c in corpus if c['package_id'] not in fixtures]
    if missing:
        parser.error('以下达标长篇缺少人工审查样本，不能跳过：' + ', '.join(missing))
    required = sum(len(fixtures[c['package_id']]['scenarios']) for c in corpus)
    if not args.dry_run and args.max_calls < required:
        parser.error(f'覆盖全部长篇需要 {required} 次调用；请明确设置足够的 --max-calls')
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(status='running', scope='authored_contract_fixtures_not_narrative_acceptance',
                  prompt_version=catalog_version(), max_calls=args.max_calls, provider_calls=0, novels=[])
    def save():
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    original_request = OpenAICompatibleGateway._request
    def bounded(gateway, *a, **kw):
        if args.dry_run or report['provider_calls'] >= args.max_calls:
            raise LlmError('达到本次审查调用上限', 'model_call_limit')
        report['provider_calls'] += 1
        save()
        return original_request(gateway, *a, **kw)
    save()
    with patch.dict(os.environ, {'STORY_PLANNER': 'openai'}), patch.object(OpenAICompatibleGateway, '_request', bounded):
        for case in corpus:
            record = {k: case[k] for k in ('package_id', 'version', 'cjk', 'sha256')}
            record['checks'] = []
            report['novels'].append(record)
            with TemporaryDirectory(prefix='ose-live-endings-') as tmp:
                read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
                play = PlayService(read, ROOT)
                try:
                    play._require_planner()
                    gateway = play._planner.gateway
                    gateway.stream = False
                    gateway.allow_transport_fallback = False
                    gateway.timeout_seconds = 45
                    record['model'] = gateway.model
                    package = load_runtime_story_package(case['path'], lazy=True)
                    for scenario in fixtures[case['package_id']]['scenarios']:
                        started = time.monotonic()
                        check = dict(id=scenario['id'], expected=scenario['expected'], status='running')
                        record['checks'].append(check)
                        save()
                        try:
                            sid, bid = seed_branch(play, case, package, scenario)
                            with read.store() as store:
                                titles = [g['title'] for g in initial_goals(package, store.contract(sid))]
                            assert titles == fixtures[case['package_id']]['goal_titles'], 'fixture goals no longer match'
                            if args.dry_run:
                                from open_story_engine.route_endings import context, eligible
                                with read.store() as store:
                                    ctx = context(read, store, sid, bid)
                                    eligible(ctx)
                                    assert scenario['quote'] in ctx['nodes'][-1]['narrativeText']
                                check['status'] = 'fixture_valid'
                                continue
                            before = report['provider_calls']
                            proposal = play.propose_ending(sid, bid, scenario['id'], scenario['summary'], scenario['quote'])
                            artifact = case['package_id'] + '-' + scenario['id'] + '.json'
                            (args.output / artifact).write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + '\n')
                            check.update(artifact=artifact, actual=proposal['status'], review=proposal['review'],
                                         metrics=proposal['audit']['metrics'], failure=proposal['audit']['failure'],
                                         provider_calls=report['provider_calls'] - before)
                            assert report['provider_calls'] - before == 1, 'review must make one HTTP request'
                            assert read.journey(sid, bid)['status'] == 'active', 'review auto-ended route'
                            count = report['provider_calls']
                            assert play.propose_ending(sid, bid, scenario['id'], scenario['summary'], scenario['quote'])['id'] == proposal['id']
                            assert report['provider_calls'] == count, 'duplicate reviewed again'
                            if scenario['expected'] == 'allow':
                                assert proposal['status'] == 'approved', 'positive review not approved'
                                receipt = play.commit_ending(sid, bid, proposal['id'])
                                assert receipt['status'] == 'completed'
                                assert play.commit_ending(sid, bid, proposal['id']) == receipt
                                try:
                                    play._turn_snapshot(sid, bid)
                                except ReadError as error:
                                    assert error.code == 'route_ended'
                                else:
                                    raise AssertionError('ended route still accepts continuation')
                            else:
                                assert proposal['status'] == 'rejected', 'negative must be rejected, not transport/format failure'
                                try:
                                    play.commit_ending(sid, bid, proposal['id'])
                                except ReadError as error:
                                    assert error.code == 'ending_review_required'
                                else:
                                    raise AssertionError('negative review committed')
                            assert report['provider_calls'] == count, 'commit made extra calls'
                            check['status'] = 'passed'
                        except Exception as error:
                            check.update(status='failed', code=getattr(error, 'code', type(error).__name__))
                        finally:
                            check['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                            save()
                            print(json.dumps({k: check[k] for k in ('id', 'expected', 'status', 'actual', 'code', 'elapsed_ms')
                                              if k in check}, ensure_ascii=False), flush=True)
                finally:
                    play.drafts.close()
    report['status'] = 'completed'
    report['passed'] = all(c['status'] in ('passed', 'fixture_valid') for n in report['novels'] for c in n['checks'])
    save()
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
