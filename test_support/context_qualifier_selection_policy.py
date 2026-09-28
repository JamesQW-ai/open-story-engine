"""Derive review disposition from proposed limitations, never semantic approval."""
import copy

from test_support import context_qualifier_selection as selection

f = selection.f
VERSION = 'qualifier-selection/0.2'
segments = selection.segments
model_input = selection.model_input


def inspect(response, case, entities):
    try:
        if not isinstance(response, dict) or set(response) != {'schemaVersion', 'items'} or response['schemaVersion'] != VERSION or not isinstance(response['items'], list):
            raise ValueError('须返回 qualifier-selection/0.2 与 items')
        decoded = dict(schemaVersion=selection.VERSION, items=[])
        for item in response['items']:
            if not isinstance(item, dict) or set(item) != {'position', 'reason', 'subject', 'boundary', 'core', 'qualified', 'limitations'}:
                raise ValueError('模型不得提供 status/normal；只提供位置解释与限定依据')
            limits, position = item['limitations'], item['position']
            if not isinstance(limits, list) or (position is None and not limits):
                raise ValueError('无位置解释且无限定，不能形成候选')
            if position is not None:
                # Validate even an uncommitted interpretation before suppressing
                # its use as an unconditional normal relation.
                checked = f.assess_item(dict(status='candidate', normal=position, reason=item['reason'], limitations=[]),
                                        dict(target={'quote': case['draft']}, expectedStatus='candidate', expectedNormal=None, expectedLimitations=[]), entities)
                if checked['status'] == 'invalid_response':
                    raise ValueError(checked['error'])
            decoded['items'].append(dict({k: copy.deepcopy(v) for k, v in item.items() if k != 'position'},
                                         status='needs_review' if limits else 'candidate',
                                         normal=None if limits else copy.deepcopy(position)))
        result = selection.inspect(decoded, case, entities)
        result['decodedSelection'] = decoded
        if result['status'] != 'invalid_response':
            for index, (original, item) in enumerate(zip(response['items'], decoded['items'])):
                if original['position'] is not None and item['limitations']:
                    probe = dict(copy.deepcopy(item), status='candidate', normal=original['position'], limitations=[])
                    checked = selection.inspect(dict(schemaVersion=selection.VERSION, items=[probe]), case, entities)
                    if checked['status'] == 'invalid_response' or any(
                            x['code'] in {'candidate_entity_anchor_unverified', 'candidate_entity_outside_position'}
                            for x in checked['issues']):
                        result['issues'].append(dict(itemIndex=index, code='position_entity_anchor_mismatch', kind=None))
            if result['issues']:
                result['status'] = 'evidence_issues'
    except (ValueError, KeyError, TypeError) as error:
        result = dict(status='invalid_response', error=str(error), semanticStatus='unverified',
                      reviewRequired=True, productionEnablement=False)
    result['proposedResponse'] = copy.deepcopy(response)
    return result


def assess(response, case, entities):
    result = inspect(response, case, entities)
    if result['status'] != 'invalid_response':
        result['extractionScore'] = f.assess(result['projection'], case, entities)
        result['status'] = 'matched' if result['extractionScore']['status'] == 'matched' and not result['issues'] else 'mismatched'
    return result
