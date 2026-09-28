"""Committed typed facts and bounded, ancestry-scoped memory selection.

Only the local consequence commit path may mint receipts. Free-text summaries
and model-supplied memory records are never promoted here.
"""
import copy
import hashlib
import json
from collections import Counter

LEGACY_VERSION = 'committed-outcome-memory/1'
VERSION = 'committed-outcome-memory/2'
MAX_SELECTED = 4
MAX_CONTENT_CHARS = 800
MAX_LIFECYCLE_DETAILS = 32
_LABELS = {'dead': '已死亡', 'departed': '已离队', 'missing': '已失踪',
           'injured': '已受伤', 'alive': '已确认存活'}


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _package(package):
    return dict(id=package['id'], version=package['version'],
                moduleIndexSha256=package.get('moduleIndexSha256'))


def receipt_for(package, node, *, version=VERSION):
    """Build after commit_consequences has persisted the observed changes.

    This records validation bindings, not a replacement semantic validator.
    The receipt is stored in the same branch JSON/transaction as its cause.
    """
    from .context_bundle import validate_dynamic_memory_transition
    from .reader_consequences import VERSION as review_version

    if version not in (LEGACY_VERSION, VERSION):
        raise ValueError('动态记忆版本不受支持')
    update = node.get('consequenceUpdate') or {}
    outcomes = update.get('outcomes', [])
    fields = ('outcomes',) if version == LEGACY_VERSION else ('outcomes', 'stateChanges', 'goalUpdates', 'threadUpdates')
    if not any(update.get(key) for key in fields) or node.get('fallbackMode'):
        return None
    body = node['narrativeText']
    body_sha = hashlib.sha256(body.encode()).hexdigest()
    if 'deliveryReceipt' in node:
        from .narrative_delivery import VERSION as delivery_version
        receipt = node['deliveryReceipt']
        if (not isinstance(receipt, dict) or receipt.get('version') != delivery_version
                or receipt.get('kind') != 'observed_state_not_prose_approval'
                or receipt.get('narrativeSha256') != body_sha):
            raise ValueError('动态记忆与已保存正文绑定不符')
        validation_binding = {k: node.get(k) for k in ('consequenceUpdate', 'deliveryReceipt')}
    elif 'stateReceipt' in node:
        from .state_receipt import VERSION as receipt_version
        receipt = node['stateReceipt']
        if (not isinstance(receipt, dict) or receipt.get('version') != receipt_version
                or receipt.get('kind') != 'state_extraction_not_quality_review'
                or receipt.get('narrativeSha256') != body_sha or not isinstance(receipt.get('data'), dict)):
            raise ValueError('动态记忆需要与正文绑定的状态凭据')
        validation_binding = {k: node.get(k) for k in ('consequenceUpdate', 'stateReceipt')}
    else:
        if node.get('consequenceReview') != review_version or node.get('reviewedNarrativeSha256') != body_sha:
            raise ValueError('动态记忆需要与当前正文一致的后果审核')
        if (not isinstance(node.get('authorityReview'), dict)
            or node['authorityReview'].get('decision') != 'allow'
            or not isinstance(node.get('observedEvents'), list)
            or not isinstance(node.get('eventChecks'), list)):
            raise ValueError('动态记忆缺少行动授权与事件审核凭据')
        validation_binding = {key: node[key] for key in (
            'consequenceReview', 'consequenceUpdate', 'authorityReview',
            'observedEvents', 'eventChecks', 'reviewedNarrativeSha256')}
    binding = dict(schemaVersion=version, package=_package(package),
                   originBranchId=node['id'], parentBranchId=node['parentId'],
                   sourceNodeRef=node['sourceNodeRef'], narrativeSha256=body_sha,
                   intentSha256=_sha({key: node.get(key) for key in (
                       'playerDirection', 'selectedDirectionId', 'selectedDirection', 'actionIntent')}),
                   reviewSha256=_sha(validation_binding))
    names = {c['id']: c['name'] for c in list(package['characters']) + node['branchState'].get('derivedCharacters', [])}
    records = []
    for item in outcomes:
        cid = item['characterId']
        state = node['branchState'].get('characterOutcomeStates', {}).get(cid)
        if not state or state.get('causeBranchId') != node['id']:
            continue  # Repetition never replaces the first committed cause.
        if state != {**item, 'causeBranchId': node['id']}:
            raise ValueError('动态记忆后果与已提交状态不符')
        if (item['status'] not in _LABELS or cid not in names
                or not isinstance(item.get('evidence'), str) or not item['evidence'].strip()
                or item['evidence'] not in body):
            raise ValueError('动态记忆缺少有效人物、后果或逐字依据')
        content = names[cid] + _LABELS[item['status']]
        if item['status'] == 'departed' and item.get('permanence') == 'permanent':
            content += '，不会再回到这条路线'
        content += '。'
        source_id = 'memory:outcome:' + node['id'] + ':' + cid
        candidate = dict(memoryId='memory-' + _sha([binding, cid])[:24], kind='event',
                         sourceIds=[source_id], branchId=node['id'], visibility='player_known',
                         authority='confirmed_evidence', validity='unknown', status='candidate',
                         sequence=1, content=content)
        confirmed = {**candidate, 'status': 'confirmed', 'validity': 'confirmed', 'sequence': 2}
        validate_dynamic_memory_transition(candidate, confirmed, branch_id=node['id'])
        records.append(dict(entityId=cid, stateSha256=_sha(state), memory=confirmed))
    if version == VERSION:
        for domain, entity, state, content in _extra_facts(package, node):
            candidate = dict(memoryId='memory-' + _sha([binding, domain, entity])[:24], kind='event',
                             sourceIds=['memory:' + domain + ':' + node['id'] + ':' + entity],
                             branchId=node['id'], visibility='player_known', authority='confirmed_evidence',
                             validity='unknown', status='candidate', sequence=1, content=content)
            confirmed = {**candidate, 'status': 'confirmed', 'validity': 'confirmed', 'sequence': 2}
            validate_dynamic_memory_transition(candidate, confirmed, branch_id=node['id'])
            records.append(dict(domain=domain, entityId=entity, stateSha256=_sha(state), memory=confirmed))
    return {**binding, 'records': records} if records else None


