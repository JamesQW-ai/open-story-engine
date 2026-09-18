"""Closure preparation checklist from committed ancestry; never writes an ending."""
import hashlib

from .reader_consequences import initial_goals
from .reader_threads import initial_threads
from .route_outline import build_outline

VERSION = 'route-closure/2'

# Passing this checklist only means no outstanding ledger item is left
# unexplained. It does not mean a natural ending has been written, approved
# or accepted as narrative quality.
NOTE = '清单通过仅表示当前路线没有未交代的目标、开放问题或受阻依赖；不表示自然结局已写成，也不构成叙事质量验收。'

ACTIVE_GOAL = ('active',)
OPEN_THREAD = ('open',)
UNKNOWN = ('unknown',)
CLOSED = ('completed', 'resolved', 'abandoned', 'transformed')


def _item(kind, target_id, title, reason, required=True, blockers=None, evidence=None):
    return dict(id=kind + ':' + target_id, kind=kind, target_id=target_id, title=title,
                reason=reason, required_disclosure=required, blockers=list(blockers or []),
                evidence=evidence)


def _inventory(package, contract, nodes, kind):
    """Missing records are unknown obligations, never evidence of closure."""
    initial = initial_goals if kind == 'goal' else initial_threads
    expected = {item['id']: item['title'] for item in initial(package, contract)}
    complete = True
    field = kind + 'Ledger'
    for index, node in enumerate(nodes):
        state = node.get('branchState')
        if not isinstance(state, dict):
            complete = False
            continue
        if field not in state:
            complete = complete and index == 0
        else:
            records = state[field]
            if not isinstance(records, list):
                raise ValueError('收束历史账本格式无效')
            seen = set()
            for item in records:
                if item['id'] in seen:
                    raise ValueError('收束历史账本 ID 重复')
                seen.add(item['id'])
                expected[item['id']] = item['title']
        # Include declared additions even if they disappeared from the very
        # snapshot that introduced them, especially transformed successors.
        for offset, update in enumerate(node.get('consequenceUpdate', {}).get(kind + 'Updates', [])):
            target = update['id']
            new = target == 'new' if kind == 'goal' else target.startswith('new-')
            if new:
                target = kind + '-' + hashlib.sha256((node['id'] + ':' + str(offset)).encode()).hexdigest()[:16]
            expected[target] = update['title']
            if kind == 'goal' and update['status'] == 'transformed':
                successor = 'goal-' + hashlib.sha256((node['id'] + ':' + str(offset)).encode()).hexdigest()[:16]
                expected[successor] = update['successor']
    return expected, complete


def preparation(package, contract, nodes, route_status):
    """Itemize what still needs disclosure before planning a closeout."""
    outline = build_outline(package, contract, nodes)
    outstanding, cleared = [], []
    coverage = dict(state='unknown' if any(not isinstance(n.get('branchState'), dict) for n in nodes) else 'recorded')
    unknown_status = False
    conflicts_by_target = {}
    for conflict in outline['conflicts']:
        key = (conflict['target_kind'], conflict['target_id']) if 'item_id' in conflict else ('goal', conflict['goal_id'])
        conflicts_by_target.setdefault(key, []).append(conflict)
        unknown_status = unknown_status or conflict['status'] == 'unknown'

    for kind, records, active_states in (
            ('goal', outline['goals'], ACTIVE_GOAL),
            ('thread', outline['threads'], OPEN_THREAD)):
        expected, complete = _inventory(package, contract, nodes, kind)
        missing = set(expected) - {record['id'] for record in records}
        coverage[kind + 's'] = 'recorded' if complete and not missing else 'unknown'
        if not complete:
            outstanding.append(_item(kind, kind + 'Ledger:history',
                                     '历史目标记录' if kind == 'goal' else '历史问题记录',
                                     '路线历史中账本或状态缺失，是否还有未交代条目尚未确认'))
        for target in sorted(missing):
            outstanding.append(_item(kind, target, expected[target], '历史条目在当前账本中缺失，状态未确认，须核实去向'))
        for record in records:
            status = record['status']
            met = record['completion']['met']
            title, target = record['title'], record['id']
            unknown_status = unknown_status or status in UNKNOWN
            if status in UNKNOWN or (status in active_states and not met):
                blockers = [c['id'] for c in conflicts_by_target.get((kind, target), [])]
                reason = ('状态未确认，收束前须先核实' if status in UNKNOWN
                          else ('依赖人物或道具受阻，须核实可行路径或交代条目去向'
                                if blockers else '尚未完成，收束前须完成或有依据地放弃'))
                outstanding.append(_item(kind, target, title, reason, blockers=blockers,
                                         evidence=record.get('evidence')))
            elif status in CLOSED and record.get('evidence'):
                reason = {'abandoned': ('已有依据放弃；不表示目标达成' if kind == 'goal'
                                        else '已有依据放弃追查；不表示问题已查明'),
                          'transformed': '已有依据转化；后继目标单独核对'}.get(status, '已完成或有依据关闭')
                cleared.append(_item(kind, target, title, reason, required=False,
                                     evidence=record.get('evidence')))

    for conflict in outline['conflicts']:
        if 'item_id' in conflict:
            reason = ('依赖原道具已永久损毁，收束前须交代替代路径或条目去向'
                      if conflict['status'] == 'destroyed' else '道具损毁记录缺少当前路线的正文依据，须先核实')
            outstanding.append(_item('dependency', conflict['id'], conflict['target_id'], reason,
                                     blockers=[conflict['item_id']], evidence=conflict.get('evidence')))
            continue
        outstanding.append(_item(
            'dependency', conflict['id'], conflict['goal_id'],
            '目标依赖人物状态为 ' + conflict['status'] + '，收束前须交代替代路径或放弃该目标',
            blockers=[conflict['character_id']], evidence=conflict.get('evidence')))

    unknown_blocks = (
        coverage['goals'] == 'unknown'
        or coverage['threads'] == 'unknown'
        or coverage['state'] == 'unknown'
        or unknown_status
    )
    if route_status != 'active':
        readiness = 'ended'
    elif unknown_blocks:
        readiness = 'blocked_unknown'
    elif any(i['kind'] == 'dependency' or i['blockers'] for i in outstanding):
        readiness = 'blocked_dependency'
    elif outstanding:
        readiness = 'needs_explanation'
    else:
        readiness = 'checklist_clear'

    return dict(
        version=VERSION,
        branch_id=nodes[-1]['id'],
        root_branch_id=nodes[0]['id'],
        status=route_status,
        structure=outline['structure'],
        coverage=coverage,
        outstanding=outstanding,
        cleared=cleared,
        outstanding_count=len(outstanding),
        cleared_count=len(cleared),
        readiness=readiness,
        # readiness=checklist_clear is not an ending and never claims one.
        ending_written=False,
        note=NOTE,
    )


def closure_projection(package, contract, nodes):
    """Planner-facing outstanding obligations; never invents completion."""
    # Planning always continues an active route. Failures must not crash the
    # turn or imply that open obligations were resolved.
    try:
        result = preparation(package, contract, nodes, 'active')
    except (ValueError, TypeError, KeyError, AttributeError):
        return dict(readiness='unknown', outstanding=[], note='收束清单暂不可用；不得据此推断目标或问题已解决。')
    items = []
    for item in result['outstanding']:
        if not item['required_disclosure']:
            continue
        items.append({key: item[key] for key in
                      ('kind', 'target_id', 'title', 'reason', 'required_disclosure', 'blockers')})
    return dict(readiness=result['readiness'], outstanding=items, note=NOTE)
