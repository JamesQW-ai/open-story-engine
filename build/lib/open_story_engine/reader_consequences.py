"""Validated reader outcomes and goals, separate from frozen source-state patches."""
import copy
import hashlib
from .prompts import render_prompt
from . import reader_actions

VERSION = 'reader-consequences/2'


class ConsequenceEvidenceError(ValueError):
    pass


def enabled(package):
    return package.get('story', {}).get('entryModel', {}).get('policy') == 'official_unknown_reader/1'


def initial_goals(package, contract):
    entry = next((e for e in package['story'].get('entryModel', {}).get('entryPoints', [])
                  if e['id'] == contract.get('entryPointId')), {})
    # These are public opening questions, not later character biographies.
    titles = entry.get('openingThreads', [])
    return [dict(id='opening-goal-' + str(i + 1), title=title, status='active',
                 characterId=contract['persona'].get('sourceCharacterId'),
                 dependencies=[], source='opening', evidence=entry.get('openingSummary', ''), causeBranchId=None)
            for i, title in enumerate(titles)]


def goals_for(package, contract, state):
    return copy.deepcopy(state.get('goalLedger', initial_goals(package, contract)))


def planning_context(context):
    package, state = context['package'], context['parent']['branchState']
    return dict(player=context['contract']['persona'], state=state,
                goals=goals_for(package, context['contract'], state),
                characters=[{'id': c['id'], 'name': c['name']} for c in package['characters'] + state.get('derivedCharacters', [])],
                locations=[{'id': p['id'], 'name': p['name']} for p in package['locations'] + state.get('derivedLocations', [])],
                items=[{'id': p['id'], 'name': p['name']} for p in package['items'] + state.get('derivedItems', [])],
                criticalHistory=[n.get('narrativeText', '') for n in context['lineage']
                                 if any(n.get('consequenceUpdate', {}).get(k) for k in ('outcomes', 'stateChanges', 'goalUpdates'))][-3:],
                opening=context['contract'].get('openingContext', {}),
                hardRules=package['world'].get('globalConstraints', []),
                lockedHistory=[f for f in package['world'].get('immutableFacts', [])
                               if f['id'] in context['contract'].get('immutableFactRefs', [])],
                history=[{'text': n.get('narrativeText', '')[-3500:], 'outcome': n.get('readerOutcome')}
                         for n in context['lineage'][-3:]])


PLAN_RULES = render_prompt('consequences.plan')

REVIEW_RULES = render_prompt('consequences.review')

PLAN_RULES += reader_actions.PLAN_RULES
REVIEW_RULES += reader_actions.REVIEW_RULES


