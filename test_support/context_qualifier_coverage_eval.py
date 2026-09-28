"""Once-only coverage evaluation; distinguish answered slots from semantic fidelity."""
import argparse
import hashlib
from collections import Counter
from statistics import median
import json
from datetime import datetime, timezone
from pathlib import Path

from test_support import context_qualifier_core_contract_eval as previous
from test_support import context_qualifier_review_view as review
from test_support import context_qualifier_coverage as coverage
from test_support import context_qualifier_coverage_projection as projection

f, e = previous.f, previous.e
PROMPT = f.ROOT/'test_support/prompts/context_qualifier_coverage.md'
EXPECTED_PROMPT_SHA256 = '5bf24d139d1d846796c11c9ca281dee11cf67c89866637ee0d66b44c39746b1f'
OUTPUT = previous.OUTPUT.with_name('context-qualifier-coverage-eval-2026-09-26.json')
AUDIT = previous.AUDIT.with_name('context-qualifier-coverage-eval-audit-2026-09-26.json')
CALL_LIMIT = 38
FIXTURE = f.ROOT/'test_support/fixtures/context-qualifier-coverage-cases-2026-09-26.json'
EXPECTED_FIXTURE_SHA256 = 'cd821ecdbab11976eac401393730edc04e611cb435ab63c226d49b9ce472dd0f'
INPUT_FILES = (PROMPT, FIXTURE, Path(__file__), Path(review.__file__),
               Path(coverage.__file__), Path(projection.__file__), Path(projection.replay.__file__))
EVIDENCE_FILES = (previous.OUTPUT, previous.AUDIT, projection.OUTPUT,
                  projection.replay.OUTPUT, projection.replay.AUDIT)


def summary(rows):
    return {k: sum(r['status'] == k for r in rows) for k in
            ('matched', 'scope_pending', 'mismatched', 'invalid_response',
             'model_error', 'harness_error', 'not_run')}


def load_cases():
    if f.sha(PROMPT) != EXPECTED_PROMPT_SHA256:
        raise ValueError('候选提示哈希变化')
    if f.sha(FIXTURE) != EXPECTED_FIXTURE_SHA256:
        raise ValueError('覆盖夹具或标签哈希变化')
    if projection.build() != json.loads(projection.OUTPUT.read_text()):
        raise ValueError('前序覆盖投影证据变化')
    fixture = previous.load_cases()
    extra = json.loads(FIXTURE.read_text())
    fixture['scope'] = ('30 条已见固定回归加 8 条合成控制；其中 2 条仅验证未决边界，'
                        '不计语义通过；不是未见场景或正文质量验收。')
    fixture['cases'].extend(extra['cases'])
    if len(fixture['cases']) != CALL_LIMIT or set(extra['expectations']) != {c['id'] for c in fixture['cases']}:
        raise ValueError('固定案例集合变化')
    for c in fixture['cases']:
        gold = extra['expectations'][c['id']]
        index = coverage.model_input(c, fixture['scenes'][c['sceneKey']])['coverageIndex']
        if (gold['draftSha256'] != hashlib.sha256(c['draft'].encode()).hexdigest()
                or set(gold['decisions']) != set(index['slots'])
                or gold['pendingUnits'] != index['pendingUnits']):
            raise ValueError('原文、覆盖标签或未决边界变化')
        c['coverageExpectation'] = gold
    return fixture


def messages(case, entities):
    return [dict(role='system', content=PROMPT.read_text()),
            dict(role='user', content=json.dumps(projection.model_input(case, entities), ensure_ascii=False))]


def decision_score(response, case):
    expected = case['coverageExpectation']['decisions']
    raw = response.get(coverage.VERSION) if isinstance(response, dict) else None
    if not isinstance(raw, list):
        return dict(available=False)
    decisions = [x for x in raw if isinstance(x, dict) and isinstance(x.get('slotId'), str)]
    ids = Counter(x['slotId'] for x in decisions)
    actual = {x['slotId']: x.get('disposition') for x in decisions}
    wrong = [sid for sid, value in expected.items() if sid in actual and actual[sid] != value]
    return dict(available=True, expectedSlots=len(expected), missingDecisions=len(set(expected)-set(ids)),
                extraDecisions=sum(n for sid, n in ids.items() if sid not in expected),
                duplicateDecisions=sum(n-1 for n in ids.values()), wrongDispositions=wrong,
                incorrectNonPosition=sum(actual[sid] == 'non_position' and expected[sid] == 'position' for sid in wrong),
                incorrectPosition=sum(actual[sid] == 'position' and expected[sid] == 'non_position' for sid in wrong),
                matched=(len(decisions) == len(raw) == len(expected) and set(ids) == set(expected)
                         and all(n == 1 for n in ids.values()) and not wrong))


