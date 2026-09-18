"""Explicit closing intent and early-end receipts, separate from story facts."""
import json

from .storage import now

MODES = ('normal', 'deviation', 'failure')


def records(store, sid, nodes):
    exists = store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='route_lifecycle'").fetchone()
    if not exists:
        return {}
    ids = {n['id'] for n in nodes}
    return {row['branch_id']: json.loads(row['record_json']) for row in store.connection.execute(
        'SELECT branch_id,record_json FROM route_lifecycle WHERE session_id=?', (sid,)) if row['branch_id'] in ids}


def save_record(store, sid, bid, record):
    store.connection.execute('INSERT INTO route_lifecycle VALUES(?,?,?) ON CONFLICT(session_id,branch_id) DO UPDATE SET record_json=excluded.record_json',
                             (sid, bid, json.dumps(record, ensure_ascii=False)))


def closing_intent(store, sid, nodes):
    saved = records(store, sid, nodes)
    result = None
    for node in nodes:
        record = saved.get(node['id'], {})
        if 'intent' in record:
            if record['intent'] not in (*MODES, None):
                raise ValueError('收束意图无效')
            result = dict(intended_type=record['intent'], branch_id=node['id'], revision=record.get('revision'))
    return result


def planning_projection(package, contract, nodes, intent, closure):
    """Ledger prerequisites for an intended ending, never ending approval."""
    from .route_outline import build_outline
    mode = intent.get('intended_type') if intent else None
    if mode is None:
        return None
    if mode not in MODES:
        raise ValueError('收束意图无效')
    unmet = []
    if closure['readiness'] != 'checklist_clear':
        unmet.append(dict(code='unresolved_obligations', target_id=None))
    try:
        goals = build_outline(package, contract, nodes)['goals']
        if mode == 'normal':
            for goal in goals:
                if goal['status'] != 'completed':
                    unmet.append(dict(code='goal_not_completed', target_id=goal['id']))
        elif mode == 'deviation':
            if not any(g['status'] in ('abandoned', 'transformed') and g['evidence'] for g in goals):
                unmet.append(dict(code='goal_change_evidence_required', target_id=None))
        elif not any(g['status'] == 'abandoned' and g['evidence'] for g in goals):
            unmet.append(dict(code='unachieved_goal_evidence_required', target_id=None))
        if not goals:
            unmet.append(dict(code='goal_scope_unknown', target_id=None))
    except (ValueError, TypeError, KeyError, AttributeError):
        unmet.append(dict(code='ledger_unknown', target_id=None))
    return dict(intended_type=mode, intent_branch_id=intent['branch_id'],
                phase='closing' if closure['readiness'] == 'checklist_clear' else 'preparing',
                ending_check=dict(ledger_ready=not unmet, unmet_conditions=unmet,
                                  outcome_review_required=True, approved=False),
                note='收束意图不授权额外行动。账本条件通过仍须独立核对结局正文及因果；主动放弃不证明失败，改换目标不证明偏离结局已经成立。')


def view(store, sid, nodes, closure):
    saved = records(store, sid, nodes)
    intent, origin, receipt = None, None, None
    for node in nodes:
        record = saved.get(node['id'], {})
        if 'intent' in record:
            intent, origin = record['intent'], node['id']
        if record.get('receipt'):
            receipt = record['receipt']
    if intent not in (*MODES, None):
        raise ValueError('收束意图无效')
    ended = closure['status'] != 'active'
    phase = ('ended' if ended else 'active' if intent is None else
             'closing' if closure['readiness'] == 'checklist_clear' else 'preparing')
    return dict(phase=phase, intended_type=intent, intent_branch_id=origin,
                ending_type=receipt['ending_type'] if ended and receipt else None,
                receipt=receipt if ended else None, requires_ending_evidence=True,
                ending_written=bool(ended and receipt and receipt.get('ending_written') is True))


def early_receipt(bid, closure):
    return dict(ending_type='early', branch_id=bid, closed_at=now(),
                readiness=closure['readiness'], coverage=closure['coverage'],
                outstanding=closure['outstanding'], cleared=closure['cleared'],
                ending_written=False)
