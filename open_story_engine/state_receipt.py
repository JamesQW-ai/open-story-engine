"""State extraction receipts, explicitly not narrative quality approval."""
import hashlib
import re
import copy

from . import reader_actions, reader_consequences
from .api_reader_quality import action_requirements, bind_action_results

VERSION = 'context-state-receipt/1'


def evidence_slots(plan):
    """Stable item keys avoid positional parallel-array bookkeeping by models."""
    slots = {}
    for collection, field, prefix, identifier in (
            ('outcomes', 'outcomeEvidence', 'outcome', 'characterId'),
            ('goalUpdates', 'goalEvidence', 'goal', 'id'),
            ('threadUpdates', 'threadEvidence', 'thread', 'id'),
            ('stateChanges', 'changeEvidence', 'change', 'id')):
        for index, item in enumerate(plan.get(collection, [])):
            slots[prefix + ':' + item[identifier]] = (field, index)
    for index, entity in enumerate(reader_actions.introductions(plan)):
        slots['introduction:' + entity['id']] = ('introductionEvidence', index)
    return slots


def validate(data, context, plan, body, expected_state):
    if not isinstance(data, dict):
        raise ValueError('状态提取须返回 JSON 对象')
    data = copy.deepcopy(data)
    if 'evidence' in data:
        slots = evidence_slots(plan)
        evidence = data['evidence']
        if not isinstance(evidence, dict) or set(evidence) != set(slots):
            actual = set(evidence) if isinstance(evidence, dict) else set()
            raise reader_consequences.ConsequenceEvidenceError(
                'evidence键缺失：' + str(sorted(set(slots) - actual))
                + '；多余：' + str(sorted(actual - set(slots))) + '。按对应条目提取实际段号，不得虚构完成。')
        for field in ('outcomeEvidence', 'goalEvidence', 'threadEvidence', 'changeEvidence', 'introductionEvidence'):
            data[field] = [evidence[key] for key, (target, _) in slots.items() if target == field]
    known = reader_actions.registry(context['package'], context['parent']['branchState'], plan.get('introductions'))
    for event in data.get('events', []) if isinstance(data.get('events'), list) else []:
        if not isinstance(event, dict) or not isinstance(event.get('changes'), list):
            continue
        for change in event['changes']:
            if (isinstance(change, dict) and isinstance(change.get('entityId'), str)
                    and known.get(change['entityId'], {}).get('kind') == 'item'
                    and change.get('attribute') == 'holderCharacterId'):
                change['attribute'] = 'ownerCharacterId'
    for field, planned in (('outcomeEvidence', plan.get('outcomes', [])),
                           ('goalEvidence', plan.get('goalUpdates', [])),
                           ('threadEvidence', plan.get('threadUpdates', [])),
                           ('changeEvidence', plan.get('stateChanges', [])),
                           ('introductionEvidence', [item for group in ('characters', 'items', 'locations')
                                                     for item in plan.get('introductions', {}).get(group, [])])):
        refs = data.get(field)
        # With one planned item, several paragraph IDs have exactly one
        # possible grouping. Multiple items still require explicit grouping.
        if (len(planned) == 1 and isinstance(refs, list) and 1 < len(refs) <= 5
                and all(isinstance(ref, str) and re.fullmatch(r'P[1-9][0-9]*', ref) for ref in refs)):
            data[field] = [refs]
    extracted = data.get('events')
    if not isinstance(extracted, list):
        raise ValueError('状态提取缺少事件列表')
    refs = {e.get('paragraphId') for e in extracted if isinstance(e, dict)}
    events = reader_actions.validate_observations(data, body, refs) if extracted else []
    links = data.get('eventLinks')
    if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
        raise ValueError('状态提取缺少事件到契约的关联')
    reader_actions.validate_event_links(links, events, plan,
                                       context['parent']['branchState'], context['package'])
    reader_consequences.validate_final_state(data, expected_state)
    outcome = bind_action_results(data, action_requirements(context['playerDirection']), body)
    update = reader_consequences.bind_state_evidence(data, plan, body, outcome)
    return update, outcome, events


def seal(data, action, body):
    return dict(version=VERSION, kind='state_extraction_not_quality_review',
                narrativeSha256=hashlib.sha256(body.encode()).hexdigest(),
                actionSha256=hashlib.sha256(action.encode()).hexdigest(), data=data)


def validate_commit(context, result, update, state):
    receipt = result.get('stateReceipt')
    body = result['narrativeText']
    action = context.get('playerDirection') or result['actionIntent']['input']
    if (not isinstance(receipt, dict) or receipt.get('version') != VERSION
            or receipt.get('kind') != 'state_extraction_not_quality_review'
            or receipt.get('narrativeSha256') != hashlib.sha256(body.encode()).hexdigest()
            or receipt.get('actionSha256') != hashlib.sha256(action.encode()).hexdigest()):
        raise ValueError('状态凭据与本回合正文或行动不符')
    expected = reader_consequences.projected_state(state, update, context['package'])
    checked, _, _ = validate(receipt.get('data'), {**context, 'playerDirection': action}, update, body, expected)
    if checked != update:
        raise ValueError('状态凭据与提交变化不一致')
