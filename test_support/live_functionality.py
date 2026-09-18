"""Explicit, bounded live checks on every supported official long novel."""
import argparse
import json
import os
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from .longform import ROOT, longform_cases
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService, ReadError
from open_story_engine.api_turn_drafts import TERMINAL, reported_usage
from open_story_engine.content import load_runtime_story_package
from open_story_engine.llm import OpenAICompatibleGateway, LlmError
from open_story_engine.prompts import catalog_version


def main():
    parser = argparse.ArgumentParser(description='正式长篇真实模型功能检查；不会修改正式存档')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=24)
    parser.add_argument('--contracts-only', action='store_true', help='只验证新功能结构化契约和真实流式取消，不尝试修复正文')
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 60:
        parser.error('--max-calls 须为 1 至 60')
    cases = longform_cases()
    args.output.mkdir(parents=True, exist_ok=True)
    report = dict(status='running', prompt_version=catalog_version(), max_calls=args.max_calls,
                  provider_calls=0, completions=[], novels=[])
    lock = threading.Lock()
    request, complete = OpenAICompatibleGateway._request, OpenAICompatibleGateway._complete
    def bounded(gateway, *a, **kw):
        with lock:
            if report['provider_calls'] >= args.max_calls:
                raise LlmError('已达到本次功能检查调用上限', 'model_call_limit')
            report['provider_calls'] += 1
        return request(gateway, *a, **kw)
    def measured(gateway, *a, **kw):
        started = time.monotonic()
        result = None
        code = None
        try:
            result = complete(gateway, *a, **kw)
            return result
        except Exception as error:
            code = getattr(error, 'code', type(error).__name__)
            raise
        finally:
            with lock:
                report['completions'].append(dict(ms=round((time.monotonic()-started)*1000),
                    error=code, usage=reported_usage(result) if result else None))
    def save():
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    save()
    with patch.dict(os.environ, {'STORY_PLANNER': 'openai', 'STORY_LLM_TIMEOUT_SECONDS': '45'}), \
            patch.object(OpenAICompatibleGateway, '_request', bounded), \
            patch.object(OpenAICompatibleGateway, '_complete', measured):
        for case in cases:
            record = dict(package_id=case['package_id'], version=case['version'], cjk=case['cjk'], checks=[])
            report['novels'].append(record)
            with TemporaryDirectory(prefix='ose-live-functions-') as tmp:
                read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
                play = PlayService(read, ROOT)
                package = load_runtime_story_package(case['path'], lazy=True)
                cid = package['story']['entryModel']['sourceCharacterIds'][0]
                person = next(c for c in package['characters'] if c['id'] == cid)
                def check(name, fn):
                    started = time.monotonic()
                    try:
                        value = fn()
                        record['checks'].append(dict(name=name, status='passed', ms=round((time.monotonic()-started)*1000)))
                        return value
                    except Exception as error:
                        record['checks'].append(dict(name=name, status='failed', code=getattr(error, 'code', type(error).__name__),
                            ms=round((time.monotonic()-started)*1000)))
                        return None
                    finally:
                        save()
                        print(json.dumps(record['checks'][-1], ensure_ascii=False), flush=True)
                try:
                    play._require_planner()
                    record['model'] = play._planner.gateway.model
                    if args.contracts_only:
                        from open_story_engine.cocreation import MockPlanner
                        from open_story_engine import reader_consequences as rc
                        from open_story_engine.api_reader_quality import action_requirements
                        from open_story_engine.reader_scene_review import scene_knowledge, public_scene_evidence
                        from open_story_engine.llm import parse_json_content
                        from open_story_engine.reader_scene_plan import validate_scene_plan
                        from open_story_engine.reader_choices import generate_choices
                        real = play._planner
                        play._planner = MockPlanner()
                        opening = play.create_session(case['package_id'], case['version'], person['defaultEntryPointId'], cid, identity_opening=True)
                        play._planner = real
                        sid, parent = opening['session']['id'], opening['branch']['id']
                        binding, snapshot = play._turn_snapshot(sid, parent)
                        context = dict(package=snapshot.package, contract=snapshot.story_contract, parent=snapshot.history[-1], lineage=snapshot.history)
                        for name, action in [('injury_contract', '我用指甲在自己的左手背划出一道浅伤，确认破皮后用衣袖按住伤口，留在原地，不做别的事。'),
                                             ('goal_change_contract', '我决定放弃当前的所有追查目标，今后只以保护自身安全为目标；本回合留在原地，不询问、不移动。')]:
                            def contract(action=action, name=name):
                                requirements = action_requirements(action)
                                active_context = {**context, 'playerDirection': action}
                                result = real.gateway.complete_json([
                                    {'role': 'system', 'content': rc.PLAN_RULES},
                                    {'role': 'user', 'content': json.dumps({**rc.planning_context(active_context), 'input': action,
                                        'requirements': requirements, 'knowledge': scene_knowledge(active_context)}, ensure_ascii=False)}])
                                plan = rc.validate_plan(parse_json_content(result.content), requirements, active_context)
                                assert plan['decision'] == 'ready'
                                validate_scene_plan(plan, public_scene_evidence(active_context),
                                    {c['id'] for c in snapshot.package['characters']})
                                if name == 'injury_contract':
                                    assert any(o['characterId'] == cid and o['status'] == 'injured' for o in plan['outcomes'])
                                else:
                                    assert plan['goalUpdates']
                                (args.output / (case['package_id'] + '-' + name + '.json')).write_text(json.dumps(plan, ensure_ascii=False, indent=2))
                            check(name, contract)
                        def menu():
                            choices, audit = generate_choices(real.gateway, snapshot.package, snapshot.story_contract, snapshot.history, snapshot.history[-1])
                            assert choices and 2 <= len(choices) <= 4
                            (args.output / (case['package_id'] + '-choices.json')).write_text(json.dumps(choices, ensure_ascii=False, indent=2))
                        check('dynamic_choice_contract', menu)
                        def cancel():
                            started = threading.Event()
                            def generate(_snapshot, _payload, delta, reset, validating, guard):
                                def receive(text):
                                    if text:
                                        started.set()
                                        play.drafts.release(sid, parent, 'cancel-live', retire=True)
                                        guard()
                                real.gateway.complete_text([{'role': 'user', 'content': '请用中文简短描述雨中的石阶，只写两句话。'}], receive)
                                raise AssertionError('cancelled stream continued')
                            play.drafts.generate = generate
                            job = play.drafts.ensure(binding, {'text': 'live cancellation only'}, snapshot, subscriber='cancel-live')
                            with play.drafts.condition:
                                assert play.drafts.condition.wait_for(lambda: 'complete_ms' in job['metrics'], timeout=60)
                            assert started.is_set() and job['status'] == 'expired' and 'artifact' not in job
                            with read.store() as store:
                                assert len(store.branches(sid)) == 1
                        check('provider_stream_cancel_without_commit', cancel)
                        record['scope'] = 'authored opening fixture; live contracts and transport, not accepted narrative'
                        continue
                    opening = check('live_opening', lambda: play.create_session(case['package_id'], case['version'],
                        person['defaultEntryPointId'], cid, identity_opening=True))
                    if opening is None:
                        record['blocked'] = 'opening_generation_failed; no quality retry or validation bypass'
                        continue
                    sid, parent = opening['session']['id'], opening['branch']['id']
                    (args.output / (case['package_id'] + '-opening.json')).write_text(json.dumps(opening, ensure_ascii=False, indent=2))
                    action = '留在原地，独自等待片刻，不说话，不移动，也不开始其他行动。'
                    result = check('custom_action_commit', lambda: play.continue_turn(sid, parent, text=action, request_id='live-wait'))
                    if result and result.get('branch'):
                        child = result['branch']['id']
                        count = report['provider_calls']
                        def replay():
                            replayed = play.continue_turn(sid, parent, text=action, request_id='live-wait')
                            assert replayed['branch']['id'] == child and report['provider_calls'] == count
                        check('same_request_no_model_call', replay)
                        (args.output / (case['package_id'] + '-turn.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
                        parent = child
                    else:
                        record['generation_blocker'] = 'custom_action_failed; original branch retained'
                    prepared = check('prepare_current_layer', lambda: play.prepare_choices(sid, parent, 'live-tab'))
                    if prepared and prepared['choices']:
                        deadline = time.monotonic() + 180
                        jobs = [play.drafts.jobs[c['draft_id']] for c in prepared['choices'] if c['draft_id'] in play.drafts.jobs]
                        while any(j['status'] not in TERMINAL for j in jobs) and time.monotonic() < deadline:
                            play.prepare_choices(sid, parent, 'live-tab')
                            with play.drafts.condition:
                                play.drafts.condition.wait(timeout=2)
                        record['prepared_statuses'] = [play.drafts.view(j) for j in jobs]
                        ready = next((c for c in prepared['choices'] if play.drafts.jobs.get(c['draft_id'], {}).get('status') == 'ready'), None)
                        if ready:
                            count = report['provider_calls']
                            def select():
                                value = play.continue_turn(sid, parent, choice_id=ready['id'], draft_id=ready['draft_id'], request_id='live-choice', subscriber_id='live-tab')
                                assert report['provider_calls'] == count
                                return value
                            chosen = check('ready_choice_no_model_call', select)
                            if chosen:
                                parent = chosen['branch']['id']
                        else:
                            record['prepared_blocker'] = 'no_ready_draft; no quality retries'
                    # Snapshot failures before ending can replace their status/error.
                    with play.drafts.condition:
                        private = [{k: v for k, v in j.items() if k not in ('snapshot', 'events', 'subscribers')} for j in play.drafts.jobs.values()]
                    (args.output / (case['package_id'] + '-drafts.json')).write_text(json.dumps(private, ensure_ascii=False, indent=2))
                    check('end_route', lambda: play.end_route(sid, parent))
                    def ended():
                        count = report['provider_calls']
                        try:
                            play.continue_turn(sid, parent, text=action, request_id='after-ending')
                        except ReadError as error:
                            assert error.code == 'route_ended' and count == report['provider_calls']
                            return
                        raise AssertionError('ended route accepted continuation')
                    check('ending_blocks_generation', ended)
                finally:
                    play.drafts.close()
                    with play.drafts.condition:
                        stopped = play.drafts.condition.wait_for(lambda: not any(play.drafts.workers), timeout=100)
                    record['workers_stopped'] = stopped
                    save()
    report['status'] = 'completed'
    save()


if __name__ == '__main__':
    main()
