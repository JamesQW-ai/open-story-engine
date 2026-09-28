"""Reference-scoring controls and deliberate faults; zero model requests."""
import argparse
from collections import Counter
import copy
import json
from pathlib import Path

from test_support import context_assertion_scope_replay as previous
from test_support import context_assertion_scoring as scoring

f = previous.f
OUTPUT = previous.OUTPUT.with_name('context-assertion-scoring-replay-2026-09-27.json')
AUDIT = previous.AUDIT.with_name('context-assertion-scoring-replay-audit-2026-09-27.json')


def probes(fixture, controls):
    rows = []
    def sample(cid):
        c = next(c for c in fixture['cases'] if c['id'] == cid)
        ref = next(r for r in controls if r['caseId'] == cid)
        return c, fixture['scenes'][c['sceneKey']], copy.deepcopy(ref['control'])
    def add(name, c, response, status='reference_mismatch', **signals):
        rows.append(dict(probeId=name, caseId=c['id'], response=response,
                         expectedStatus=status, expectedSignals=signals))
    for cid in ('frame:self_report', 'frame:other_report'):
        c, entities, response = sample(cid)
        d = next(d for d in response[scoring.scope.VERSION] if d['resolution'] == 'positions')
        d['items'][0]['origin']['speaker'] = None
        add('promote:'+cid, c, response, reportedAsNarrator=1)
    c, entities, response = sample('frame:other_report')
    d = response[scoring.scope.VERSION][1]
    d['items'][0]['origin']['speaker'] = d['subject']
    add('wrong_speaker', c, response, **{'originBindings.missing': 1})
    c, entities, response = sample('frame:mixed_ownership')
    a, b = response[scoring.scope.VERSION]
    item = a['items'][0]
    table = list(scoring.scope.frame.extraction.segments(c, entities))
    item['core'] = table[:]
    item['origin']['scope'] = table[:]
    a.update(resolution='absent', basis=table[:])
    del a['items']
    b.update(resolution='positions', items=[item])
    del b['basis']
    add('ambiguous_multi_person_core', c, response, 'invalid_response')
    c, entities, response = sample('control:mixed_people')
    data = scoring.scope.model_input(c, entities)
    a, b = response[scoring.scope.VERSION]
    item = b['items'][0]
    basis = a['basis'][:]
    item['core'] = [data['entityRefs'][a['subject']]['segmentId']]
    item['origin']['scope'] = item['core'][:]
    item['boundary'] = data['tasks'][a['subject']]['boundaries'][0]
    a.update(resolution='positions', items=[item])
    del a['basis']
    b.update(resolution='absent', basis=basis)
    del b['items']
    add('wrong_subject', c, response, **{'subjects.missing': 1, 'subjects.extra': 1})
    c, entities, response = sample('control:location_mention')
    data = scoring.scope.model_input(c, entities)
    d = response[scoring.scope.VERSION][0]
    table = d.pop('basis')
    d.update(resolution='positions', items=[dict(boundary=data['tasks'][d['subject']]['boundaries'][0],
        position=dict(value='inside', polarity='positive'), core=table, conditions=[], modifiers=[],
        nonPremiseUnits=[], unresolvedUnits=[], origin=dict(speaker=None, scope=table))])
    add('invent_position', c, response, unexpectedPositionTasks=1, **{'positionBindings.extra': 1})
    c, entities, response = sample('hall:source_position')
    d = response[scoring.scope.VERSION][0]
    d.update(resolution='absent', basis=d.pop('items')[0]['core'])
    add('erase_position', c, response, falseAbsences=1, **{'positionBindings.missing': 1})
    c, entities, response = sample('control:location_mention')
    response[scoring.scope.VERSION][0]['resolution'] = 'unresolved'
    add('unnecessary_abstention', c, response, unexpectedAbstentions=1)
    c, entities, response = sample('gate:both_paraphrase')
    item = response[scoring.scope.VERSION][0]['items'][0]
    item['nonPremiseUnits'] = item['conditions'][0]['units'][:]
    item['conditions'] = []
    add('drop_condition', c, response, **{'qualifierBindings.missing': 1})
    c, entities, response = sample('hall:source_position')
    position = response[scoring.scope.VERSION][0]['items'][0]['position']
    position['value'] = 'inside' if position['value'] == 'outside' else 'outside'
    add('wrong_position', c, response, **{'positionBindings.missing': 1, 'positionBindings.extra': 1})
    c, entities, response = sample('two_people:first_operator')
    old = next(r for r in json.loads(previous.previous.OUTPUT.read_text())['cases'] if r['caseId'] == c['id'])
    bad = json.loads(old['calls'][0]['content'])[scoring.scope.frame.VERSION]
    for d, old_d in zip(response[scoring.scope.VERSION], bad):
        for item, old_item in zip(d['items'], old_d['items']):
            item['nonPremiseUnits'] = old_item['nonPremiseUnits'][:]
    add('original_invalid_unit_ids', c, response, 'invalid_response')
    c, entities, response = sample('hall:source_position')
    response[scoring.scope.VERSION][0]['items'] *= 2
    add('duplicate_candidate', c, response, **{'positionBindings.extra': 1})
    c, entities, response = sample('hall:source_position')
    response[scoring.scope.VERSION] = []
    add('missing_task', c, response, 'invalid_response')
    return rows


