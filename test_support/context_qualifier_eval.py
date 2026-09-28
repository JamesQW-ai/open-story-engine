"""Bounded paired qualifier experiment; never writes production state."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from test_support import context_fullscan as f
from test_support.context_fullscan_audit import alignment

FIXTURE = f.ROOT/'test_support/fixtures/context-qualifier-cases-2026-09-26.json'
PROMPTS = {'baseline': f.PROMPT, 'clarified': f.ROOT/'test_support/prompts/context_qualifier.md'}
OUTPUT = f.OUTPUT.with_name('context-qualifier-2026-09-26.json')
AUDIT = f.OUTPUT.with_name('context-qualifier-audit-2026-09-26.json')
DEPENDENCIES = [
    'test_support/context_fullscan.py', 'test_support/context_fullscan_audit.py',
    'test_support/context_candidate_mapping.py', 'test_support/context_indexed_pipeline.py',
    'test_support/context_scene_relations.py', 'test_support/context_claim_pipeline.py',
    'test_support/context_planner_smoke.py', 'open_story_engine/llm.py',
]


def load_cases(path=FIXTURE):
    fixture = f.load_cases(path)
    if fixture['parentFixtureSha256'] != f.sha(f.FIXTURE):
        raise ValueError('前序扫描夹具变化')
    if len(fixture['cases']) != 9:
        raise ValueError('本轮冻结为 9 对、18 次调用')
    original = next(c for c in f.load_cases()['cases'] if c['id'] == 'conditional')
    regression = fixture['cases'][0]
    if regression['draft'] != original['draft'] or regression['targets'] != original['targets']:
        raise ValueError('原失败稿或期待标签变化')
    return fixture


def schedule(fixture):
    for index, case in enumerate(fixture['cases']):
        for arm in (('baseline', 'clarified') if index % 2 == 0 else ('clarified', 'baseline')):
            yield case, arm


def messages(case, entities, arm):
    return [dict(role='system', content=PROMPTS[arm].read_text()),
            dict(role='user', content=json.dumps(f.model_input(case, entities), ensure_ascii=False))]


def score(content, case, entities):
    response = f.parse_json_content(content)
    result = f.assess(response, case, entities)
    if result['status'] != 'invalid_response':
        result['alignment'] = alignment(response['items'], case['targets'])
    return result


def summary(rows):
    statuses = ('matched', 'mismatched', 'invalid_response', 'model_error', 'harness_error', 'not_run')
    return {arm: {s: sum(r['arm'] == arm and r['status'] == s for r in rows) for s in statuses}
            for arm in PROMPTS}


def run(output):
    fixture = load_cases()
    config = f.writer_config_from_env()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')):
        raise ValueError('缺少模型配置')
    if any('json' not in p.read_text().lower() for p in PROMPTS.values()):
        raise ValueError('提示缺少 JSON')
    report = dict(schemaVersion='context-qualifier/0.1', status='incomplete',
                  recordedAt=datetime.now(timezone.utc).isoformat(), fixtureSha256=f.sha(FIXTURE),
                  promptHashes={a: f.sha(p) for a, p in PROMPTS.items()}, harnessSha256=f.sha(Path(__file__)),
                  dependencyHashes={p: f.sha(f.ROOT/p) for p in DEPENDENCIES},
                  priorEvidenceHashes={str(p.relative_to(f.ROOT)): f.sha(p) for p in
                                       (f.FIXTURE, f.OUTPUT, f.OUTPUT.with_name('context-fullscan-audit-2026-09-26.json'))},
                  model={k: config[k] for k in ('route', 'model')}, scope=fixture['scope'],
                  callLimit=18, actualCalls=0, cases=[], acceptance=False, productionEnablement=False,
                  formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    f.save_checkpoint(output, report, create=True)
    recorder = f._gateway(config, 18)

    def save():
        report['actualCalls'] = len(recorder.calls)
        try:
            f.save_checkpoint(output, report)
        except OSError as error:
            raise f.CheckpointError('证据保存失败，停止调用') from error

    blocked = False
    for case, arm in schedule(fixture):
        row = dict(caseId=case['id'], arm=arm, status='incomplete', calls=[])
        report['cases'].append(row)
        if blocked:
            row['status'] = 'not_run'
            save()
            continue
        before = len(recorder.calls)
        try:
            try:
                response = recorder.complete_json(messages(case, fixture['entities'], arm))
            finally:
                row['calls'].extend(recorder.calls[before:])
                save()
            try:
                row.update(score(response.content, case, fixture['entities']))
            except (ValueError, f.LlmError) as error:
                row.update(status='invalid_response', error=str(error))
        except f.CheckpointError:
            raise
        except f.LlmError as error:
            row.update(status='model_error', error=str(error))
            blocked = True
        except Exception as error:
            row.update(status='harness_error', error=str(error))
            blocked = True
        save()
        print(json.dumps(dict(caseId=row['caseId'], arm=arm, status=row['status'])), flush=True)
    report['status'] = 'blocked' if blocked else 'completed'
    report['summary'] = summary(report['cases'])
    save()
    return report


def audit(source=OUTPUT):
    fixture = load_cases()
    report = json.loads(source.read_text())
    if report['fixtureSha256'] != f.sha(FIXTURE) or report['harnessSha256'] != f.sha(Path(__file__)):
        raise ValueError('实验输入或程序变化')
    if report['promptHashes'] != {a: f.sha(p) for a, p in PROMPTS.items()}:
        raise ValueError('提示变化')
    for name, h in {**report['dependencyHashes'], **report['priorEvidenceHashes']}.items():
        if f.sha(f.ROOT/name) != h:
            raise ValueError('依赖或旧证据变化：'+name)
    if report['status'] != 'completed' or len(report['cases']) != 18 or report['actualCalls'] != 18:
        raise ValueError('未完成完整配对')
    results = []
    for row, (case, arm) in zip(report['cases'], schedule(fixture)):
        if (row['caseId'], row['arm']) != (case['id'], arm) or len(row['calls']) != 1:
            raise ValueError('请求顺序或数量变化')
        call = row['calls'][0]
        if call['messages'] != messages(case, fixture['entities'], arm):
            raise ValueError('实际输入与冻结输入不符')
        rescored = score(call['content'], case, fixture['entities'])
        if any(row[k] != v for k, v in rescored.items()):
            raise ValueError('评分不可复现')
        results.append(dict(caseId=case['id'], arm=arm, **rescored))
    if summary(results) != report['summary']:
        raise ValueError('汇总不可复现')
    source_name = source.relative_to(f.ROOT) if source.is_relative_to(f.ROOT) else source
    return dict(schemaVersion='context-qualifier-audit/0.1', sourceArtifact=str(source_name),
                sourceSha256=f.sha(source), allScoresReproduced=True, actualInputsVerified=True,
                originalCalls=18, newModelCalls=0, cases=results, summary=summary(results),
                acceptance=False, productionEnablement=False, requiresQuoteSemanticReview=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.audit:
        result = audit()
        f.save_checkpoint(args.output or AUDIT, result, create=True)
    else:
        f._load_env(f.ROOT/'.env')
        result = run(args.output or OUTPUT)
    print(json.dumps(result['summary']))
