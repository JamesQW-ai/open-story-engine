"""Permanent item consequences, expressed through reviewed action changes."""

ATTRIBUTE = 'destroyedPermanently'
STATE_KEY = 'readerEntityStates'


def destroyed(state, item_id):
    states = state.get(STATE_KEY, {})
    item = states.get(item_id) if isinstance(states, dict) else None
    return isinstance(item, dict) and item.get(ATTRIBUTE) is True


def dependencies(update, previous=None):
    # Omission preserves existing requirements; only an explicit [] clears them.
    return update.get('itemDependencies', (previous or {}).get('itemDependencies', []))


def validate_dependencies(update, previous, plan, known, state, active):
    refs = dependencies(update, previous)
    if (not isinstance(refs, list) or len(refs) > 16
            or any(not isinstance(iid, str) or known.get(iid, {}).get('kind') != 'item' for iid in refs)
            or len(set(refs)) != len(refs)):
        raise ValueError('道具依赖必须是不重复的已登记道具 ID 数组')
    destroying = {c['entityId'] for c in plan['stateChanges'] if c['attribute'] == ATTRIBUTE and c['value'] is True}
    if active and any(destroyed(state, iid) or iid in destroying for iid in refs):
        raise ValueError('新建或更新的进行中目标／问题不能依赖已永久损毁的原道具；须有依据地改换路径或保留旧记录待复核')


def destruction_evidence(nodes, item_id):
    """A current terminal bit alone cannot invent its narrative provenance."""
    if not destroyed(nodes[-1]['branchState'], item_id):
        return None
    for node in nodes:
        if not destroyed(node['branchState'], item_id):
            continue
        for change in node.get('consequenceUpdate', {}).get('stateChanges', []):
            quote = change.get('evidence')
            if (change.get('entityId') == item_id and change.get('attribute') == ATTRIBUTE
                    and change.get('value') is True and change.get('before') is not True
                    and isinstance(quote, str) and quote.strip() and quote in node.get('narrativeText', '')):
                return dict(kind='narrative', branch_id=node['id'], ref=item_id, quote=quote)
    return None


def validate_changes(state, plan, known):
    changes = plan.get('stateChanges', [])
    unavailable = {iid for iid, entity in known.items() if entity['kind'] == 'item' and destroyed(state, iid)}
    destroying = set()
    for change in changes:
        iid, attribute, value = change['entityId'], change['attribute'], change['value']
        if attribute == ATTRIBUTE:
            if known[iid]['kind'] != 'item' or value is not True:
                raise ValueError('永久损毁标记只允许对道具登记 true，不能撤销')
            destroying.add(iid)
        if iid in unavailable and not (attribute == ATTRIBUTE and value is True):
            raise ValueError('已永久损毁的道具不能恢复、转交或继续改变可用状态：' + iid)
    for change in changes:
        if change['entityId'] in destroying and change['attribute'] in ('ownerCharacterId', 'locationId') and change['value'] is not None:
            raise ValueError('同一回合不能永久损毁道具并重新登记持有人或位置')
    # A step may use the object while destroying it, but later steps may not.
    for step in plan.get('steps', []):
        uses = step.get('usedItemIds', [])
        if not isinstance(uses, list) or any(not isinstance(iid, str) or known.get(iid, {}).get('kind') != 'item' for iid in uses):
            raise ValueError('行动使用的道具必须引用已登记道具 ID')
        if unavailable.intersection(uses):
            raise ValueError('行动不能使用已永久损毁的道具')
        unavailable.update(c['entityId'] for c in changes if c['stepId'] == step['id'] and c['attribute'] == ATTRIBUTE)


def clear_destroyed_positions(state):
    for iid in state.get(STATE_KEY, {}):
        if destroyed(state, iid):
            state.get('itemOwnerCharacterIds', {}).pop(iid, None)
            state.get('itemLocationIds', {}).pop(iid, None)


def compatible_art(card, state):
    requirements = card.get('required_state', {})
    explicit = requirements.get(STATE_KEY, {})
    states = state.get(STATE_KEY, {})
    if not isinstance(states, dict):
        return not any(requirements.get(field) for field in (STATE_KEY, 'itemOwnerCharacterIds', 'itemLocationIds'))
    for iid in states:
        if destroyed(state, iid) and any(iid in facts for facts in requirements.values()):
            required = explicit.get(iid)
            if not isinstance(required, dict) or required.get(ATTRIBUTE) is not True:
                return False
    return True