def _item_state(state, entity):
    from .item_lifecycle import destroyed
    return dict(destroyed=destroyed(state, entity),
                owner=state.get('itemOwnerCharacterIds', {}).get(entity),
                location=state.get('itemLocationIds', {}).get(entity))


def _extra_facts(package, node):
    """Typed committed facts only; ledger titles remain goal/question labels."""
    from .reader_actions import registry, value_at

    state, update, body = node['branchState'], node['consequenceUpdate'], node['narrativeText']
    known = registry(package, state)

    def require_evidence(item):
        quote = item.get('evidence')
        if not isinstance(quote, str) or not quote.strip() or quote not in body:
            raise ValueError('动态记忆缺少逐字依据')

    changed_items = {}
    for item in update.get('stateChanges', []):
        entity, attr = item['entityId'], item['attribute']
        if known.get(entity, {}).get('kind') != 'item' or attr not in ('destroyedPermanently', 'ownerCharacterId', 'locationId'):
            continue
        require_evidence(item)
        if value_at(state, entity, attr, 'item') != item['value']:
            raise ValueError('动态记忆物品变化与已提交状态不符')
        if item['before'] != item['value']:
            changed_items.setdefault(entity, set()).add(attr)
    for entity in sorted(changed_items):
        current = _item_state(state, entity)
        name = known[entity]['name']
        changed = changed_items[entity]
        if current['destroyed'] and 'destroyedPermanently' in changed:
            content = name + '已永久毁坏。'
        elif 'ownerCharacterId' in changed and current['owner'] and known.get(current['owner'], {}).get('kind') == 'character':
            content = name + '当前由' + known[current['owner']]['name'] + '持有。'
        elif 'locationId' in changed and current['location'] and known.get(current['location'], {}).get('kind') == 'location':
            content = name + '当前位于' + known[current['location']]['name'] + '。'
        else:
            continue  # Cleared/unknown whereabouts invalidate old memory, never invent a destination.
        yield 'item', entity, current, content

    for domain, field, ledger_key, labels in (
            ('goal', 'goalUpdates', 'goalLedger', {'active': '进行中', 'completed': '已完成', 'transformed': '已转化', 'abandoned': '已放下'}),
            ('thread', 'threadUpdates', 'threadLedger', {'open': '待处理', 'resolved': '已解决', 'abandoned': '已放下'})):
        ledger = {entry['id']: entry for entry in state.get(ledger_key, [])}
        for index, item in enumerate(update.get(field, [])):
            require_evidence(item)
            new_id = domain + '-' + hashlib.sha256((node['id'] + ':' + str(index)).encode()).hexdigest()[:16]
            is_new = item['id'] == 'new' if domain == 'goal' else item['id'].startswith('new-')
            targets = [(new_id if is_new else item['id'], item['title'], item['status'])]
            if domain == 'goal' and item['status'] == 'transformed':
                targets.append((new_id, item['successor'], 'active'))
            for entity, title, status in targets:
                current = ledger.get(entity)
                checks = ('reason', 'evidence', 'dependencies', 'itemDependencies') if domain == 'goal' else ('reason', 'evidence', 'priority', 'recoveryWindow', 'itemDependencies')
                if (not current or current.get('causeBranchId') != node['id']
                        or current.get('title') != title or current.get('status') != status
                        or status not in labels or any(current.get(key) != item[key] for key in checks if key in item)
                        or (domain == 'goal' and item['status'] == 'transformed' and entity == new_id
                            and current.get('previousGoalId') != item['id'])):
                    raise ValueError('动态记忆账本变化与已提交状态不符')
                prefix = '目标' if domain == 'goal' else '剧情问题'
                yield domain, entity, current, prefix + '「' + title + '」：' + labels[status] + '。'


