"""Single identity selection: entities derive only from selected source references."""
import copy

from test_support import context_qualifier_entity_refs as refs_v1

f = refs_v1.f
VERSION = 'qualifier-entity-refs/0.2'
segments = refs_v1.segments
entity_refs = refs_v1.entity_refs


def model_input(case, entities):
    source = refs_v1.model_input(case, entities)
    return dict(sourceBlocks=source['sourceBlocks'], entityRefs={
        rid: dict(kind=ref['kinds'][0] if len(ref['kinds']) == 1 else None,
                  ambiguous=len(ref['entityIds']) != 1,
                  **{key: ref[key] for key in ('segmentId', 'quote', 'occurrence')})
        for rid, ref in source['entityRefs'].items()})


def decode(response, case, entities):
    if not isinstance(response, dict) or set(response) != {VERSION} or not isinstance(response[VERSION], list):
        raise ValueError('顶层必须且仅包含 '+VERSION+' 数组；不补版本或迁移旧响应')
    # Validate selected references first; this is a new wire contract, not repair
    # of a saved v1 response. Identity is represented once, by the selected refs.
    decoded = refs_v1.decode({refs_v1.VERSION: response[VERSION]}, case, entities)
    refs = entity_refs(case, entities)
    for index, (original, item) in enumerate(zip(response[VERSION], decoded['items'])):
        position = original['position']
        if position is None:
            continue
        if not isinstance(position, dict) or set(position) != {'value', 'polarity'}:
            raise ValueError(f'items[{index}].position 只允许 value/polarity，不接收重复身份、条件或常量字段')
        if original['subject'] is None or original['boundary'] is None:
            raise ValueError(f'items[{index}] 未决引用不能导出完整位置解释')
        item['position'] = dict(subject=refs[original['subject']]['entityIds'][0],
                                relation='boundary_side', object=refs[original['boundary']]['entityIds'][0],
                                value=position['value'], polarity=position['polarity'],
                                time='snapshot', condition=None)
    return decoded


def _evaluate(response, case, entities, scoring):
    try:
        decoded = decode(response, case, entities)
        strict = refs_v1.strict
        result = (strict.assess if scoring else strict.inspect)(decoded, case, entities)
        result['decodedResponse'] = decoded
    except (ValueError, KeyError, TypeError) as error:
        result = dict(status='invalid_response', error=str(error), semanticStatus='unverified',
                      reviewRequired=True, productionEnablement=False)
        if isinstance(response, dict) and isinstance(response.get(VERSION), list):
            diagnostic = refs_v1.strict.scoped.inspect(
                dict(schemaVersion=refs_v1.strict.scoped.VERSION, items=response[VERSION]), case, entities)
            for key in ('fieldDiagnostics', 'scopeDiagnostics'):
                result[key] = diagnostic[key]
    result['proposedResponse'] = copy.deepcopy(response)
    return result


def inspect(response, case, entities):
    return _evaluate(response, case, entities, False)


def assess(response, case, entities):
    return _evaluate(response, case, entities, True)
