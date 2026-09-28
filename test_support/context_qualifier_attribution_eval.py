"""Frozen attribution prompt replacement, same frame contract and labels."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from test_support import context_qualifier_frame_eval as previous

f = previous.f
PROMPT = f.ROOT/'test_support/prompts/context_qualifier_attribution_v2.md'
EXPECTED_PROMPT_SHA256 = 'a9119b26726b32760872c2289012657eff8fcffbcfd904fbccbddea9510b688b'
OUTPUT = previous.OUTPUT.with_name('context-qualifier-attribution-eval-2026-09-27.json')
AUDIT = previous.AUDIT.with_name('context-qualifier-attribution-eval-audit-2026-09-27.json')
CALL_LIMIT = previous.CALL_LIMIT
INPUT_FILES = (Path(__file__), PROMPT)
EVIDENCE_FILES = (previous.OUTPUT, previous.AUDIT)
summary = previous.summary
cohort_summaries = previous.cohort_summaries
assess_content = previous.assess_content
metrics = previous.metrics


def prompt_text():
    text = PROMPT.read_text()
    if f.sha(PROMPT) != EXPECTED_PROMPT_SHA256:
        raise ValueError('归属提示哈希变化')
    old = previous.replay.PROMPT.read_text().split('\n\n')
    new = text.split('\n\n')
    if (len(new) != len(old) or any(new[i] != old[i] for i in range(len(old)) if i != 2)
            or len(new[2]) > len(old[2])):
        raise ValueError('只能替换归属段落且不得增加字符预算')
    return text


def load_cases():
    prompt_text()
    if previous.audit() != json.loads(previous.AUDIT.read_text()):
        raise ValueError('前序框架实测审计变化')
    return previous.load_cases()


def messages(case, entities):
    result = previous.messages(case, entities)
    result[0] = dict(role='system', content=prompt_text())
    return result


def run(output=OUTPUT):
    fixture = load_cases()
    for c in fixture['cases']:
        messages(c, fixture['scenes'][c['sceneKey']])
    config = f.writer_config_from_env()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')):
        raise ValueError('模型配置无效')
    prior = json.loads(previous.OUTPUT.read_text())
    hashes = dict(prior['inputHashes'])
    for path in INPUT_FILES:
        hashes[str(path.relative_to(f.ROOT))] = f.sha(path)
    evidence = dict(prior['priorEvidenceHashes'])
    for path in EVIDENCE_FILES:
        evidence[str(path.relative_to(f.ROOT))] = f.sha(path)
    report = dict(schemaVersion='context-qualifier-attribution-eval/0.1', status='incomplete',
                  recordedAt=datetime.now(timezone.utc).isoformat(), inputHashes=hashes,
                  priorEvidenceHashes=evidence, scope=fixture['scope'],
                  model={k: config[k] for k in ('route', 'model')}, callLimit=CALL_LIMIT,
                  actualCalls=0, cases=[], acceptance=False, productionEnablement=False,
                  formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    f.save_checkpoint(output, report, create=True)
    recorder = f._gateway(config, CALL_LIMIT)

    def save():
        report['actualCalls'] = len(recorder.calls)
        try:
            f.save_checkpoint(output, report)
        except OSError as error:
            raise f.CheckpointError('证据保存失败，停止调用') from error

    blocked = False
    for c in fixture['cases']:
        row = dict(caseId=c['id'], cohort=c['cohort'], status='incomplete', calls=[])
        report['cases'].append(row)
        if blocked:
            row['status'] = 'not_run'
            save()
            continue
        before = len(recorder.calls)
        try:
            try:
                response = recorder.complete_json(messages(c, fixture['scenes'][c['sceneKey']]))
            finally:
                row['calls'].extend(recorder.calls[before:])
                save()
            row.update(assess_content(response.content, c, fixture['scenes'][c['sceneKey']]))
        except f.CheckpointError:
            raise
        except f.LlmError as error:
            row.update(status='model_error', error=str(error))
            blocked = True
        except Exception as error:
            row.update(status='harness_error', error=str(error))
            blocked = True
        save()
        print(json.dumps(dict(caseId=c['id'], status=row['status'])), flush=True)
    report.update(status='blocked' if blocked else 'completed', summary=summary(report['cases']),
                  cohorts=cohort_summaries(report['cases']), metrics=metrics(report['cases']))
    save()
    return report


def audit(source=OUTPUT):
    fixture = load_cases()
    report = json.loads(source.read_text())
    prior = json.loads(previous.OUTPUT.read_text())
    expected_inputs = dict(prior['inputHashes'])
    expected_evidence = dict(prior['priorEvidenceHashes'])
    for path in INPUT_FILES:
        expected_inputs[str(path.relative_to(f.ROOT))] = f.sha(path)
    for path in EVIDENCE_FILES:
        expected_evidence[str(path.relative_to(f.ROOT))] = f.sha(path)
    if report['inputHashes'] != expected_inputs or report['priorEvidenceHashes'] != expected_evidence:
        raise ValueError('输入、实现或历史证据绑定变化')
    for name, digest in {**expected_inputs, **expected_evidence}.items():
        if f.sha(f.ROOT/name) != digest:
            raise ValueError('绑定文件变化：'+name)
    if (report['status'] != 'completed' or report['actualCalls'] != CALL_LIMIT
            or report['callLimit'] != CALL_LIMIT or len(report['cases']) != CALL_LIMIT):
        raise ValueError('实验未完成或预算变化')
    if (report['schemaVersion'] != 'context-qualifier-attribution-eval/0.1'
            or report['scope'] != fixture['scope']
            or report['acceptance'] is not False or report['productionEnablement'] is not False
            or any(report[k] != 0 for k in ('formalSessionWrites', 'reviewRecordsWritten', 'jevCalls'))):
        raise ValueError('实验边界字段变化')
    rows = []
    for c, row in zip(fixture['cases'], report['cases']):
        if c['id'] != row['caseId'] or c['cohort'] != row['cohort'] or len(row['calls']) != 1:
            raise ValueError('案例、分组或请求次数变化')
        call = row['calls'][0]
        raw = json.loads(call['rawResponse'])
        if raw['choices'][0]['message']['content'] != call['content']:
            raise ValueError('原始响应与评分输入不同')
        if call['messages'] != messages(c, fixture['scenes'][c['sceneKey']]):
            raise ValueError('实际输入变化')
        result = assess_content(call['content'], c, fixture['scenes'][c['sceneKey']])
        if {k: v for k, v in row.items() if k not in ('caseId', 'cohort', 'calls')} != result:
            raise ValueError('评分不可复现')
        rows.append(dict(caseId=c['id'], cohort=c['cohort'], **result))
    if (summary(rows) != report['summary'] or cohort_summaries(rows) != report['cohorts']
            or metrics(report['cases']) != report['metrics']):
        raise ValueError('汇总不可复现')
    name = source.relative_to(f.ROOT) if source.is_relative_to(f.ROOT) else source
    return dict(schemaVersion='context-qualifier-attribution-eval-audit/0.1', sourceArtifact=str(name),
                sourceSha256=f.sha(source), originalCalls=CALL_LIMIT, newModelCalls=0,
                actualInputsVerified=True, allScoresReproduced=True, cases=rows,
                summary=summary(rows), cohorts=cohort_summaries(rows), metrics=metrics(report['cases']),
                requiresSemanticReview=True, acceptance=False, productionEnablement=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    args = parser.parse_args()
    if args.audit:
        result = audit()
        f.save_checkpoint(AUDIT, result, create=True)
    else:
        f._load_env(f.ROOT/'.env')
        result = run()
    print(json.dumps(result['cohorts']))
