"""Bound ending proposals: independent review, then explicit atomic commit."""
import copy
import json
import time
import uuid

from .api_read import ReadError
from .api_turn_drafts import digest, reported_usage
from .api_journey import preferences, route_status, save_preferences
from .llm import LlmError, parse_json_content
from .prompts import catalog_version, render_prompt
from .route_closure import preparation
from .route_lifecycle import closing_intent, planning_projection, records, save_record
from .route_outline import build_outline
from .storage import SessionStore, now
from .character_presentation import known_status

CHECKS = ('ending_type_supported', 'threads_accounted_for', 'no_new_unresolved_conflict', 'ending_present')


def context(read, store, sid, bid):
    session = read.session(store, sid)
    read.branch_state(read.branch(store, sid, bid))
    _, package = read.load_package(session['storyPackageId'], session['storyPackageVersion'])
    try:
        store.assert_session_package(sid, package)
        nodes, contract = store.lineage(sid, bid), store.contract(sid)
        status = route_status(store, sid, nodes, package)
        outline = build_outline(package, contract, nodes)
        intent = closing_intent(store, sid, nodes)
        closure = preparation(package, contract, nodes, 'active')
        plan = planning_projection(package, contract, nodes, intent, closure)
        binding = digest([outline['binding_digest'], intent, 'ending-evidence/1', catalog_version()])
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ReadError(409, 'route_history_unavailable', '终局所需的路线记录或故事包绑定无效') from error
    children = bool(store.connection.execute('SELECT 1 FROM branch_nodes WHERE session_id=? AND parent_id=? LIMIT 1', (sid, bid)).fetchone())
    outcome_ids = nodes[-1]['branchState'].get('characterOutcomeStates', {})
    outcomes = [dict(id=c['id'], name=c['name'], outcome=known_status(nodes, c['id']))
                for c in list(package['characters']) + nodes[-1]['branchState'].get('derivedCharacters', []) if c['id'] in outcome_ids]
    return dict(nodes=nodes, status=status, outline=outline, closure=closure, plan=plan,
                binding=binding, has_children=children, outcomes=outcomes)


def eligible(ctx):
    if ctx['status'] != 'active':
        raise ReadError(409, 'route_ended', '这条路线已经结束')
    if ctx['has_children']:
        raise ReadError(409, 'route_has_continuation', '请在路线末尾提交终局证据')
    if not ctx['plan'] or not ctx['plan']['ending_check']['ledger_ready']:
        raise ReadError(409, 'ending_not_ready', '收束意图或必要账本条件尚未满足')


def local_record(store, sid, bid):
    branch = store.branch(sid, bid)
    return records(store, sid, [branch]).get(bid, {})


def find_proposal(store, sid, bid, proposal_id):
    proposal = local_record(store, sid, bid).get('endingProposals', {}).get(proposal_id)
    if proposal is None:
        raise ReadError(404, 'ending_proposal_not_found', '当前分支没有这份终局提案')
    return proposal


def matches(read, store, sid, bid, proposal):
    try:
        ctx = context(read, store, sid, bid)
        eligible(ctx)
        return ctx['binding'] == proposal['binding_digest']
    except ReadError:
        return False


def proposal_view(read, store, sid, bid, proposal):
    result = copy.deepcopy(proposal)
    result['can_cancel'] = proposal['status'] == 'pending'
    if result['status'] in ('approved', 'pending') and not matches(read, store, sid, bid, proposal):
        result['status'] = 'stale'
    return result


def review_metrics(response, elapsed_ms):
    # A transport fallback is another provider request. Only the final response
    # has a retained receipt; missing earlier receipts must not become zero cost.
    observations = getattr(response, 'observations', [])
    calls = len(observations) or (0 if getattr(response, 'code', None) == 'model_call_limit' else 1)
    receipt = copy.copy(response)
    receipt.observations = []
    tokens, missing = reported_usage(receipt)
    unreported = max(0, calls - 1) + int(bool(calls) and missing)
    return dict(calls=calls, reported_tokens=tokens, unreported_calls=unreported,
                tokens=None if unreported else tokens, elapsed_ms=elapsed_ms,
                call_count_source='transport_observations' if observations else 'gateway_invocation')


