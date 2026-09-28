"""Offline agreement with frozen hand-authored references, never semantic authority."""
from collections import Counter
import copy
import hashlib
import json

from test_support import context_assertion_scope as scope


def _key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _candidate(subject, item):
    normalized = copy.deepcopy(item)
    for field in ('conditions', 'modifiers'):
        normalized[field].sort(key=_key)
    return dict(subject=subject, **normalized)


def _projections(response):
    result = {k: Counter() for k in ('decisions', 'subjects', 'positionBindings',
              'originBindings', 'qualifierBindings', 'rangeBindings', 'fullBindings')}
    for decision in response[scope.VERSION]:
        subject = decision['subject']
        result['decisions'][_key({k: v for k, v in decision.items() if k != 'items'})] += 1
        for item in decision.get('items', []):
            position = [subject, item['boundary'], item['position']]
            origin = position+[item['origin']['speaker']]
            candidate = _candidate(subject, item)
            result['subjects'][_key(subject)] += 1
            result['positionBindings'][_key(position)] += 1
            result['originBindings'][_key(origin)] += 1
            result['qualifierBindings'][_key(origin+[candidate['conditions'], candidate['modifiers'],
                candidate['nonPremiseUnits'], candidate['unresolvedUnits']])] += 1
            result['rangeBindings'][_key(origin+[item['core'], item['origin']['scope']])] += 1
            result['fullBindings'][_key(candidate)] += 1
    return result


def _difference(expected, actual):
    def entries(counter):
        return [dict(value=json.loads(k), count=n) for k, n in sorted(counter.items())]
    missing, extra = expected-actual, actual-expected
    return dict(expected=sum(expected.values()), actual=sum(actual.values()),
                matched=sum((expected & actual).values()), missing=sum(missing.values()),
                extra=sum(extra.values()), missingEntries=entries(missing), extraEntries=entries(extra))


def assess(response, case, entities, reference):
    """Compare only after structural validation; no automatic old-wire conversion."""
    if (reference['caseId'] != case['id']
            or reference['draftSha256'] != hashlib.sha256(case['draft'].encode()).hexdigest()
            or reference['legacyAttributions'] != case['frameAttributions']):
        raise ValueError('评分参考未绑定当前原文和旧标签')
    expected = reference['control']
    expected_check = scope.inspect(expected, case, entities)
    if expected_check['status'] == 'invalid_response':
        raise ValueError('评分参考结构无效')
    checked = scope.inspect(response, case, entities)
    result = dict(proposedResponse=copy.deepcopy(response), structuralStatus=checked['status'],
                  status='invalid_response', referenceAgreement=None, dimensions=None,
                  semanticStatus='unverified', productionEnablement=False, acceptance=False,
                  reviewRequired=True, readyForModelEvaluation=False,
                  referenceKind='frozen_hand_authored_control', legacySubtypeComparable=False,
                  pendingUnits=checked.get('pendingUnits', []),
                  pendingDependencies=checked.get('pendingDependencies', []))
    valid = checked['status'] != 'invalid_response'
    expected_decisions = {d['subject']: d for d in expected[scope.VERSION]}
    actual_decisions = {d['subject']: d for d in response[scope.VERSION]} if valid else {}
    result['legacyDiagnostics'] = [dict(subject=s, expectedLegacySubtype=kind,
        referenceResolution=expected_decisions[s]['resolution'],
        actualResolution=actual_decisions[s]['resolution'] if valid else None,
        predictedLegacySubtype=None, subtypeScored=False)
        for s, kind in reference['legacyAttributions'].items()]
    if not valid:
        result['error'] = checked['error']
        return result
    if not expected_decisions:
        # Empty task equality must not become a successful pronoun-resolution score.
        result.update(status='out_of_scope', exclusionReason='no_registered_subject_boundary_task')
        return result
    wanted, actual = _projections(expected), _projections(response)
    dimensions = {name: _difference(wanted[name], actual[name]) for name in wanted}
    # Excess narrator assertions on an otherwise identical position are a
    # diagnostic lower bound, not a causal pairing of candidate lists.
    narrator = lambda d: Counter({_key(json.loads(k)[:-1]): n for k, n in d.items()
                                  if json.loads(k)[-1] is None})
    reported = Counter()
    for k, n in wanted['originBindings'].items():
        if json.loads(k)[-1] is not None:
            reported[_key(json.loads(k)[:-1])] += n
    excess_direct = narrator(actual['originBindings'])-narrator(wanted['originBindings'])
    diagnostics = dict(
        unexpectedAbstentions=sum(d['resolution'] == 'unresolved' and
            expected_decisions[s]['resolution'] != 'unresolved' for s, d in actual_decisions.items()),
        falseAbsences=sum(d['resolution'] == 'absent' and
            expected_decisions[s]['resolution'] == 'positions' for s, d in actual_decisions.items()),
        unexpectedPositionTasks=sum(d['resolution'] == 'positions' and
            expected_decisions[s]['resolution'] == 'absent' for s, d in actual_decisions.items()),
        reportedAsNarrator=sum((excess_direct & reported).values()))
    equal = all(not d['missing'] and not d['extra'] for d in dimensions.values())
    result.update(status='reference_match' if equal else 'reference_mismatch',
                  referenceAgreement=equal, dimensions=dimensions, diagnostics=diagnostics)
    return result
