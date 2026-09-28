"""Bounded occurrence-level accounting; never infer a fact from co-occurrence."""
import copy
import hashlib
import json

from test_support import context_qualifier_unit_premises as extraction

VERSION = 'qualifier-coverage/0.1'
MAX_UNITS = 64
MAX_SLOTS = 32
MAX_BOUNDARIES_PER_UNIT = 8
MAX_INDEX_CHARS = 16384
MAX_ITEMS = 64


def model_input(case, entities):
    source = extraction.model_input(case, entities)
    units = [x for x in source['source'] if isinstance(x, dict)]
    if len(units) > MAX_UNITS:
        raise ValueError('覆盖单元预算超限，不截断原文')
    binding = json.dumps(source, ensure_ascii=False, sort_keys=True)
    prefix = 'C'+hashlib.sha256(binding.encode()).hexdigest()[:16]
    slots, pending = {}, []
    for unit in units:
        refs = {rid: ref for rid, ref in source['entityRefs'].items()
                if ref['segmentId'] in unit['segments']}
        people = [rid for rid, ref in refs.items()
                  if ref['kind'] == 'scene_person' and not ref['ambiguous']]
        boundaries = [rid for rid, ref in refs.items()
                      if ref['kind'] == 'scene_boundary' and not ref['ambiguous']]
        reasons = []
        if not people:
            reasons.append('no_registered_person')
        if not boundaries:
            reasons.append('no_registered_boundary')
        if any(ref['ambiguous'] or ref['kind'] is None for ref in refs.values()):
            reasons.append('ambiguous_entity_reference')
        if len(boundaries) > MAX_BOUNDARIES_PER_UNIT:
            raise ValueError('单元边界引用预算超限，不截断候选')
        if people and boundaries:
            for person in people:
                if len(slots) >= MAX_SLOTS:
                    raise ValueError('覆盖任务预算超限，不截断候选')
                slots[f'{prefix}-{len(slots)+1}'] = dict(
                    unitId=unit['unitId'], subjectRef=person, boundaryRefs=list(boundaries))
        if reasons:
            pending.append(dict(unitId=unit['unitId'], reasons=reasons))
    index = dict(schemaVersion=VERSION, scope='same_unit_registered_occurrences',
                 slots=slots, pendingUnits=pending)
    if len(json.dumps(index, ensure_ascii=False)) > MAX_INDEX_CHARS:
        raise ValueError('覆盖索引字符预算超限，不截断索引')
    return dict(source, coverageIndex=index)


def decode(response, case, entities):
    data = model_input(case, entities)
    index = data['coverageIndex']
    if (not isinstance(response, dict) or set(response) != {VERSION}
            or not isinstance(response[VERSION], list)):
        raise ValueError('须显式返回 '+VERSION+' 决策数组；不升级旧输出')
    if len(response[VERSION]) > MAX_SLOTS:
        raise ValueError('覆盖决策数量超限')
    decisions = {}
    for decision in response[VERSION]:
        if not isinstance(decision, dict) or set(decision) != {'slotId', 'disposition', 'items'}:
            raise ValueError('覆盖决策字段无效')
        sid = decision['slotId']
        if not isinstance(sid, str) or sid not in index['slots'] or sid in decisions:
            raise ValueError('覆盖任务未知、重复或绑定其他原文')
        disposition = decision['disposition']
        items = decision['items']
        if (not isinstance(disposition, str) or disposition not in ('position', 'non_position', 'unresolved')
                or not isinstance(items, list)):
            raise ValueError('覆盖决策类型无效')
        if (disposition == 'position' and not items) or (disposition != 'position' and items):
            raise ValueError('位置决策必须有候选，非位置或未决不得夹带候选')
        decisions[sid] = decision
    if sum(len(d['items']) for d in decisions.values()) > MAX_ITEMS:
        raise ValueError('位置候选数量超限')
    if set(decisions) != set(index['slots']):
        raise ValueError('缺少覆盖任务决策，空数组不能表示已完整审查')
    table = extraction.segments(case, entities)
    items, ledger = [], []
    for sid, slot in index['slots'].items():
        decision = decisions[sid]
        indices = []
        for item in decision['items']:
            if (not isinstance(item, dict) or item.get('subject') != slot['subjectRef']
                    or item.get('boundary') not in slot['boundaryRefs']):
                raise ValueError('候选身份引用不属于当前覆盖任务')
            core = item.get('core')
            if (not isinstance(core, list) or not core
                    or any(not isinstance(s, str) or s not in table
                           or table[s]['unitId'] != slot['unitId'] for s in core)):
                raise ValueError('候选 core 超出当前覆盖单元；跨单元内容须保持未决')
            indices.append(len(items))
            items.append(copy.deepcopy(item))
        ledger.append(dict(slotId=sid, disposition=decision['disposition'], itemIndices=indices))
    proposal = {extraction.VERSION: items}
    checked = extraction.inspect(proposal, case, entities)
    if checked['status'] != 'structurally_valid':
        raise ValueError('候选未通过冻结引用与范围检查：'+checked['status'])
    coverage = dict(decisionsComplete=True, semanticCoverage='unverified',
                    scope=index['scope'], ledger=ledger, pendingUnits=copy.deepcopy(index['pendingUnits']),
                    unresolvedSlots=[x['slotId'] for x in ledger if x['disposition'] == 'unresolved'],
                    nonPositionSlots=[x['slotId'] for x in ledger if x['disposition'] == 'non_position'],
                    reviewRequired=True, productionEnablement=False)
    return proposal, coverage


def inspect(response, case, entities):
    try:
        proposal, coverage = decode(response, case, entities)
        result = dict(status='structurally_valid', decodedProposal=proposal, coverage=coverage)
    except (ValueError, TypeError, KeyError) as error:
        result = dict(status='invalid_response', error=str(error))
    result.update(proposedResponse=copy.deepcopy(response), semanticStatus='unverified',
                  reviewRequired=True, productionEnablement=False)
    return result
