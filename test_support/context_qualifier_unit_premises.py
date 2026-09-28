"""Select whole premise units and derive coverage from explicit selections only."""
import copy

from test_support import context_qualifier_entity_refs_v2 as previous
from test_support.context_qualifier_evidence import resolve

f = previous.f
VERSION = 'qualifier-unit-premises/0.1'
segments = previous.segments


def model_input(case, entities):
    refs = previous.model_input(case, entities)['entityRefs']
    table = segments(case, entities)
    units = f.indexed_input(case)['units']
    source, cursor = [], 0
    for uid, unit in units.items():
        if cursor < unit['start']:
            source.append(case['draft'][cursor:unit['start']])
        source.append(dict(unitId=uid, segments={sid: part['quote'] for sid, part in table.items()
                                               if part['unitId'] == uid}))
        cursor = unit['end']
    if cursor < len(case['draft']):
        source.append(case['draft'][cursor:])
    return dict(source=source, entityRefs=refs)


def _selection(ids, order, *, empty=False):
    if (not isinstance(ids, list) or (not ids and not empty)
            or any(not isinstance(i, str) or i not in order for i in ids)):
        raise ValueError('未知或缺失的原文选择')
    if ids and ids != order[order.index(ids[0]):order.index(ids[-1])+1]:
        raise ValueError('选择须连续、有序且不重复；不自动填补空隙')
    return ids


def decode(response, case, entities):
    if not isinstance(response, dict) or set(response) != {VERSION} or not isinstance(response[VERSION], list):
        raise ValueError('顶层必须且仅包含 '+VERSION+' 数组；不迁移旧响应')
    table = segments(case, entities)
    unit_order = list(f.indexed_input(case)['units'])
    order = list(table)
    items = []
    for original in response[VERSION]:
        if not isinstance(original, dict) or set(original) != {'position', 'reason', 'subject', 'boundary', 'core', 'limitations'}:
            raise ValueError('命题须包含六个指定字段；不接收重复的 qualified')
        core = _selection(original['core'], order)
        if not isinstance(original['limitations'], list):
            raise ValueError('限定列表无效')
        selected, limits = set(core), []
        for limit in original['limitations']:
            if not isinstance(limit, dict) or set(limit) != {'kind', 'cue', 'premiseUnits'}:
                raise ValueError('限定须包含 kind/cue/premiseUnits')
            kind = limit['kind']
            if not isinstance(kind, str) or kind not in f.LIMITATIONS:
                raise ValueError('限定类型无效')
            units = _selection(limit['premiseUnits'], unit_order, empty=kind != 'condition')
            if kind != 'condition' and units:
                raise ValueError('只有条件限定可选择前提单元')
            premise = [sid for sid, part in table.items() if part['unitId'] in units]
            if set(premise) & set(core):
                raise ValueError('条件前提不能包含结论 core')
            cue = limit['cue']
            if not isinstance(cue, dict) or set(cue) != {'segmentId', 'quote', 'occurrence'}:
                raise ValueError('线索须精确选择单个片段中的引文')
            # Resolve against the stated segment. Never search a neighbour,
            # accept a cross-segment quote, or replace the model's cue.
            resolve(
                dict(unitId=cue['segmentId'], quote=cue['quote'], occurrence=cue['occurrence']), table, order)
            if kind == 'condition' and cue['segmentId'] not in premise:
                raise ValueError('条件线索不在所选完整前提中')
            selected.update(premise)
            selected.add(cue['segmentId'])
            limits.append(dict(kind=kind, cue=copy.deepcopy(cue), premise=premise))
        qualified = [sid for sid in order if sid in selected]
        _selection(qualified, order)
        items.append(dict({k: copy.deepcopy(original[k]) for k in ('position', 'reason', 'subject', 'boundary', 'core')},
                          qualified=qualified, limitations=limits))
    return {previous.VERSION: items}


def _evaluate(response, case, entities, scoring):
    try:
        decoded = decode(response, case, entities)
        result = (previous.assess if scoring else previous.inspect)(decoded, case, entities)
        result['decodedProposal'] = decoded
    except (ValueError, KeyError, TypeError) as error:
        result = dict(status='invalid_response', error=str(error), semanticStatus='unverified',
                      reviewRequired=True, productionEnablement=False)
    result['proposedResponse'] = copy.deepcopy(response)
    return result


def inspect(response, case, entities):
    return _evaluate(response, case, entities, False)


def assess(response, case, entities):
    return _evaluate(response, case, entities, True)