def cancel(play, sid, bid, proposal_id):
    """Revoke a pending review without promising provider-side cancellation."""
    play.read.branch_view(sid, bid)
    with play._lock:
        store = SessionStore(str(play.database_path))
        try:
            with store.connection:
                store.connection.execute('BEGIN IMMEDIATE')
                record = local_record(store, sid, bid)
                proposal = find_proposal(store, sid, bid, proposal_id)
                if proposal['status'] == 'pending':
                    proposal['status'] = 'cancelled'
                    proposal['audit']['cancellation'] = dict(at=now(), reason='user_cancelled',
                                                            provider_cancelled=False)
                    record['endingProposals'][proposal_id] = proposal
                    save_record(store, sid, bid, record)
                elif proposal['status'] != 'cancelled':
                    raise ReadError(409, 'ending_review_finished', '审查已经完成，请刷新查看结果')
                return proposal_view(play.read, store, sid, bid, proposal)
        finally:
            store.close()


def validate_review(value, body):
    if not isinstance(value, dict) or value.get('decision') not in ('allow', 'reject', 'unknown'):
        raise ValueError('终局审查 decision 无效')
    checks = value.get('checks')
    if not isinstance(checks, dict) or set(checks) != set(CHECKS):
        raise ValueError('终局审查必须逐项覆盖四项检查')
    for item in checks.values():
        if not isinstance(item, dict) or type(item.get('passed')) is not bool:
            raise ValueError('终局审查结论格式无效')
        if not isinstance(item.get('reason'), str) or not item['reason'].strip():
            raise ValueError('终局审查缺少理由')
        if item['passed'] and (not isinstance(item.get('evidence'), str) or not item['evidence'].strip() or item['evidence'] not in body):
            raise ValueError('终局审查通过项缺少当前正文原句')
    if value['decision'] == 'allow' and not all(i['passed'] for i in checks.values()):
        raise ValueError('终局审查通过结论与检查项矛盾')
    return value


def propose(play, sid, bid, request_id, summary, quote):
    if not all(isinstance(s, str) and s.strip() for s in (request_id, summary, quote)):
        raise ReadError(422, 'invalid_ending_evidence', '终局提案需要请求标识、结局说明和正文原句')
    if len(request_id) > 200 or len(summary) > 2000 or len(quote) > 6000:
        raise ReadError(422, 'invalid_ending_evidence', '终局提案字段过长')
    play.read.branch_view(sid, bid)
    payload_digest = digest([request_id, summary, quote])
    with play._lock:
        store = SessionStore(str(play.database_path))
        try:
            with store.connection:
                store.connection.execute('BEGIN IMMEDIATE')
                record = local_record(store, sid, bid)
                for old in record.get('endingProposals', {}).values():
                    if old['request_id'] == request_id:
                        if old['payload_digest'] != payload_digest:
                            raise ReadError(409, 'request_conflict', '同一终局请求不能提交不同证据')
                        return proposal_view(play.read, store, sid, bid, old)
                ctx = context(play.read, store, sid, bid)
                eligible(ctx)
                body = ctx['nodes'][-1]['narrativeText']
                if quote not in body:
                    raise ReadError(422, 'invalid_ending_evidence', '结局引文必须来自当前分支正文')
                gateway = getattr(play._planner, 'gateway', None)
                if gateway is None:
                    raise ReadError(503, 'ending_reviewer_unavailable', '独立终局审查模型尚未配置')
                review_input = dict(ending_type=ctx['plan']['intended_type'], outcome_summary=summary,
                                    ending_quote=quote, narrative=body, goals=ctx['outline']['goals'],
                                    threads=ctx['outline']['threads'], conflicts=ctx['outline']['conflicts'],
                                    character_outcomes=ctx['outcomes'])
                audit = dict(model=gateway.model, prompt_version=catalog_version(), input=review_input,
                             raw_response=None, failure=None, metrics=None)
                proposal = dict(id=uuid.uuid4().hex, branch_id=bid, request_id=request_id, payload_digest=payload_digest,
                                binding_digest=ctx['binding'], ending_type=ctx['plan']['intended_type'],
                                outcome_summary=summary, ending_quote=quote, status='pending', created_at=now(),
                                review=None, audit=audit)
                record.setdefault('endingProposals', {})[proposal['id']] = proposal
                save_record(store, sid, bid, record)
        finally:
            store.close()
    # No DB or play lock while the provider is running. This is one review,
    # with no prose generation, repair loop or automatic ending commit.
    started = time.monotonic()
    response = None
    try:
        response = copy.copy(gateway).complete_json([
            {'role': 'system', 'content': render_prompt('reader.ending_review')},
            {'role': 'user', 'content': json.dumps(review_input, ensure_ascii=False)},
        ])
        audit['raw_response'] = response.raw_response
        review = validate_review(parse_json_content(response.content), body)
        proposal['review'] = review
        proposal['status'] = 'approved' if review['decision'] == 'allow' else 'rejected'
    except (LlmError, ValueError, TypeError, KeyError, AttributeError) as error:
        proposal['status'] = 'failed'
        audit['failure'] = dict(code=getattr(error, 'code', 'invalid_review'), message=str(error))
        if response is None:
            response = error
            audit['raw_response'] = getattr(error, 'raw_response', None)
    audit['observations'] = getattr(response, 'observations', [])
    audit['metrics'] = review_metrics(response, round((time.monotonic() - started) * 1000))
    proposal['audit'] = audit
    with play._lock:
        # A deleted save cannot be recreated by a late review response.
        play.read.branch_view(sid, bid)
        store = SessionStore(str(play.database_path))
        try:
            with store.connection:
                store.connection.execute('BEGIN IMMEDIATE')
                record = local_record(store, sid, bid)
                if proposal['id'] not in record.get('endingProposals', {}):
                    raise ReadError(409, 'ending_proposal_missing', '原终局提案已不存在')
                current = record['endingProposals'][proposal['id']]
                if current['status'] == 'cancelled':
                    audit['cancellation'] = current['audit']['cancellation']
                    audit['late_result'] = dict(status=proposal['status'], review=proposal['review'], received_at=now())
                    proposal.update(status='cancelled', review=None)
                record['endingProposals'][proposal['id']] = proposal
                save_record(store, sid, bid, record)
                return proposal_view(play.read, store, sid, bid, proposal)
        finally:
            store.close()