def validate_receipt(package, node, *, allow_legacy=False):
    """Recheck a prepared artifact before its atomic branch write."""
    receipt = node.get('contextMemory')
    version = receipt.get('schemaVersion') if isinstance(receipt, dict) else VERSION
    if version != VERSION and not (allow_legacy and version == LEGACY_VERSION):
        raise ValueError('动态记忆版本不受支持')
    expected = receipt_for(package, node, version=version)
    if node.get('contextMemory') != expected:
        raise ValueError('动态记忆凭据与分支、正文或审核不一致')


def select(context, state, module_context):
    """Select current, relevant facts; return memory, proof and audit separately."""
    from .context_bundle import validate_dynamic_memory_transition

    lineage = context.get('lineage', [])
    audit = dict(schemaVersion=VERSION, selectedCount=0, omitted={}, contentChars=0,
                 selectionPolicy='explicit_subject_before_module_recency/1')
    lifecycle = dict(schemaVersion='memory-selection-lifecycle/1', counts={}, details=[], omittedDetails=0)
    audit['lifecycle'] = lifecycle

    def record_decision(reason, node, record=None, replacement=None):
        lifecycle['counts'][reason] = lifecycle['counts'].get(reason, 0) + 1
        if len(lifecycle['details']) >= MAX_LIFECYCLE_DETAILS:
            lifecycle['omittedDetails'] += 1
            return
        detail = dict(reason=reason, originBranchId=node['id'], atBranchId=context['parent']['id'])
        if record is not None:
            detail.update(memoryId=record['memory']['memoryId'], domain=record.get('domain', 'outcome'), entityId=record['entityId'])
        if replacement is not None:
            detail['replacementMemoryId'] = replacement
        lifecycle['details'].append(detail)
    if not any(n.get('contextMemory') is not None for n in lineage):
        return [], [], audit
    parent, package = context['parent'], context['package']
    if not lineage or lineage[-1]['id'] != parent['id']:
        raise ValueError('动态记忆 lineage 未绑定当前父分支')
    seen = set()
    for index, node in enumerate(lineage):
        if (node['id'] in seen or node.get('sessionId') != parent.get('sessionId')
                or (index and node.get('parentId') != lineage[index - 1]['id'])
                or (not index and node.get('parentId'))):
            raise ValueError('动态记忆只能读取当前会话的完整祖先链')
        seen.add(node['id'])
    from .reader_actions import registry

    relevant = {c['id'] for key in ('characters', 'items', 'locations') for c in module_context.get(key, [])}
    action = context.get('playerDirection') or ''
    names = {key: item['name'] for key, item in registry(package, state).items()}
    explicit = {cid for cid, name in names.items() if name and name in action}
    relevant.update(explicit)
    memories, evidence, omitted = [], [], Counter()
    verified = []
    for node in reversed(lineage):
        if node.get('contextMemory') is None:
            continue  # Legacy history is never retroactively promoted to confirmed memory.
        try:
            validate_receipt(package, node, allow_legacy=True)
        except (ValueError, TypeError, KeyError, AttributeError):
            omitted['source_changed'] += 1
            record_decision('source_invalid', node)
            continue
        verified.append(node)
    # Track actual snapshot changes, including implicit ownership clearing and legacy
    # nodes without receipts. A -> B -> A must never revive A's original cause.
    item_ids = {r['entityId'] for n in verified for r in n['contextMemory']['records']
                if r.get('domain') == 'item'}
    latest_item_change = {}
    for before, after in zip(lineage, lineage[1:]):
        for entity in item_ids:
            if _item_state(before['branchState'], entity) != _item_state(after['branchState'], entity):
                latest_item_change[entity] = after['id']
    size = 0
    current_ledgers = {domain: {entry['id']: entry for entry in state.get(field, [])}
                       for domain, field in (('goal', 'goalLedger'), ('thread', 'threadLedger'))}
    current_snapshots, replacements, candidates = {}, {}, []
    for node in verified:
        receipt = node['contextMemory']
        for record in receipt['records']:
            cid = record['entityId']
            domain = record.get('domain', 'outcome')
            ledger_key = {'outcome': 'characterOutcomeStates', 'goal': 'goalLedger', 'thread': 'threadLedger', 'item': 'items'}[domain]
            key = (domain, cid)
            if key not in current_snapshots:
                if domain == 'item':
                    current = _item_state(state, cid)
                    cause = latest_item_change.get(cid)
                else:
                    current = (state.get(ledger_key, {}) if domain == 'outcome' else current_ledgers[domain]).get(cid)
                    cause = (current or {}).get('causeBranchId')
                current_snapshots[key] = (current, cause, _sha(current) if current else None)
            current, cause, state_sha = current_snapshots[key]
            if not current or cause != node['id'] or state_sha != record['stateSha256']:
                omitted['state_changed'] += 1
                replacement = replacements.get(key)
                record_decision('superseded' if replacement else 'state_unverified', node, record, replacement)
                continue
            # Even an irrelevant/budget-omitted current fact can supersede an
            # old one. This audit is not injected into any model projection.
            replacements[key] = record['memory']['memoryId']
            matches = cid in relevant
            priority = 0 if cid in explicit else 2
            if domain in ('goal', 'thread'):
                dependencies = set(current.get('dependencies', []) + current.get('itemDependencies', []))
                matches = (bool(dependencies & relevant) or current['title'] in action
                           or any(names.get(entity) and names[entity] in current['title'] for entity in relevant))
                if current['title'] in action:
                    priority = 0
                elif (dependencies & explicit
                      or any(names[entity] in current['title'] for entity in explicit)):
                    priority = 1
            if not matches:
                omitted['irrelevant'] += 1
                continue
            candidates.append((priority, node, record, ledger_key))
    # Stable sorting preserves recency within each relevance tier. Priority
    # cannot rescue invalid receipts/states, which were rejected above.
    for _, node, record, ledger_key in sorted(candidates, key=lambda row: row[0]):
        previous = record['memory']
        if len(memories) >= MAX_SELECTED or size + len(previous['content']) > MAX_CONTENT_CHARS:
            omitted['selection_budget'] += 1
            continue
        memory = copy.deepcopy(previous)
        if node['id'] != parent['id']:
            memory.update(memoryId='memory-' + _sha([previous['memoryId'], parent['id']])[:24],
                          branchId=parent['id'], parentMemoryId=previous['memoryId'], sequence=3,
                          inheritanceReason='已提交祖先来源仍有效且当前状态一致')
            validate_dynamic_memory_transition(previous, memory, branch_id=parent['id'])
        memories.append(memory)
        cid = record['entityId']
        location = ('.consequenceUpdate.stateChanges[entityId=' + cid + ']'
                    if record.get('domain') == 'item' else '.' + ledger_key + '.' + cid)
        evidence.append(dict(sourceId=memory['sourceIds'][0], kind='confirmed_event',
                             visibility='player_known', branchId=parent['id'],
                             location='branch:' + node['id'] + location,
                             content=memory['content'], authority='confirmed_evidence',
                             validity='confirmed', tier='protected'))
        size += len(memory['content'])
    audit.update(selectedCount=len(memories), omitted=dict(omitted), contentChars=size)
    return memories, evidence, audit
