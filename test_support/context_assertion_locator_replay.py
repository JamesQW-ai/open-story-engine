"""Bounded offline comparison of source locators, without changing candidate contracts."""
import argparse
import hashlib
import json
from pathlib import Path

from test_support import context_assertion_locator as locator
from test_support import context_assertion_scoring_replay as previous

f = previous.f
FIXTURE = f.ROOT/'test_support/fixtures/context-assertion-locator-cases-2026-09-27.json'
FIXTURE_SHA256 = '04e012c82051d48cb1e21d19cda4b38f8f03643a71d6cc8414e0d0e8abc55690'
PREVIOUS_SHA256 = 'c5e852f7b3ba562c4188c8a0527a0ecc4addf94a2129cbbdbaa010372d8e72a4'
PREVIOUS_AUDIT_SHA256 = '42242269b2139bdc67d3d447802c1f372a204d8da180c83fcfa63fab70c6ddf0'
OUTPUT = previous.OUTPUT.with_name('context-assertion-locator-replay-2026-09-27.json')
AUDIT = previous.AUDIT.with_name('context-assertion-locator-replay-audit-2026-09-27.json')


def load_inputs():
    if f.sha(FIXTURE) != FIXTURE_SHA256:
        raise ValueError('定位控制夹具哈希变化')
    fixture, controls = previous.previous.load_inputs()
    designs = json.loads(FIXTURE.read_text())['cases']
    for row in designs:
        if hashlib.sha256(row['draft'].encode()).hexdigest() != row['draftSha256']:
            raise ValueError('定位设计原文变化')
        if row['sourceCaseId'] not in {c['id'] for c in fixture['cases']}:
            raise ValueError('定位设计没有绑定原诊断场景')
    return fixture, controls, designs


def _prior():
    # Reuse the frozen successful audit, verifying every bound file; avoid
    # recursively rerunning historical evaluators on each new offline check.
    if f.sha(previous.OUTPUT) != PREVIOUS_SHA256 or f.sha(previous.AUDIT) != PREVIOUS_AUDIT_SHA256:
        raise ValueError('前序评分证据或审计哈希变化')
    report = json.loads(previous.OUTPUT.read_text())
    audit = json.loads(previous.AUDIT.read_text())
    if audit['sourceSha256'] != PREVIOUS_SHA256 or not audit['allRowsReproduced']:
        raise ValueError('前序评分审计未绑定原报告')
    for group in ('inputHashes', 'priorEvidenceHashes'):
        for name, expected in report[group].items():
            if f.sha(f.ROOT/name) != expected:
                raise ValueError('前序绑定文件变化：'+name)
    return report


def _chars(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def locate_design(row):
    table = locator.units(row)
    spans = []
    for selection in row['selections']:
        resolved = locator.resolve(selection['ref'], table, table)
        if (resolved['start'], resolved['end']) != (selection['expectedStart'], selection['expectedEnd']):
            raise ValueError('细粒度定位与手写控制不一致')
        encoded = locator._encode(resolved['unitId'], resolved['start'], resolved['end'], table, 'offset')
        if locator.resolve(encoded, table, table, 'offset') != resolved:
            raise ValueError('两种定位不能还原同一范围')
        spans.append(dict(role=selection['role'], quoteRef=selection['ref'], offsetRef=encoded, resolved=resolved))
    return dict(caseId=row['id'], sourceCaseId=row['sourceCaseId'], source=row['source'],
                draft=row['draft'], selections=spans, semanticStatus='unverified',
                productionEnablement=False)


def build():
    prior = _prior()
    fixture, controls, designs = load_inputs()
    rows = []
    for c, control in zip(fixture['cases'], controls):
        entities = fixture['scenes'][c['sceneKey']]
        row = dict(caseId=c['id'], oldInputChars=_chars(locator.previous.model_input(c, entities)),
                   oldPreviewChars=_chars(control['control']))
        projections = {}
        for encoding in ('quote', 'offset'):
            data = locator.project_input(c, entities, encoding)
            if ''.join(p if isinstance(p, str) else p['text'] for p in data['source']) != c['draft']:
                raise ValueError('投影没有逐字保留原文')
            preview = locator.project_control(control['control'], c, entities, encoding)
            projections[encoding] = locator.evidence_spans(preview, c, encoding)
            row[encoding+'InputChars'] = _chars(data)
            row[encoding+'PreviewChars'] = _chars(preview)
        if projections['quote'] != projections['offset']:
            raise ValueError('旧控制的两种定位映射不一致')
        row['resolvedReferences'] = len(projections['quote'])
        rows.append(row)
    inputs, evidence = dict(prior['inputHashes']), dict(prior['priorEvidenceHashes'])
    for p in (FIXTURE, Path(__file__), Path(locator.__file__)):
        inputs[str(p.relative_to(f.ROOT))] = f.sha(p)
    for p in (previous.OUTPUT, previous.AUDIT):
        evidence[str(p.relative_to(f.ROOT))] = f.sha(p)
    return dict(schemaVersion='assertion-locator-replay/0.1', inputHashes=inputs,
        priorEvidenceHashes=evidence, cases=rows, designs=[locate_design(d) for d in designs],
        summary=dict(cases=len(rows), designCases=len(designs),
            **{key:sum(row[key] for row in rows) for key in rows[0] if key != 'caseId'}),
        priorAuditPolicy='verify_pinned_audit_and_all_bound_hashes_without_rescoring_history',
        originalSummary=prior['originalSummary'], originalFailures=prior['originalFailures'],
        source='hand_authored_locator_controls_not_model_results', semanticStatus='unverified',
        limitations=['locator_only_not_candidate_validator', 'attribution_and_nested_sources_unverified',
                     'accepted_semantic_ranges_not_frozen', 'model_prompt_tokens_and_latency_unmeasured'],
        newModelCalls=0, formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0,
        productionEnablement=False, acceptance=False, readyForModelEvaluation=False)


def audit(source=OUTPUT):
    report = json.loads(source.read_text())
    if report != build():
        raise ValueError('定位比较不可复现')
    return dict(schemaVersion='assertion-locator-replay-audit/0.1', sourceArtifact=str(source),
        sourceSha256=f.sha(source), allRowsReproduced=True, summary=report['summary'],
        newModelCalls=0, productionEnablement=False, acceptance=False, readyForModelEvaluation=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    args = parser.parse_args()
    result = audit() if args.audit else build()
    f.save_checkpoint(AUDIT if args.audit else OUTPUT, result, create=True)
    print(json.dumps(result['summary']))
