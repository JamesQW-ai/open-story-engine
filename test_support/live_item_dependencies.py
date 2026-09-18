"""Bounded first-attempt item-contract checks; never narrative acceptance."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

from .longform import ROOT, longform_cases
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.api_reader_quality import action_requirements
from open_story_engine.api_turn_drafts import reported_usage
from open_story_engine import reader_actions as ra, reader_consequences as rc, item_lifecycle as items
from open_story_engine.llm import OpenAICompatibleGateway, LlmError, parse_json_content
from open_story_engine.prompts import catalog_version, render_prompt
from open_story_engine.reader_choices import choice_context, validate_choices
from open_story_engine.reader_scene_plan import validate_scene_plan
from open_story_engine.reader_scene_review import scene_knowledge, public_scene_evidence
from open_story_engine.route_outline import build_outline


def build_cases(case, fixture):
    """Seed explicit authored alternatives, not outputs from preceding live checks."""
    if (fixture['version'], fixture['sha256']) != (case['version'], case['sha256']):
        raise ValueError('道具样本与当前长篇版本／母本摘要不符')
    with TemporaryDirectory(prefix='ose-item-fixture-') as temp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
        read = ReadService(ROOT / 'content/packages', Path(temp) / 'sessions.sqlite')
        play = PlayService(read, Path(temp))
        try:
            start = play.create_session(case['package_id'], case['version'], fixture['entry_id'],
                                        fixture['character_id'], identity_opening=True)
            _, snapshot = play._turn_snapshot(start['session']['id'], start['branch']['id'])
            context = dict(package=snapshot.package, contract=snapshot.story_contract,
                           parent=snapshot.history[-1], lineage=snapshot.history)
        finally:
            play.drafts.close()
    package, root = context['package'], context['parent']
    cid, iid = fixture['character_id'], fixture['item_id']
    assert root['branchState']['itemOwnerCharacterIds'].get(iid) == cid, '样本道具不在指定开局角色手中'
    assert next(i['name'] for i in package['items'] if i['id'] == iid) == fixture['item_name']
    bid = 'item-contract-established'
    goal = dict(id='new', title=fixture['goal_title'], status='active', dependencies=[],
                itemDependencies=[iid], reason='隔离样本中的明确计划', evidence=fixture['established_body'])
    thread = dict(id='new-1', title=fixture['thread_title'], status='open', itemDependencies=[iid],
                  stepIds=['S1'], reason='隔离样本中的待查问题', evidence=fixture['established_body'])
    gid, tid = [kind + '-' + hashlib.sha256((bid + ':0').encode()).hexdigest()[:16] for kind in ('goal', 'thread')]
    state = copy.deepcopy(root['branchState'])
    state.setdefault('goalLedger', rc.initial_goals(package, context['contract'])).append(
        dict(goal, id=gid, source='player_branch', causeBranchId=bid))
    state['threadLedger'].append(dict(thread, id=tid, source='player_branch', openedBranchId=bid, causeBranchId=bid))
    established = dict(root, id=bid, parentId=root['id'], sequence=1, branchState=state,
                       narrativeText=fixture['established_body'],
                       consequenceUpdate=dict(goalUpdates=[goal], threadUpdates=[thread], stateChanges=[]))
    established_context = dict(context, parent=established, lineage=[*context['lineage'], established])
    change = dict(id='C1', entityId=iid, attribute=items.ATTRIBUTE, before=None, value=True,
                  stepId='S1', reason='隔离样本已确认损毁', evidence=fixture['destroyed_body'])
    projection = dict(introductions=dict(characters=[], items=[], locations=[]), stateChanges=[change], steps=[])
    destroyed = dict(established, id='item-contract-destroyed', parentId=bid, sequence=2,
                     branchState=ra.project(state, projection, package), narrativeText=fixture['destroyed_body'],
                     consequenceUpdate=dict(goalUpdates=[], threadUpdates=[], stateChanges=[change]))
    destroyed_context = dict(context, parent=destroyed, lineage=[*established_context['lineage'], destroyed])
    conflicts = build_outline(package, context['contract'], destroyed_context['lineage'])['conflicts']
    assert {c['target_id'] for c in conflicts} == {gid, tid}, '夹具未产生目标与问题两个阻碍'
    assert all(c['status'] == 'destroyed' for c in conflicts)
    jobs = []
    for stage, ctx in (('establish', context), ('destroy', established_context), ('reroute', destroyed_context)):
        ctx = dict(ctx, playerDirection=fixture[stage + '_action'])
        requirements = action_requirements(ctx['playerDirection'])
        messages = [dict(role='system', content=rc.PLAN_RULES), dict(role='user', content=json.dumps(
            {**rc.planning_context(ctx), 'input': ctx['playerDirection'], 'requirements': requirements,
             'knowledge': scene_knowledge(ctx)}, ensure_ascii=False))]

        def validate(data, stage=stage, ctx=ctx, requirements=requirements):
            plan = rc.validate_plan(data, requirements, ctx)
            assert plan['decision'] == 'ready', '未给出可执行契约'
            validate_scene_plan(plan, public_scene_evidence(ctx),
                {key for key, entity in ra.registry(package, ctx['parent']['branchState'], plan['introductions']).items()
                 if entity['kind'] == 'character'})
            if stage == 'establish':
                assert any(g['id'] == 'new' and iid in g.get('itemDependencies', []) for g in plan['goalUpdates']), '新目标漏记道具依赖'
                assert any(t['id'].startswith('new-') and iid in t.get('itemDependencies', [])
                           for t in plan.get('threadUpdates', [])), '新问题漏记道具依赖'
            elif stage == 'destroy':
                assert any(c['entityId'] == iid and c['attribute'] == items.ATTRIBUTE and c['value'] is True
                           for c in plan['stateChanges']), '漏记永久损毁标记'
                assert any(iid in s.get('usedItemIds', []) for s in plan['steps']), '损毁步骤漏记原物使用依赖'
                assert not plan['goalUpdates'] and not plan.get('threadUpdates'), '未获授权却改动目标／问题'
                projected = ra.project(ctx['parent']['branchState'], plan, package)
                assert items.destroyed(projected, iid) and iid not in projected['itemOwnerCharacterIds']
            else:
                assert any(g['id'] == gid and g['status'] == 'active' and g.get('itemDependencies') == []
                           for g in plan['goalUpdates']), '未明确解除原目标的失效道具依赖'
                assert any(t['id'] == tid and t['status'] == 'open' and t.get('itemDependencies') == []
                           for t in plan.get('threadUpdates', [])), '未明确解除原问题的失效道具依赖'
                assert all(iid not in s.get('usedItemIds', []) for s in plan['steps']), '再次使用已毁原物'
            return plan
        jobs.append(dict(id=stage, messages=messages, validate=validate))

    registry = ra.registry(package, state)
    observation = dict(viewpoint=dict(id=cid, name=context['contract']['persona']['name']),
        registry={key: {k: e[k] for k in ('id', 'name', 'kind', 'aliases') if k in e} for key, e in registry.items()},
        state=rc.prompt_state(state), previous=established['narrativeText'],
        attributeVocabulary=ra.attribute_vocabulary(package, state, dict(projection, stateChanges=[])),
        draft={f'P{i+1}': p for i, p in enumerate(fixture['destroyed_body'].split('\n\n'))})

    def validate_observation(data):
        events = ra.validate_observations(data, fixture['destroyed_body'])
        assert any(e['mode'] == 'actual' and any(c['entityId'] == iid and c['attribute'] == items.ATTRIBUTE
                   and c['value'] is True for c in e['changes']) for e in events), '独立提取漏报实际永久损毁'
        assert any(iid in e.get('usedItemIds', []) for e in events if e['mode'] == 'actual'), '独立提取漏报道具使用'
        return events
    jobs.append(dict(id='observe_destroyed', validate=validate_observation, messages=[
        dict(role='system', content=ra.OBSERVE_RULES + render_prompt('reader.scene_observe')),
        dict(role='user', content=json.dumps(observation, ensure_ascii=False))]))
    menu_context = choice_context(package, context['contract'], established_context['lineage'], destroyed)

    def validate_menu(data):
        # Test every model suggestion: silent filtering is not acceptance of the raw menu.
        assert isinstance(data, dict) and isinstance(data.get('choices'), list) and 2 <= len(data['choices']) <= 4
        choices = validate_choices(data, menu_context, package)
        assert len(choices) == len(data['choices']), '模型菜单有方向被程序过滤'
        assert all(isinstance(c.get('useItems'), list) and iid not in c['useItems'] for c in data['choices']), '遗漏使用声明或使用已毁原物'
        return choices
    jobs.append(dict(id='choices_after_destruction', validate=validate_menu, messages=[
        dict(role='system', content=render_prompt('reader.choices')),
        dict(role='user', content=json.dumps(menu_context, ensure_ascii=False))]))
    return jobs


def run_jobs(jobs, gateway, output, record, save, dry_run=False):
    for job in jobs:
        started = time.monotonic()
        check = dict(id=job['id'], status='running', usage=None)
        record['checks'].append(check)
        artifact = dict(messages=job['messages'], scope='authored_state_contract_probe')
        try:
            if dry_run:
                check['status'] = 'fixture_valid'
            else:
                completion = gateway.complete_json(job['messages'])
                artifact.update(raw_response=completion.raw_response, response=completion.content,
                                observations=completion.observations)
                check['usage'] = reported_usage(completion)
                artifact['validated'] = job['validate'](parse_json_content(completion.content))
                check['status'] = 'passed'
        except Exception as error:
            check.update(status='failed', code=getattr(error, 'code', type(error).__name__))
            # Provider errors can contain URLs. Only local validation details are public.
            if isinstance(error, (AssertionError, ValueError)):
                check['reason'] = str(error)
            elif isinstance(error, LlmError):
                artifact['observations'] = error.observations
                artifact['raw_response'] = error.raw_response
                check['usage'] = reported_usage(error)
        finally:
            check['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            check['artifact'] = record['package_id'] + '-' + job['id'] + '.json'
            (output / check['artifact']).write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + '\n')
            save()
            print(json.dumps(check, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description='全部官方长篇的道具依赖真实模型契约检查；不写正文或正式存档')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    corpus = longform_cases()
    fixtures = {f['package_id']: f for path in (ROOT / 'test_support/fixtures').glob('item-dependencies-*.json')
                for f in [json.loads(path.read_text())]}
    if not 1 <= args.max_calls <= 30:
        parser.error('--max-calls 必须为 1 至 30')
    missing = [c['package_id'] for c in corpus if c['package_id'] not in fixtures]
    if missing:
        parser.error('以下达标长篇缺少人工样本，不能跳过：' + ', '.join(missing))
    if not args.dry_run and args.max_calls < 5 * len(corpus):
        parser.error('调用上限不足以覆盖全部长篇的五项检查')
    # Validate every fixture before any paid call or persistent output.
    prepared = [(case, build_cases(case, fixtures[case['package_id']])) for case in corpus]
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(status='running', scope='independent_authored_snapshots_not_continuous_play',
                  prompt_version=catalog_version(), provider_calls=0, max_calls=args.max_calls, novels=[])
    def save():
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    request = OpenAICompatibleGateway._request
    def bounded(gateway, *a, **kw):
        if args.dry_run or report['provider_calls'] >= args.max_calls:
            raise LlmError('达到本次检查调用上限', 'model_call_limit')
        report['provider_calls'] += 1
        save()
        return request(gateway, *a, **kw)
    save()
    with TemporaryDirectory(prefix='ose-live-item-') as temp, \
            patch.dict(os.environ, {'STORY_PLANNER': 'mock' if args.dry_run else 'openai'}), \
            patch.object(OpenAICompatibleGateway, '_request', bounded):
        play = PlayService(ReadService(ROOT / 'content/packages', Path(temp) / 'sessions.sqlite'), ROOT)
        try:
            gateway = None
            if not args.dry_run:
                play._require_planner()
                gateway = play._planner.gateway
                gateway.stream, gateway.allow_transport_fallback, gateway.timeout_seconds = False, False, 45
                gateway.remaining_calls = args.max_calls
            for case, jobs in prepared:
                record = {k: case[k] for k in ('package_id', 'version', 'cjk', 'sha256')}
                record.update(model=gateway.model if gateway else None, checks=[])
                report['novels'].append(record)
                run_jobs(jobs, gateway, args.output, record, save, args.dry_run)
        finally:
            play.drafts.close()
    report['status'] = 'completed'
    report['passed'] = all(c['status'] in ('passed', 'fixture_valid') for r in report['novels'] for c in r['checks'])
    save()
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
