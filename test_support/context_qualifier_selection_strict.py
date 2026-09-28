"""Separate position content, qualifier spans and unconfirmed disposition."""
import copy

from test_support import context_qualifier_selection_scoped as scoped

f = scoped.f
VERSION = 'qualifier-selection/0.3'
segments = scoped.segments
model_input = scoped.model_input


def inspect(response, case, entities):
    if not isinstance(response, dict) or response.get('schemaVersion') != VERSION:
        return dict(status='invalid_response', error='须返回 '+VERSION,
                    proposedResponse=copy.deepcopy(response), semanticStatus='unverified',
                    reviewRequired=True, productionEnablement=False)
    decoded = copy.deepcopy(response)
    decoded['schemaVersion'] = scoped.VERSION
    result = scoped.inspect(decoded, case, entities)
    result['proposedResponse'] = copy.deepcopy(response)
    if result['status'] == 'invalid_response':
        return result
    table = segments(case, entities)
    for index, item in enumerate(response['items']):
        def issue(code):
            result['issues'].append(dict(itemIndex=index, code=code, kind=None))
        if item['position'] is None:
            # An unresolved interpretation cannot count as an extracted relation.
            issue('position_interpretation_missing')
        for limit in item['limitations']:
            if limit['kind'] != 'condition':
                continue
            premise = limit['premise']
            if set(premise) & set(item['core']):
                issue('condition_premise_overlaps_core')
            # A selected premise must not omit another segment of its own clause.
            # This is a conservative envelope check, not a condition classifier.
            units = {table[sid]['unitId'] for sid in premise}
            if any(sid not in premise for sid, part in table.items() if part['unitId'] in units):
                issue('condition_premise_partial_unit')
    result['status'] = 'evidence_issues' if result['issues'] else 'structurally_valid'
    return result


def assess(response, case, entities):
    result = inspect(response, case, entities)
    if result['status'] == 'invalid_response':
        return result
    result['extractionScore'] = f.assess(result['projection'], case, entities)
    remaining = list(enumerate(case['selectionExpectations']))
    comparisons = []
    for index, item in enumerate(response['items']):
        found = next(((n, expected) for n, expected in remaining
                      if any(item['core'] == span['core'] for span in expected['spans'])), None)
        if found is None:
            comparisons.append(dict(itemIndex=index, position=False, scope=False))
            continue
        remaining.remove(found)
        _, expected = found
        scope = dict(core=item['core'], qualified=item['qualified']) in expected['spans']
        premises = [x['premise'] for x in item['limitations'] if x['kind'] == 'condition']
        comparisons.append(dict(itemIndex=index, position=item['position'] == expected['position'],
                                scope=scope and sorted(premises) == sorted(expected['conditionPremises'])))
    complete = not remaining and len(comparisons) == len(case['selectionExpectations'])
    result['contentScore'] = dict(complete=complete, items=comparisons,
                                 missingExpectedItems=[n for n, _ in remaining])
    result['status'] = ('matched' if result['extractionScore']['status'] == 'matched'
                        and not result['issues'] and complete
                        and all(x['position'] and x['scope'] for x in comparisons) else 'mismatched')
    return result