def validate_plan(data, requirements, context):
    if not isinstance(data, dict) or data.get('decision') not in ('ready', 'clarification_needed', 'conflict'):
        raise ValueError('结果规划缺少有效判定')
    if data['decision'] != 'ready':
        message = data.get('message')
        if not isinstance(message, str) or not message.strip():
            raise ValueError('结果规划须说明具体冲突或歧义')
        return data
    entries = data.get('requirements')
    if not isinstance(entries, dict) or set(entries) != set(requirements):
        raise ValueError('结果契约必须覆盖每项原始输入')
    for item in entries.values():
        if not isinstance(item, dict) or item.get('mode') not in ('result', 'attempt', 'constraint') or not isinstance(item.get('summary'), str) or not item['summary'].strip():
            raise ValueError('结果契约缺少行动性质或结果')
    if not isinstance(data.get('method'), str) or not data['method'].strip():
        raise ValueError('结果契约缺少可行的因果方法')
    data = copy.deepcopy(data)
    if isinstance(data.get('stateChanges'), list):
        data['stateChanges'] = [c for c in data['stateChanges'] if not (isinstance(c, dict) and 'before' in c and 'value' in c and c['before'] == c['value'])]
    reader_actions.validate_plan(data, context)
    state = context['parent']['branchState']
    people = {cid for cid, c in reader_actions.registry(context['package'], state, data['introductions']).items() if c['kind'] == 'character'}
    outcomes = data.get('outcomes')
    if not isinstance(outcomes, list) or len(outcomes) > 12:
        raise ValueError('人物后果格式无效')
    seen = set()
    for item in outcomes:
        if not isinstance(item, dict) or not isinstance(item.get('characterId'), str) or item['characterId'] not in people or item['characterId'] in seen:
            raise ValueError('人物后果引用无效或重复')
        cid = item['characterId']
        seen.add(cid)
        if item.get('status') not in ('alive', 'dead', 'departed') or item.get('permanence') not in ('temporary', 'permanent'):
            raise ValueError('人物后果类型无效')
        if item['status'] == 'dead' and item['permanence'] != 'permanent':
            raise ValueError('死亡必须永久保存，不能改成暂时下线')
        if not isinstance(item.get('requirementId'), str) or item['requirementId'] not in requirements or not isinstance(item.get('cause'), str) or not item['cause'].strip():
            raise ValueError('人物后果缺少行动来源与因果')
        old = state.get('characterOutcomeStates', {}).get(cid, {})
        if old.get('permanence') == 'permanent' and old.get('status') in ('dead', 'departed'):
            if any(old.get(k) != item[k] for k in ('status', 'permanence')):
                raise ValueError('永久后果不可撤销：' + cid)
    goals = {g['id']: g for g in goals_for(context['package'], context['contract'], state)}
    updates = data.get('goalUpdates')
    if not isinstance(updates, list) or len(updates) > 8:
        raise ValueError('目标变化格式无效')
    seen = set()
    for item in updates:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or (item.get('id') != 'new' and item.get('id') not in goals) or item.get('id') in seen:
            raise ValueError('目标引用无效或重复')
        seen.add(item['id'])
        if item.get('status') not in ('active', 'completed', 'transformed', 'abandoned'):
            raise ValueError('目标状态无效')
        if item['id'] == 'new' and item['status'] != 'active':
            raise ValueError('新目标必须为进行中')
        if item['id'] in goals and item['status'] != 'active' and item.get('title') != goals[item['id']]['title']:
            raise ValueError('结束旧目标时不得改写旧目标文字')
        if item['id'] in goals and item['status'] == 'active' and item.get('title') != goals[item['id']]['title']:
            raise ValueError('改变目标须转化或放下旧目标再新建，不能覆盖旧目标历史')
        if item['id'] in goals and goals[item['id']]['status'] != 'active':
            raise ValueError('已结束目标不能重置；请建立新目标并说明原因')
        for field in ('title', 'reason'):
            if not isinstance(item.get(field), str) or not 0 < len(item[field]) <= 180:
                raise ValueError('目标缺少简短文字或改变原因')
        deps = item.get('dependencies')
        if not isinstance(deps, list) or any(not isinstance(cid, str) or cid not in people for cid in deps):
            raise ValueError('目标依赖引用无效')
        unavailable = {cid for cid, o in projected_state(state, data).get('characterOutcomeStates', {}).items() if o.get('permanence') == 'permanent' and o.get('status') in ('dead', 'departed')}
        if item['status'] in ('active', 'transformed') and unavailable.intersection(deps):
            raise ValueError('目标不能依赖永久下线者亲自参与；提及死者不构成行动依赖')
        if item['status'] == 'transformed' and not (isinstance(item.get('successor'), str) and 0 < len(item['successor']) <= 180):
            raise ValueError('目标转化必须给出后续目标')
    result = copy.deepcopy(data)
    # Repeating an established outcome is history, not a new death to enact.
    result['outcomes'] = [o for o in result['outcomes'] if any(
        state.get('characterOutcomeStates', {}).get(o['characterId'], {}).get(k) != o[k]
        for k in ('status', 'permanence'))]
    result['goalUpdates'] = [g for g in result['goalUpdates'] if g['id'] not in goals or any(goals[g['id']].get(k) != g.get(k) for k in ('title', 'status', 'dependencies'))]
    return result


def projected_state(state, plan, package=None):
    state = reader_actions.project(state, plan, package) if package is not None else copy.deepcopy(state)
    for outcome in plan['outcomes']:
        state.setdefault('characterOutcomeStates', {})[outcome['characterId']] = copy.deepcopy(outcome)
        if outcome['status'] == 'departed':
            state.get('characterLocationIds', {}).pop(outcome['characterId'], None)
    return state


