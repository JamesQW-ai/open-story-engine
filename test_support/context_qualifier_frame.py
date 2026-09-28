"""Explicit attribution and bounded dependency partition; no semantic inference."""
import copy
import json

from test_support import context_qualifier_coverage as previous

extraction = previous.extraction
VERSION = 'qualifier-frame/0.1'
MAX_CONTEXT_UNITS = 8
MAX_TASK_CHARS = 16384
ATTRIBUTIONS = {'position', 'word_mention', 'other_entity', 'no_position', 'unresolved'}


def model_input(case, entities):
    data = previous.model_input(case, entities)
    units = extraction.f.indexed_input(case)['units']
    blocks, block = [], []
    prior_end = 0
    for uid, unit in units.items():
        # Only a punctuation envelope, never a condition or attribution classifier.
        gap = case['draft'][prior_end:unit['start']]
        if block and '\n\n' in gap:
            blocks.append(block)
            block = []
        block.append(uid)
        if unit['quote'].rstrip().rstrip('”’"」』').endswith(tuple('。！？!?；;')):
            blocks.append(block)
            block = []
        prior_end = unit['end']
    if block:
        blocks.append(block)
    envelope = {uid: group for group in blocks for uid in group}
    tasks = {}
    for slot in data['coverageIndex']['slots'].values():
        context = [uid for uid in envelope[slot['unitId']] if uid != slot['unitId']]
        if len(context) > MAX_CONTEXT_UNITS:
            raise ValueError('依赖候选单元预算超限，不截断原文')
        tasks[slot['subjectRef']] = dict(boundaries=slot['boundaryRefs'], contextUnits=context)
    if len(json.dumps(tasks, ensure_ascii=False)) > MAX_TASK_CHARS:
        raise ValueError('归属任务字符预算超限')
    return dict(source=data['source'], entityRefs=data['entityRefs'], tasks=tasks)


def _fields(value, fields, message):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(message)


def _units(value, allowed):
    if (not isinstance(value, list)
            or any(not isinstance(uid, str) or uid not in allowed for uid in value)
            or len(set(value)) != len(value)
            or value != [uid for uid in allowed if uid in value]):
        raise ValueError('依赖单元须来自允许范围、有序且不重复')
    return value


def decode(response, case, entities):
    data = model_input(case, entities)
    tasks = data['tasks']
    _fields(response, {VERSION}, '须显式返回 '+VERSION+'；不迁移旧响应')
    decisions = response[VERSION]
    if not isinstance(decisions, list) or len(decisions) > previous.MAX_SLOTS:
        raise ValueError('归属决策数组无效或超预算')
    converted, attribution, dependencies, seen = [], [], [], set()
    slots = previous.model_input(case, entities)['coverageIndex']['slots']
    slot_ids = {slot['subjectRef']: sid for sid, slot in slots.items()}
    total_items = 0
    for decision in decisions:
        _fields(decision, {'subject', 'attribution', 'items'}, '归属决策字段无效')
        subject, kind, items = (decision[k] for k in ('subject', 'attribution', 'items'))
        if not isinstance(subject, str) or subject not in tasks or subject in seen:
            raise ValueError('任务主体未知、重复或绑定其他原文')
        seen.add(subject)
        if not isinstance(kind, str) or kind not in ATTRIBUTIONS or not isinstance(items, list):
            raise ValueError('归属类型或候选列表无效')
        if (kind == 'position' and not items) or (kind != 'position' and items):
            raise ValueError('只有 position 可以且必须携带位置候选')
        total_items += len(items)
        if total_items > previous.MAX_ITEMS:
            raise ValueError('位置候选数量超限')
        mapped = []
        for item_index, item in enumerate(items):
            _fields(item, {'boundary', 'position', 'core', 'conditions', 'modifiers',
                           'nonPremiseUnits', 'unresolvedUnits'}, '位置框架字段无效')
            allowed = tasks[subject]['contextUnits']
            independent = _units(item['nonPremiseUnits'], allowed)
            unresolved = _units(item['unresolvedUnits'], allowed)
            if not isinstance(item['conditions'], list) or not isinstance(item['modifiers'], list):
                raise ValueError('条件与其他限定须为数组')
            assigned = list(independent)+list(unresolved)
            limitations = []
            for condition in item['conditions']:
                _fields(condition, {'units', 'cue'}, '条件依赖字段无效')
                units = _units(condition['units'], allowed)
                if not units:
                    raise ValueError('条件依赖必须选择完整前提单元')
                assigned.extend(units)
                limitations.append(dict(kind='condition', cue=copy.deepcopy(condition['cue']),
                                        premiseUnits=list(units)))
            if len(assigned) != len(set(assigned)) or set(assigned) != set(allowed):
                raise ValueError('依赖候选须恰好分类一次，禁止重叠或遗漏')
            for modifier in item['modifiers']:
                _fields(modifier, {'kind', 'cue'}, '其他限定字段无效')
                if modifier['kind'] == 'condition':
                    raise ValueError('条件须进入 conditions，不得绕过依赖分类')
                limitations.append(dict(kind=modifier['kind'], cue=copy.deepcopy(modifier['cue']), premiseUnits=[]))
            mapped.append(dict(subject=subject, boundary=item['boundary'], position=copy.deepcopy(item['position']),
                               core=copy.deepcopy(item['core']), limitations=limitations,
                               reason='structured_source_evidence'))
            dependencies.append(dict(subject=subject, itemIndex=item_index,
                                     conditions=copy.deepcopy(item['conditions']),
                                     nonPremiseUnits=list(independent), unresolvedUnits=list(unresolved)))
        disposition = 'position' if kind == 'position' else 'unresolved' if kind == 'unresolved' else 'non_position'
        converted.append(dict(slotId=slot_ids[subject], disposition=disposition, items=mapped))
        attribution.append(dict(subject=subject, attribution=kind))
    if seen != set(tasks):
        raise ValueError('缺少任务主体的归属决策')
    # Compatibility projection fills only identity/format fields, never semantics.
    proposal, ledger = previous.decode({previous.VERSION: converted}, case, entities)
    ledger.update(attribution=attribution, dependencies=dependencies,
                  dependencyPartitionComplete=True,
                  unresolvedDependencies=[x for x in dependencies if x['unresolvedUnits']],
                  legacyReasonOrigin='adapter_sentinel_not_model_explanation')
    return proposal, ledger


def inspect(response, case, entities):
    try:
        proposal, ledger = decode(response, case, entities)
        status = 'dependency_pending' if ledger['unresolvedDependencies'] else 'structurally_valid'
        key = 'withheldProposal' if status == 'dependency_pending' else 'decodedProposal'
        result = dict(status=status, coverage=ledger, **{key: proposal})
    except (ValueError, TypeError, KeyError) as error:
        result = dict(status='invalid_response', error=str(error))
    result.update(proposedResponse=copy.deepcopy(response), semanticStatus='unverified',
                  reviewRequired=True, productionEnablement=False)
    return result


def assess(response, case, entities):
    result = inspect(response, case, entities)
    if result['status'] != 'structurally_valid':
        return result
    actual = {x['subject']: x['attribution'] for x in result['coverage']['attribution']}
    result['attributionMatches'] = actual == case['frameAttributions']
    strict = extraction.assess(result['decodedProposal'], case, entities)
    result['extractionAssessment'] = strict
    matched = result['attributionMatches'] and strict['status'] == 'matched'
    result['status'] = 'matched' if matched else 'mismatched'
    if matched and case['cohort'] == 'scope_controls':
        result['status'] = 'scope_pending'
    return result
