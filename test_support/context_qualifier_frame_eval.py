"""Fixed-budget evaluation of attribution and dependency choices; no repairs."""
import argparse
from collections import Counter
import json
from datetime import datetime, timezone
from pathlib import Path

from test_support import context_qualifier_frame_replay as replay
from test_support import context_qualifier_frame as frame

previous = replay.previous
f = previous.f
OUTPUT = previous.OUTPUT.with_name('context-qualifier-frame-eval-2026-09-27.json')
AUDIT = previous.AUDIT.with_name('context-qualifier-frame-eval-audit-2026-09-27.json')
CALL_LIMIT = 44
INPUT_FILES = (Path(__file__),)
EVIDENCE_FILES = (replay.OUTPUT, replay.AUDIT)
messages = replay.messages


def load_cases():
    if replay.audit() != json.loads(replay.AUDIT.read_text()):
        raise ValueError('前序替代契约回放审计变化')
    fixture = replay.load_cases()
    if len(fixture['cases']) != CALL_LIMIT:
        raise ValueError('冻结案例数量与调用预算不一致')
    fixture['scope'] = ('44 条固定用例，其中 4 条只验证未决路由；'
                        '非未见场景、来源支持或正文质量验收。')
    return fixture


def summary(rows):
    return {k: sum(r['status'] == k for r in rows) for k in
            ('matched', 'scope_pending', 'dependency_pending', 'mismatched',
             'invalid_response', 'model_error', 'harness_error', 'not_run')}


def cohort_summaries(rows):
    return {name: summary([r for r in rows if r['cohort'] == name])
            for name in sorted({r['cohort'] for r in rows})}


def attribution_score(response, case):
    expected = case['frameAttributions']
    raw = response.get(frame.VERSION) if isinstance(response, dict) else None
    if not isinstance(raw, list):
        return dict(available=False)
    decisions = [x for x in raw if isinstance(x, dict) and isinstance(x.get('subject'), str)]
    counts = Counter(x['subject'] for x in decisions)
    actual = {x['subject']: x.get('attribution') for x in decisions}
    wrong = [dict(subject=subject, expected=value, actual=actual[subject])
             for subject, value in expected.items() if subject in actual and actual[subject] != value]
    return dict(available=True, expectedSubjects=len(expected),
                missingDecisions=len(set(expected)-set(counts)),
                extraDecisions=sum(n for subject, n in counts.items() if subject not in expected),
                duplicateDecisions=sum(n-1 for n in counts.values()), wrongClassifications=wrong,
                incorrectNonPosition=sum(x['expected'] == 'position' and x['actual'] in
                                         ('word_mention', 'other_entity', 'no_position') for x in wrong),
                unexpectedPosition=sum(x['actual'] == 'position' for x in wrong),
                unexpectedUnresolved=sum(x['actual'] == 'unresolved' for x in wrong),
                matched=(len(decisions) == len(raw) == len(expected) and set(counts) == set(expected)
                         and all(n == 1 for n in counts.values()) and not wrong))


def dependency_score(response, case, entities):
    # Called only after structural validation. Scores explicit choices, never
    # fills missing premises or promotes a withheld proposal to strict scoring.
    refs = frame.extraction.previous.entity_refs(case, entities)
    table = frame.extraction.segments(case, entities)
    remaining = list(case['selectionExpectations'])
    result = dict(comparisonScope='content_matched_positions_only', matchedPositions=0,
                  expectedConditionUnits=0, incorrectNonPremiseUnits=0, missingConditionUnits=0,
                  extraConditionUnits=0, unresolvedConditionUnits=0, unresolvedUnits=0)
    for decision in response[frame.VERSION]:
        for item in decision['items']:
            p = dict(subject=refs[decision['subject']]['entityIds'][0],
                     object=refs[item['boundary']]['entityIds'][0], **item['position'])
            found = next((n for n, gold in enumerate(remaining)
                          if all(gold['position'][k] == value for k, value in p.items())), None)
            if found is None:
                continue
            gold = remaining.pop(found)
            expected = {table[s]['unitId'] for premise in gold['conditionPremises'] for s in premise}
            selected = {u for condition in item['conditions'] for u in condition['units']}
            unresolved = set(item['unresolvedUnits'])
            result['matchedPositions'] += 1
            result['expectedConditionUnits'] += len(expected)
            result['incorrectNonPremiseUnits'] += len(expected & set(item['nonPremiseUnits']))
            result['missingConditionUnits'] += len(expected-selected)
            result['extraConditionUnits'] += len(selected-expected)
            result['unresolvedConditionUnits'] += len(expected & unresolved)
            result['unresolvedUnits'] += len(unresolved)
    return result


def assess_content(content, case, entities):
    try:
        response = f.parse_json_content(content)
        result = frame.assess(response, case, entities)
        result['attributionScore'] = attribution_score(response, case)
        if 'coverage' in result:
            result['dependencyScore'] = dependency_score(response, case, entities)
        if 'decodedProposal' in result:
            proposal = result['decodedProposal']
            result['positionMetrics'] = previous.position_metrics(proposal, case, entities)
            result['reviewView'] = previous.review.build(proposal, case, entities)
        return result
    except (ValueError, f.LlmError) as error:
        return dict(status='invalid_response', error=str(error), semanticStatus='unverified',
                    reviewRequired=True, productionEnablement=False)


def metrics(rows):
    # Reuse frozen position/cost aggregation without changing historical data.
    base = previous.metrics(rows)
    decisions = [r['attributionScore'] for r in rows if r.get('attributionScore', {}).get('available')]
    dependencies = [r['dependencyScore'] for r in rows if 'dependencyScore' in r]
    return dict(positionScoredCases=base['positionScoredCases'], positions=base['positions'], costs=base['costs'],
                attributionScoredCases=len(decisions),
                attribution={k: sum(x[k] for x in decisions) for k in
                             ('expectedSubjects', 'missingDecisions', 'extraDecisions', 'duplicateDecisions',
                              'incorrectNonPosition', 'unexpectedPosition', 'unexpectedUnresolved')},
                wrongClassifications=sum(len(x['wrongClassifications']) for x in decisions),
                dependencyScoredCases=len(dependencies),
                dependencies={k: sum(x[k] for x in dependencies) for k in
                              ('matchedPositions', 'expectedConditionUnits', 'incorrectNonPremiseUnits',
                               'missingConditionUnits', 'extraConditionUnits', 'unresolvedConditionUnits', 'unresolvedUnits')},
                dependencyPendingCases=sum(r['status'] == 'dependency_pending' for r in rows))


def run(output=OUTPUT):
    fixture = load_cases()
    for c in fixture['cases']:
        messages(c, fixture['scenes'][c['sceneKey']])
    config = f.writer_config_from_env()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')):
        raise ValueError('模型配置无效')
    prior = json.loads(replay.OUTPUT.read_text())
    hashes = dict(prior['inputHashes'])
    for path in INPUT_FILES:
        hashes[str(path.relative_to(f.ROOT))] = f.sha(path)
    evidence = dict(prior['priorEvidenceHashes'])
    for path in EVIDENCE_FILES:
        evidence[str(path.relative_to(f.ROOT))] = f.sha(path)
    report = dict(schemaVersion='context-qualifier-frame-eval/0.1', status='incomplete',
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
    prior = json.loads(replay.OUTPUT.read_text())
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
    if (report['schemaVersion'] != 'context-qualifier-frame-eval/0.1'
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
    return dict(schemaVersion='context-qualifier-frame-eval-audit/0.1', sourceArtifact=str(name),
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