def position_metrics(proposal, case, entities):
    # Count semantic content separately from strict core/cue/premise scoring.
    refs = e.previous.entity_refs(case, entities)
    remaining = list(zip(case['selectionExpectations'], case['targets']))
    extra, matched, false_positive, false_negative = 0, 0, 0, 0
    for item in proposal[e.VERSION]:
        p = dict(subject=refs[item['subject']]['entityIds'][0], object=refs[item['boundary']]['entityIds'][0],
                 **item['position'])
        found = next((n for n, (gold, _) in enumerate(remaining)
                      if all(gold['position'][k] == value for k, value in p.items())), None)
        if found is None:
            extra += 1
            continue
        _, target = remaining.pop(found)
        matched += 1
        wanted = Counter(target['expectedLimitations'])
        actual = Counter(x['kind'] for x in item['limitations'])
        false_positive += sum((actual-wanted).values())
        false_negative += sum((wanted-actual).values())
    return dict(expectedPositions=len(case['targets']), returnedPositions=len(proposal[e.VERSION]),
                contentMatchedPositions=matched, missingPositions=len(remaining), extraPositions=extra,
                qualifierFalsePositive=false_positive, qualifierFalseNegative=false_negative,
                qualifierComparisonScope='content_matched_positions_only')


def assess_content(content, case, entities):
    result = dict(semanticStatus='unverified', reviewRequired=True, productionEnablement=False)
    try:
        response = f.parse_json_content(content)
        result.update(proposedResponse=response, decisionScore=decision_score(response, case))
        checked = coverage.inspect(response, case, entities)
        if checked['status'] != 'structurally_valid':
            result.update(status='invalid_response', error=checked['error'])
            return result
        proposal, ledger = checked['decodedProposal'], checked['coverage']
        strict = e.assess(proposal, case, entities)
        matched = strict['status'] == 'matched' and result['decisionScore']['matched']
        status = 'matched' if matched else 'mismatched'
        if case['cohort'] == 'scope_controls' and matched:
            status = 'scope_pending'  # Empty extraction is NOT semantic success.
        result.update(status=status, extractionAssessment=strict, coverage=ledger,
                      positionMetrics=position_metrics(proposal, case, entities),
                      reviewView=review.build(proposal, case, entities))
    except (ValueError, f.LlmError) as error:
        result.update(status='invalid_response', error=str(error))
    return result


def cohort_summaries(rows):
    return {name: summary([r for r in rows if r['cohort'] == name])
            for name in sorted({r['cohort'] for r in rows})}


def metrics(rows):
    scored = [r['positionMetrics'] for r in rows if 'positionMetrics' in r]
    decisions = [r['decisionScore'] for r in rows if r.get('decisionScore', {}).get('available')]
    calls = [c for r in rows for c in r['calls']]
    usage, durations = [], []
    for call in calls:
        try:
            u = json.loads(call['rawResponse']).get('usage', {})
            if all(type(u.get(k)) is int for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
                usage.append(u)
        except (ValueError, TypeError):
            pass
        observations = call.get('observations', [])
        for observation in observations:
            duration = observation.get('transport', {}).get('durationMs')
            if isinstance(duration, (int, float)):
                durations.append(duration)
    return dict(positionScoredCases=len(scored), decisionScoredCases=len(decisions),
                positions={k: sum(p[k] for p in scored) for k in ('expectedPositions', 'returnedPositions',
                    'contentMatchedPositions', 'missingPositions', 'extraPositions',
                    'qualifierFalsePositive', 'qualifierFalseNegative')},
                decisions={k: sum(p[k] for p in decisions) for k in ('expectedSlots', 'missingDecisions',
                    'extraDecisions', 'duplicateDecisions', 'incorrectNonPosition', 'incorrectPosition')},
                costs=dict(usageRecordedCalls=len(usage), tokens={k: sum(u[k] for u in usage)
                           for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
                           durationRecordedObservations=len(durations),
                           medianDurationMs=median(durations) if durations else None,
                           inputChars=sum(len(m['content']) for c in calls for m in c['messages'])))


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
    report = dict(schemaVersion='context-qualifier-coverage-eval/0.1', status='incomplete',
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
    if (report['schemaVersion'] != 'context-qualifier-coverage-eval/0.1'
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
    return dict(schemaVersion='context-qualifier-coverage-eval-audit/0.1', sourceArtifact=str(name),
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