def evaluate_probe(probe, fixture, controls):
    c = next(c for c in fixture['cases'] if c['id'] == probe['caseId'])
    ref = next(r for r in controls if r['caseId'] == c['id'])
    result = scoring.assess(probe['response'], c, fixture['scenes'][c['sceneKey']], ref)
    if result['status'] != probe['expectedStatus']:
        raise ValueError('评分反例未按预期检出：'+probe['probeId'])
    for key, expected in probe['expectedSignals'].items():
        if '.' in key:
            dimension, field = key.split('.')
            actual = result['dimensions'][dimension][field]
        else:
            actual = result['diagnostics'][key]
        if actual != expected:
            raise ValueError('评分反例诊断不一致：'+probe['probeId']+': '+key)
    return dict(probe, assessment=result)


def build():
    if previous.audit() != json.loads(previous.AUDIT.read_text()):
        raise ValueError('前序离线证据变化')
    fixture, controls = previous.load_inputs()
    rows = [dict(caseId=c['id'], assessment=scoring.assess(row['control'], c,
                fixture['scenes'][c['sceneKey']], row)) for c, row in zip(fixture['cases'], controls)]
    faults = [evaluate_probe(p, fixture, controls) for p in probes(fixture, controls)]
    prior = json.loads(previous.OUTPUT.read_text())
    inputs, evidence = dict(prior['inputHashes']), dict(prior['priorEvidenceHashes'])
    for p in (Path(__file__), Path(scoring.__file__)):
        inputs[str(p.relative_to(f.ROOT))] = f.sha(p)
    for p in (previous.OUTPUT, previous.AUDIT):
        evidence[str(p.relative_to(f.ROOT))] = f.sha(p)
    groups = {}
    for row in rows:
        for d in row['assessment']['legacyDiagnostics']:
            groups[d['expectedLegacySubtype']] = groups.get(d['expectedLegacySubtype'], 0)+1
    return dict(schemaVersion='assertion-scoring-replay/0.1', inputHashes=inputs,
        priorEvidenceHashes=evidence, cases=rows, probes=faults,
        summary=dict(referenceControls=dict(Counter(r['assessment']['status'] for r in rows)),
            faultControls=dict(Counter(r['assessment']['status'] for r in faults)),
            legacyReferenceGroups=groups, legacySubtypeScored=0),
        originalSummary=prior['originalSummary'], originalFailures=prior['originalFailures'],
        source='hand_authored_reference_and_fault_controls_not_model_results',
        limitations=['single_reference_exact_ranges_not_semantic_equivalence',
            'legacy_subtypes_unobservable', 'scope_and_identifier_design_pending',
            'model_prompt_and_budget_unverified'],
        semanticStatus='unverified', acceptance=False, productionEnablement=False,
        readyForModelEvaluation=False, newModelCalls=0, formalSessionWrites=0,
        reviewRecordsWritten=0, jevCalls=0)


def audit(source=OUTPUT):
    report = json.loads(source.read_text())
    if report != build():
        raise ValueError('离线评分回放不可复现')
    return dict(schemaVersion='assertion-scoring-replay-audit/0.1', sourceArtifact=str(source),
        sourceSha256=f.sha(source), allRowsReproduced=True, summary=report['summary'],
        newModelCalls=0, productionEnablement=False, acceptance=False, readyForModelEvaluation=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    args = parser.parse_args()
    result = audit() if args.audit else build()
    f.save_checkpoint(AUDIT if args.audit else OUTPUT, result, create=True)
    print(json.dumps(result['summary']))
