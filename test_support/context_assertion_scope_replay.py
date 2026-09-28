"""Offline expressivity review only; no model calls or production promotion."""
import argparse
import json
from pathlib import Path

from test_support import context_assertion_scope as scope
from test_support import context_qualifier_attribution_eval as previous

f = previous.f
FIXTURE = f.ROOT/'test_support/fixtures/context-assertion-scope-controls-2026-09-27.json'
EXPECTED_FIXTURE_SHA256 = '38a843f1de68f8b3aedcc22a118bae10a5a66922b71e021ba1469aef2b5e608b'
OUTPUT = previous.OUTPUT.with_name('context-assertion-scope-replay-2026-09-27.json')
AUDIT = previous.AUDIT.with_name('context-assertion-scope-replay-audit-2026-09-27.json')


def load_inputs():
    if f.sha(FIXTURE) != EXPECTED_FIXTURE_SHA256:
        raise ValueError('手写控制夹具哈希变化')
    fixture = previous.previous.replay.load_cases()
    controls = json.loads(FIXTURE.read_text())['cases']
    if len(controls) != 44 or len(fixture['cases']) != 44:
        raise ValueError('原诊断集数量变化')
    for c, row in zip(fixture['cases'], controls):
        if (row['caseId'] != c['id'] or row['draftSha256'] != c['draftSha256']
                or row['legacyAttributions'] != c['frameAttributions']):
            raise ValueError('手写控制与原诊断标签不一致')
    return fixture, controls


def _chars(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def _legacy_control(row):
    # Gold-assisted offline size baseline, never model input or automatic migration.
    return {scope.frame.VERSION: [dict(subject=d['subject'],
                attribution=row['legacyAttributions'][d['subject']],
                items=[{k:v for k,v in item.items() if k != 'origin'} for item in d['items']]
                if row['legacyAttributions'][d['subject']] == 'position' else [])
            for d in row['control'][scope.VERSION]]}


def build():
    if previous.audit() != json.loads(previous.AUDIT.read_text()):
        raise ValueError('前序实测审计变化')
    fixture, controls = load_inputs()
    prior = json.loads(previous.OUTPUT.read_text())
    rows = []
    for c, control, old in zip(fixture['cases'], controls, prior['cases']):
        entities = fixture['scenes'][c['sceneKey']]
        data = scope.model_input(c, entities)
        if data != json.loads(old['calls'][0]['messages'][1]['content']):
            raise ValueError('原文或任务输入变化')
        checked = scope.inspect(control['control'], c, entities)
        if checked['status'] == 'invalid_response':
            raise ValueError('手写契约控制不可表达：'+c['id']+': '+checked['error'])
        rows.append(dict(caseId=c['id'], originalStatus=old['status'],
                         structuralStatus=checked['status'],
                         narratorCandidates=len(checked['narratorCandidates']),
                         reportedCandidates=len(checked['reportedCandidates']),
                         absenceDecisions=len(checked['absenceDecisions']),
                         unresolvedDecisions=len(checked['unresolvedDecisions']),
                         userChars=_chars(data), controlResponseChars=_chars(control['control']),
                         legacyControlResponseChars=_chars(_legacy_control(control)),
                         legacyAttributions=control['legacyAttributions'],
                         legacyAttributionComparable=False))
    inputs = dict(prior['inputHashes'])
    for p in (FIXTURE, Path(__file__), Path(scope.__file__)):
        inputs[str(p.relative_to(f.ROOT))] = f.sha(p)
    evidence = dict(prior['priorEvidenceHashes'])
    for p in (previous.OUTPUT, previous.AUDIT):
        evidence[str(p.relative_to(f.ROOT))] = f.sha(p)
    return dict(schemaVersion='assertion-scope-replay/0.1', inputHashes=inputs,
                priorEvidenceHashes=evidence, cases=rows, originalSummary=prior['summary'],
                originalFailures=[dict(caseId=x['caseId'], status=x['status']) for x in prior['cases']
                                  if x['status'] not in ('matched', 'scope_pending')],
                summary=dict(cases=len(rows),
                    structurallyValid=sum(x['structuralStatus']=='structurally_valid' for x in rows),
                    scopePending=sum(x['structuralStatus']=='scope_pending' for x in rows),
                    **{key:sum(x[key] for x in rows) for key in ('narratorCandidates','reportedCandidates',
                       'absenceDecisions','unresolvedDecisions','userChars','controlResponseChars','legacyControlResponseChars')}),
                source='hand_authored_controls_not_model_results', semanticStatus='unverified',
                legacyAttributionComparable=False, readyForModelEvaluation=False,
                openIssues=['legacy_subtype_comparison', 'origin_semantics_unverified',
                            'unit_segment_namespaces_unchanged', 'scope_granularity_unverified',
                            'model_prompt_and_token_budget_unverified'],
                newModelCalls=0, acceptance=False, productionEnablement=False,
                formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0)


def audit(source=OUTPUT):
    report = json.loads(source.read_text())
    if report != build():
        raise ValueError('离线设计回放不可复现')
    return dict(schemaVersion='assertion-scope-replay-audit/0.1', sourceArtifact=str(source),
                sourceSha256=f.sha(source), allRowsReproduced=True, summary=report['summary'],
                newModelCalls=0, acceptance=False, productionEnablement=False,
                readyForModelEvaluation=False)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit',action='store_true')
    args=parser.parse_args()
    report=audit() if args.audit else build()
    f.save_checkpoint(AUDIT if args.audit else OUTPUT,report,create=True)
    print(json.dumps(report['summary']))