def commit(play, sid, bid, proposal_id):
    play.read.branch_view(sid, bid)
    with play._lock:
        store = SessionStore(str(play.database_path))
        try:
            with store.connection:
                store.connection.execute('BEGIN IMMEDIATE')
                record = local_record(store, sid, bid)
                proposal = find_proposal(store, sid, bid, proposal_id)
                receipt = record.get('receipt')
                if receipt and receipt.get('proposal_id') == proposal_id:
                    result = dict(status='completed', receipt=receipt)
                else:
                    ctx = context(play.read, store, sid, bid)
                    eligible(ctx)
                    if ctx['binding'] != proposal['binding_digest']:
                        raise ReadError(409, 'ending_proposal_stale', '终局证据或收束意图已变化，请重新提案')
                    if proposal['status'] != 'approved':
                        raise ReadError(409, 'ending_review_required', '终局提案尚未通过独立审查')
                    try:
                        review = validate_review(proposal['review'], ctx['nodes'][-1]['narrativeText'])
                    except ValueError as error:
                        raise ReadError(409, 'ending_review_required', str(error)) from error
                    if review['decision'] != 'allow':
                        raise ReadError(409, 'ending_review_required', '终局审查未通过')
                    closure = ctx['closure']
                    receipt = dict(ending_type=proposal['ending_type'], branch_id=bid, closed_at=now(),
                                   readiness=closure['readiness'], coverage=closure['coverage'],
                                   outstanding=closure['outstanding'], cleared=closure['cleared'], ending_written=True,
                                   proposal_id=proposal_id, binding_digest=ctx['binding'])
                    record['receipt'] = receipt
                    proposal['status'] = 'committed'
                    record['endingProposals'][proposal_id] = proposal
                    save_record(store, sid, bid, record)
                    prefs = preferences(store, sid)
                    prefs['ended'][bid] = 'completed'
                    save_preferences(store, sid, ended=prefs['ended'])
                    result = dict(status='completed', receipt=receipt)
            play.drafts.discard_parent(sid, bid)
            return result
        finally:
            store.close()