def validate_review(data, plan, body, reader_outcome):
    if data.get('checkedConsequences') is not True:
        raise ValueError('未完整核对人物后果和目标契约')
    for action in reader_outcome['actions']:
        if plan['requirements'][action['id']]['mode'] != 'attempt' and action['status'] != 'performed':
            raise ValueError('指定结果或限制未兑现，不能以尝试受阻通过：' + action['requirement'])
    paragraphs = {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}
    result = copy.deepcopy(plan)
    collections = [('outcomes', 'outcomeEvidence'), ('goalUpdates', 'goalEvidence'), ('stateChanges', 'changeEvidence')]
    for collection, field in collections + [('newEntities', 'introductionEvidence')]:
        entries = reader_actions.introductions(result) if collection == 'newEntities' else result[collection]
        evidence = data.get(field)
        if not isinstance(evidence, list) or len(evidence) != len(entries):
            raise ConsequenceEvidenceError(field + '应有' + str(len(entries)) + '项，须与' + collection + '逐项对应，相同段号也必须重复填写')
        for item, ref in zip(entries, evidence):
            refs = [ref] if isinstance(ref, str) else ref
            if not isinstance(refs, list) or not 1 <= len(refs) <= 5 or any(not isinstance(r, str) or r not in paragraphs for r in refs):
                raise ConsequenceEvidenceError('后果证据须引用1至5个有效段落编号')
            positions = [int(r[1:]) for r in refs]
            # Keep the original contiguous passage so commit can verify it verbatim.
            quote = '\n\n'.join(paragraphs[f'P{i}'] for i in range(min(positions), max(positions) + 1))
            if not quote or len(quote) < 4:
                raise ConsequenceEvidenceError('后果证据必须引用本回合正文段落')
            item['evidence'] = quote
    return result


def public_summary(update, package, state=None):
    names = {cid: c['name'] for cid, c in reader_actions.registry(package, state or {}, update['introductions']).items()}
    labels = {'dead': '已死亡', 'departed': '已离队', 'alive': '已恢复在场'}
    parts = [names[o['characterId']] + labels[o['status']] + ('，不会再回到这条路线' if o['status'] == 'departed' and o['permanence'] == 'permanent' else '') for o in update['outcomes']]
    if update['goalUpdates']:
        parts.append('你的目标已有变化，可在故事手记中查看')
    return '；'.join(parts)


