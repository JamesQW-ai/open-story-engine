"""One first-attempt menu per official identity; no prose generation or live commit."""
import argparse
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from .longform import ROOT, longform_cases
from .live_item_dependencies import run_jobs
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.content import load_runtime_story_package
from open_story_engine.llm import OpenAICompatibleGateway, LlmError
from open_story_engine.prompts import catalog_version, render_prompt
from open_story_engine.reader_choices import choice_context, validate_choices, DEPENDENCY_VERSION


def build_cases(case, fixture):
    if (fixture['package_id'], fixture['version'], fixture['sha256']) != (case['package_id'], case['version'], case['sha256']):
        raise ValueError('菜单夹具与当前长篇版本／母本摘要不符')
    package = load_runtime_story_package(case['path'], lazy=True)
    entries = {e['id']: e for e in package['story']['entryModel']['entryPoints']}
    scenarios = fixture['scenarios']
    if len(scenarios) != len(entries) or {s['entry_id'] for s in scenarios} != set(entries):
        raise ValueError('菜单样本必须不重复地覆盖全部预设身份')
    jobs = []
    with TemporaryDirectory(prefix='ose-menu-fixture-') as temp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
        play = PlayService(ReadService(ROOT / 'content/packages', Path(temp) / 'sessions.sqlite'), Path(temp))
        try:
            for scenario in scenarios:
                entry = entries[scenario['entry_id']]
                start = play.create_session(case['package_id'], case['version'], entry['id'], entry['sourceCharacterIds'][0], identity_opening=True)
                _, snap = play._turn_snapshot(start['session']['id'], start['branch']['id'])
                node = copy.deepcopy(snap.history[-1])
                target = scenario['target_id']
                if scenario['kind'] == 'departed':
                    body = scenario['append']
                    name = next(c['name'] for c in snap.package['characters'] if c['id'] == target)
                    if name not in body or not body.strip():
                        raise ValueError('离队样本缺少公开人物依据')
                    node.update(id='authored-menu-departure', kind='generated', parentId=node['id'],
                                narrativeText=node['narrativeText'] + '\n\n' + body)
                    outcome = dict(characterId=target, status='departed', permanence='permanent', evidence=body)
                    node['consequenceUpdate'] = dict(outcomes=[outcome])
                    node['branchState']['characterOutcomeStates'][target] = dict(outcome, causeBranchId=node['id'])
                    node['branchState']['characterLocationIds'].pop(target, None)
                elif scenario['kind'] not in ('unknown_location', 'same_location'):
                    raise ValueError('未登记的菜单验证场景类型')
                context = choice_context(snap.package, snap.story_contract, snap.history, node)
                person = next(p for p in context['people'] if p['id'] == target)
                expected = {'unknown_location': ('unknown', 'contact_unconfirmed'),
                            'same_location': ('unknown', 'same_location'),
                            'departed': ('departed', 'unavailable_outcome')}[scenario['kind']]
                if person['available'] is not scenario['available'] or (person['status'], person['interactionBasis']) != expected:
                    raise ValueError('夹具没有形成声明的人物接触条件')

                def validate(data, context=context, package=snap.package):
                    if not isinstance(data, dict) or not isinstance(data.get('choices'), list) or not 2 <= len(data['choices']) <= 4:
                        raise ValueError('本批场景须给出 2 至 4 项方向')
                    for option in data['choices']:
                        if not isinstance(option, dict) or any(not isinstance(option.get(k), list) for k in ('interactWith', 'mentionOnly', 'useItems')):
                            raise ValueError('方向必须显式声明人物与道具依赖')
                    choices = validate_choices(data, context, package)
                    if len(choices) != len(data['choices']):
                        raise ValueError('原始菜单存在被程序过滤的方向')
                    return choices
                jobs.append(dict(id=entry['id'], validate=validate, messages=[
                    dict(role='system', content=render_prompt('reader.choices')),
                    dict(role='user', content=json.dumps(context, ensure_ascii=False))]))
        finally:
            play.drafts.close()
    return jobs


def main():
    parser = argparse.ArgumentParser(description='全部官方身份的有限首轮菜单验证，不生成正文')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=3)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 30:
        parser.error('--max-calls 必须为 1 至 30')
    corpus = longform_cases()
    fixtures = {f['package_id']: f for p in (ROOT / 'test_support/fixtures').glob('choice-availability-*.json')
                for f in [json.loads(p.read_text())]}
    if any(c['package_id'] not in fixtures for c in corpus):
        parser.error('存在缺少菜单夹具的达标长篇，不能跳过')
    prepared = [(c, build_cases(c, fixtures[c['package_id']])) for c in corpus]
    if not args.dry_run and sum(len(jobs) for _, jobs in prepared) > args.max_calls:
        parser.error('调用预算不足以覆盖全部预设身份')
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(status='running', scope='official_openings_and_authored_departure_not_continuous_play',
                  prompt_version=catalog_version(), dependency_version=DEPENDENCY_VERSION,
                  provider_calls=0, max_calls=args.max_calls, novels=[])
    def save():
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    request = OpenAICompatibleGateway._request
    def bounded(gateway, *a, **kw):
        if args.dry_run or report['provider_calls'] >= args.max_calls:
            raise LlmError('达到本次菜单检查调用上限', 'model_call_limit')
        report['provider_calls'] += 1
        save()
        return request(gateway, *a, **kw)
    save()
    try:
        with TemporaryDirectory(prefix='ose-live-menu-') as temp, \
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
    except Exception as error:
        report.update(status='failed', passed=False, code=getattr(error, 'code', type(error).__name__))
        save()
        raise SystemExit(1)
    report['status'] = 'completed'
    report['passed'] = all(c['status'] in ('passed', 'fixture_valid') for r in report['novels'] for c in r['checks'])
    save()
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
