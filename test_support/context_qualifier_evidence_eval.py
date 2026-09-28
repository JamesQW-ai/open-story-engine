"""One frozen nine-call trial of structured qualifier proposals."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from test_support import context_qualifier_eval as q
from test_support import context_qualifier_evidence as e

f = q.f
PROMPT = f.ROOT/'test_support/prompts/context_qualifier_evidence.md'
OUTPUT = f.OUTPUT.with_name('context-qualifier-evidence-2026-09-26.json')
AUDIT = f.OUTPUT.with_name('context-qualifier-evidence-audit-2026-09-26.json')


def messages(case, entities):
    return [dict(role='system', content=PROMPT.read_text()),
            dict(role='user', content=json.dumps(f.model_input(case, entities), ensure_ascii=False))]


def summary(rows):
    return {s: sum(r['status'] == s for r in rows) for s in
            ('matched', 'mismatched', 'invalid_response', 'model_error', 'harness_error', 'not_run')}


def run(output):
    fixture = q.load_cases()
    config = f.writer_config_from_env()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')) or 'json' not in PROMPT.read_text().lower():
        raise ValueError('模型配置或 JSON 提示无效')
    dependencies = q.DEPENDENCIES + ['test_support/context_qualifier_eval.py', 'test_support/context_qualifier_evidence.py']
    paths = [q.FIXTURE, PROMPT, Path(__file__)] + [f.ROOT/p for p in dependencies]
    report = dict(schemaVersion='context-qualifier-evidence/0.1', status='incomplete',
                  recordedAt=datetime.now(timezone.utc).isoformat(),
                  inputHashes={str(p.relative_to(f.ROOT)): f.sha(p) for p in paths},
                  priorEvidenceHashes={str(p.relative_to(f.ROOT)): f.sha(p) for p in (q.OUTPUT, q.AUDIT)},
                  model={k: config[k] for k in ('route', 'model')}, scope=fixture['scope'],
                  callLimit=9, actualCalls=0, cases=[], acceptance=False, productionEnablement=False,
                  formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    f.save_checkpoint(output, report, create=True)
    recorder = f._gateway(config, 9)

    def save():
        report['actualCalls'] = len(recorder.calls)
        try:
            f.save_checkpoint(output, report)
        except OSError as error:
            raise f.CheckpointError('证据保存失败，停止调用') from error

    blocked = False
    for case in fixture['cases']:
        row = dict(caseId=case['id'], status='incomplete', calls=[])
        report['cases'].append(row)
        if blocked:
            row['status'] = 'not_run'
            save()
            continue
        before = len(recorder.calls)
        try:
            try:
                response = recorder.complete_json(messages(case, fixture['entities']))
            finally:
                row['calls'].extend(recorder.calls[before:])
                save()
            try:
                row.update(e.assess(f.parse_json_content(response.content), case, fixture['entities']))
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
        print(json.dumps(dict(caseId=row['caseId'], status=row['status'])), flush=True)
    report['status'] = 'blocked' if blocked else 'completed'
    report['summary'] = summary(report['cases'])
    save()
    return report


def audit(source=OUTPUT):
    fixture = q.load_cases()
    report = json.loads(source.read_text())
    for name, h in {**report['inputHashes'], **report['priorEvidenceHashes']}.items():
        if f.sha(f.ROOT/name) != h:
            raise ValueError('输入、实现或历史证据变化：'+name)
    if report['status'] != 'completed' or report['actualCalls'] != 9 or len(report['cases']) != 9:
        raise ValueError('实验未完成')
    rows = []
    for case, row in zip(fixture['cases'], report['cases']):
        if case['id'] != row['caseId'] or len(row['calls']) != 1:
            raise ValueError('案例或请求次数不一致')
        call = row['calls'][0]
        if call['messages'] != messages(case, fixture['entities']):
            raise ValueError('实际输入不一致')
        result = e.assess(f.parse_json_content(call['content']), case, fixture['entities'])
        if any(row[k] != v for k, v in result.items()):
            raise ValueError('评分不可复现')
        rows.append(dict(caseId=case['id'], **result))
    if summary(rows) != report['summary']:
        raise ValueError('汇总不可复现')
    name = source.relative_to(f.ROOT) if source.is_relative_to(f.ROOT) else source
    return dict(schemaVersion='context-qualifier-evidence-audit/0.1', sourceArtifact=str(name),
                sourceSha256=f.sha(source), originalCalls=9, newModelCalls=0, actualInputsVerified=True,
                allScoresReproduced=True, cases=rows, summary=summary(rows),
                requiresSemanticReview=True, acceptance=False, productionEnablement=False)


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