def commit_consequences(context, state, result, branch_id):
    """Only reviewed prose can authorize these reserved state changes."""
    update = result.get('consequenceUpdate')
    if update is None:
        return state
    if not enabled(context['package']):
        raise ValueError('当前故事不支持人物后果契约')
    from .api_reader_quality import action_requirements
    action = context.get('playerDirection') or result['actionIntent']['input']
    validate_plan(update, action_requirements(action), context)
    if update.get('decision') != 'ready' or result.get('consequenceReview') != VERSION:
        raise ValueError('未校验的结果不能提交')
    if result.get('reviewedNarrativeSha256') != hashlib.sha256(result['narrativeText'].encode()).hexdigest():
        raise ValueError('提交正文与已审查全文不符，须重新核对')
    reader_actions.validate_authority(result.get('authorityReview'), update, action, context['parent']['narrativeText'])
    events = reader_actions.validate_observations({'events': result.get('observedEvents')}, result['narrativeText'])
    reader_actions.validate_events({'eventChecks': result.get('eventChecks')}, events, update, context['parent']['branchState'], context['package'])
    for item in update['outcomes'] + update['goalUpdates'] + update['stateChanges'] + reader_actions.introductions(update):
        if not isinstance(item.get('evidence'), str) or item['evidence'] not in result['narrativeText']:
            raise ValueError('结果证据与提交正文不符')
    next_state = reader_actions.project(state, update, context['package'])
    ledger = next_state.setdefault('characterOutcomeStates', {})
    for item in update['outcomes']:
        cid = item['characterId']
        old = ledger.get(cid)
        if old and old.get('status') == item['status'] and old.get('permanence') == item['permanence']:
            continue  # Preserve the first cause, not a later mention.
        ledger[cid] = {**item, 'causeBranchId': branch_id}
        if item['status'] == 'departed':
            next_state.get('characterLocationIds', {}).pop(cid, None)
    goals = goals_for(context['package'], context['contract'], state)
    for i, item in enumerate(update['goalUpdates']):
        new_id = 'goal-' + hashlib.sha256((branch_id + ':' + str(i)).encode()).hexdigest()[:16]
        record = {**item, 'characterId': context['contract']['persona'].get('sourceCharacterId'),
                  'source': 'player_branch', 'causeBranchId': branch_id}
        if item['id'] == 'new':
            goals.append({**record, 'id': new_id})
        else:
            old = next(g for g in goals if g['id'] == item['id'])
            if item['status'] != 'active' and item['title'] != old['title']:
                raise ValueError('结束旧目标时不得改写旧目标文字')
            old.update(record)
        if item['status'] == 'transformed':
            goals.append({**record, 'id': new_id, 'title': item['successor'], 'status': 'active', 'previousGoalId': item['id']})
    unavailable = {cid for cid, o in ledger.items() if o.get('status') in ('dead', 'departed') and o.get('permanence') == 'permanent'}
    for goal in goals:
        if goal['status'] == 'active' and unavailable.intersection(goal['dependencies']):
            raise ValueError('当前目标仍依赖已永久下线人物，需要转化或放下')
    next_state['goalLedger'] = goals
    from .branch_ledger import append_branch_ledger
    changes = []
    for key in ('characterOutcomeStates', 'goalLedger', reader_actions.STATE_KEY, 'characterLocationIds', 'playerLocationId', 'itemOwnerCharacterIds', 'itemLocationIds'):
        if state.get(key) != next_state.get(key):
            changes.append(dict(kind='event', operation='changed', entityId='state:' + key,
                                summary='已核对正文的行动结果与持续状态变化', before=state.get(key), after=next_state.get(key)))
    append_branch_ledger(next_state, changes, {'kind': 'player_direction', 'ref': branch_id, 'nodeRef': context['parent']['sourceNodeRef']})
    return next_state


def filter_directions(directions, package, state):
    """Conservatively remove source actions depending on unavailable actors."""
    unavailable = {cid for cid, o in state.get('characterOutcomeStates', {}).items()
                   if o.get('status') in ('dead', 'departed') and o.get('permanence') == 'permanent'}
    names = [c['name'] for c in package['characters'] if c['id'] in unavailable]
    goal_ids = {d['id'] for d in (goal_directions(state) or [])}
    return [d for d in directions if (d.get('id') in goal_ids or not any(name in (d.get('title', '') + d.get('summary', '')) for name in names))
            and not unavailable.intersection(d.get('statePatch', {}).get('characterLocationIds', {}))]


def goal_directions(state):
    goals = state.get('goalLedger', [])
    if not any(g.get('source') == 'player_branch' for g in goals):
        return None
    progress = state.get('freeTextProgress')
    if not isinstance(progress, int):
        return None
    return [dict(id='goal-direction-' + g['id'] + '-' + str(progress + 1),
                 title='推进目标：' + g['title'],
                 summary='围绕“' + g['title'] + '”，依据眼前条件采取下一步行动，不恢复已放下的目标。',
                 statePatch={'freeTextProgress': progress + 1}, isFreeText=True)
            for g in goals if g['status'] == 'active']


def final_state_projection(state):
    return {k: copy.deepcopy(state.get(k, {} if k != 'playerLocationId' else None))
            for k in ('playerLocationId', 'itemOwnerCharacterIds', 'itemLocationIds')}



def validate_final_state(data, expected):
    observed = data.get('finalState')
    if not isinstance(observed, dict) or set(observed) != set(final_state_projection(expected)):
        raise ConsequenceEvidenceError('缺少完整的finalState，须根据正文提取玩家地点、物品归属和地面物品，未改变项继承已有状态')
    for field, value in final_state_projection(expected).items():
        if observed[field] != value:
            raise ValueError('正文出现未经授权的状态变化：' + field + '。本回合结尾必须保持' + str(value) + '，不要在用户所选行动后擅自移动或交出物品。')
