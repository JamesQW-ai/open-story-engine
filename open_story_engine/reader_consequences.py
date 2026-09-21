"""Validated reader outcomes and goals, separate from frozen source-state patches."""
import copy
import hashlib
import json
import re
from .prompts import render_prompt
from . import reader_actions, reader_threads, item_lifecycle

VERSION = 'reader-consequences/8'


class ConsequenceEvidenceError(ValueError):
    pass


def _committed_node_summary(node, limit=700):
    """Build a bounded handoff summary without exposing prior prose."""
    node = node or {}
    outcome = node.get('readerOutcome') or {}
    action = outcome.get('action') if isinstance(outcome, dict) else {}
    action = action if isinstance(action, dict) else {}
    values = [node.get('summary'), node.get('playerDirection'),
              (node.get('selectedDirection') or {}).get('title') if isinstance(node.get('selectedDirection'), dict) else None,
              action.get('summary')]
    text = '；'.join(str(value).strip() for value in values if isinstance(value, str) and value.strip())
    return text[:limit]


def _summary_outcome(node):
    """Keep planner history factual without carrying paragraph quotations."""
    outcome = node.get('readerOutcome') or {}
    action = outcome.get('action') if isinstance(outcome, dict) else {}
    action = action if isinstance(action, dict) else {}
    clues = outcome.get('clues') if isinstance(outcome, dict) else []
    return {
        'action': {key: action[key] for key in ('status', 'summary') if key in action},
        'clues': [
            {'summary': item['summary']} for item in (clues or [])
            if isinstance(item, dict) and isinstance(item.get('summary'), str)
        ][:3],
    }


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


def prompt_state(state):
    # The append-only transaction ledger repeats before/after snapshots. Models
    # need the current projection; validation/commit still use the full state.
    return {k: v for k, v in state.items() if k != 'branchLedger'}


def planning_context(context):
    # Lazy import: route_outline depends on this module for initial goals.
    from .route_closure import closure_projection
    from .route_lifecycle import planning_projection
    package, state = context['package'], context['parent']['branchState']
    nodes = context.get('lineage', [context['parent']])
    closure = closure_projection(package, context['contract'], nodes)
    return dict(player=context['contract']['persona'], state=prompt_state(state),
                goals=goals_for(package, context['contract'], state),
                threads=reader_threads.threads_for(package, context['contract'], state),
                # Outstanding closeout obligations from the same ancestry; not
                # an ending claim and not a substitute for goal/thread records.
                closure=closure,
                closing=planning_projection(package, context['contract'], nodes, context.get('closingIntent'), closure),
                characters=[{'id': c['id'], 'name': c['name']} for c in package['characters'] + state.get('derivedCharacters', [])],
                locations=[{'id': p['id'], 'name': p['name']} for p in package['locations'] + state.get('derivedLocations', [])],
                items=[{'id': p['id'], 'name': p['name']} for p in package['items'] + state.get('derivedItems', [])],
                criticalHistory=[_committed_node_summary(n) for n in context['lineage']
                                 if any(n.get('consequenceUpdate', {}).get(k)
                                        for k in ('outcomes', 'stateChanges', 'goalUpdates', 'threadUpdates'))][-3:],
                opening=context['contract'].get('openingContext', {}),
                hardRules=package['world'].get('globalConstraints', []),
                lockedHistory=[f for f in package['world'].get('immutableFacts', [])
                               if f['id'] in context['contract'].get('immutableFactRefs', [])],
                history=[{
                    'summary': _committed_node_summary(n),
                    'outcome': _summary_outcome(n),
                } for n in context['lineage'][-3:]])


PLAN_RULES = render_prompt('consequences.plan')

REVIEW_RULES = render_prompt('consequences.review')

PLAN_RULES += render_prompt('consequences.threads_plan')
REVIEW_RULES += render_prompt('consequences.threads_review')

PLAN_RULES += reader_actions.PLAN_RULES
REVIEW_RULES += reader_actions.REVIEW_RULES


_MOVEMENT_INTENT = re.compile(
    r'(?:离开|离去|出发|前往|返回|回到|走向|走进|走出|走入|撤离|下山|进山|迈步|奔向|赶往)'
)
_MOVEMENT_NEGATION = re.compile(r'(?:不|不要|不必|暂不|暂时不|不再|未曾|没有|拒绝)$')

# A permanent-destruction result is an abstract player intent.  These verbs
# describe a physical manifestation chosen by the planner; unless the player
# used the verb, it remains a pending narrative option and cannot become a
# player-authorized step or state-change reason.
_CONCRETE_DESTRUCTION = re.compile(
    r'(?:取出|抽出|拿出|撕毁|撕碎|撕裂|撕扯|扯碎|揉散|揉烂|揉碎|揉搓|搓成|烧毁|焚烧|焚毁|点燃|折断|折碎|砸碎|摔碎|剪碎|磨碎|掰断|踩碎|碾碎)'
)
_PERMANENT_DESTRUCTION = re.compile(r'(?:永久|彻底|完全|不可再).{0,5}(?:损毁|销毁|毁掉|毁坏|破坏)')
_DESTRUCTION_ASSERTION = re.compile(r'(?<!未)(?<!尚未)(?<!没有)(?<!不)(?:已|已经|完成|发生|被).{0,8}(?:损毁|销毁|毁掉|毁坏|破坏)|(?:永久|彻底|完全)(?:地)?(?:损毁|销毁|毁掉|毁坏)')
_WITNESS_CLAIM = re.compile(r'(?:目睹|亲眼|看见|看到|看清|见证|知晓|确认)')
_ROLE_WITNESS = re.compile(r'(?:守门弟子|门卫|执事|旁人|弟子|证人)')


def _positive_destruction_assertion(text):
    """Match an affirmative destruction claim, excluding ``尚未/未`` claims."""
    if not isinstance(text, str):
        return None
    for match in _DESTRUCTION_ASSERTION.finditer(text):
        prefix = text[max(0, match.start() - 8):match.start()]
        if re.search(r'(?:尚未|未|没有|不曾|不能|无法)\s*$', prefix):
            continue
        return match
    return None


def _canonical_plan_texts(data):
    """Return ``(JSON path, field, value)`` for executable plan text.

    ``narrativeOptions`` is deliberately absent: it is a pending candidate
    channel and must never be interpreted as an executable plan field.
    """
    entries = []
    requirements = data.get('requirements') if isinstance(data.get('requirements'), dict) else {}
    for key, item in requirements.items():
        if isinstance(item, dict):
            for field in ('summary', 'playerIntent', 'method'):
                if isinstance(item.get(field), str):
                    entries.append((f'$.requirements[{json.dumps(key, ensure_ascii=False)}].{field}', f'requirements.{field}', item[field]))
    player_intent = data.get('playerIntent')
    if isinstance(player_intent, str):
        entries.append(('$.playerIntent', 'playerIntent', player_intent))
    elif isinstance(player_intent, dict):
        for field in ('summary', 'action', 'method'):
            if isinstance(player_intent.get(field), str):
                entries.append((f'$.playerIntent.{field}', f'playerIntent.{field}', player_intent[field]))
    if isinstance(data.get('method'), str):
        entries.append(('$.method', 'method', data['method']))
    for index, step in enumerate(data.get('steps', []) if isinstance(data.get('steps'), list) else []):
        if isinstance(step, dict):
            for field in ('action', 'method', 'playerIntent'):
                if isinstance(step.get(field), str):
                    entries.append((f'$.steps[{index}].{field}', f'steps.{field}', step[field]))
    for index, change in enumerate(data.get('stateChanges', []) if isinstance(data.get('stateChanges'), list) else []):
        if isinstance(change, dict):
            for field in ('reason', 'action', 'method', 'playerIntent'):
                if isinstance(change.get(field), str):
                    entries.append((f'$.stateChanges[{index}].{field}', f'stateChanges.{field}', change[field]))
    scene = data.get('scenePlan')
    if isinstance(scene, dict):
        for key in ('start', 'outcome', 'stop', 'lengthReason'):
            if isinstance(scene.get(key), str):
                entries.append((f'$.scenePlan.{key}', f'scenePlan.{key}', scene[key]))
        for key in ('beats', 'knowledge', 'observationLimits'):
            values = scene.get(key)
            if not isinstance(values, list):
                continue
            for index, entry in enumerate(values):
                if isinstance(entry, str):
                    entries.append((f'$.scenePlan.{key}[{index}]', f'scenePlan.{key}', entry))
                elif isinstance(entry, dict):
                    for field in ('purpose', 'statement'):
                        if isinstance(entry.get(field), str):
                            entries.append((f'$.scenePlan.{key}[{index}].{field}', f'scenePlan.{key}.{field}', entry[field]))
    return entries


def _validate_player_intent_boundaries(data, requirements, context):
    """Keep irreversible item plans within the words and evidence of the turn."""
    original = '；'.join(str(value) for value in requirements.values())
    if not _PERMANENT_DESTRUCTION.search(original):
        return
    if not any(isinstance(change, dict) and change.get('attribute') == item_lifecycle.ATTRIBUTE
               and change.get('value') is True for change in data.get('stateChanges', [])):
        raise ValueError('玩家明确要求永久损毁，但计划没有登记不可逆的道具结果')
    # ``requirements`` is the normalized playerIntent.  It must not become a
    # second, more specific command than the original input.
    violations = []
    canonical_texts = _canonical_plan_texts(data)
    for path, field, text in canonical_texts:
        extra = _CONCRETE_DESTRUCTION.search(text)
        if extra and not _CONCRETE_DESTRUCTION.search(original):
            violations.append({'path': path, 'field': field, 'value': text,
                               'rule': '具体动作须留在pending narrativeOptions，不能进入canonical plan；命中=' + extra.group()})

    # A witness/knowledge claim needs a current access path.  The planner has
    # no authority to turn the role in the scene into an eyewitness merely by
    # mentioning it in outcome/method text.
    names = {character.get('name') for character in context.get('package', {}).get('characters', [])
             if isinstance(character, dict) and character.get('name')}
    roles = names | {'守门弟子', '门卫', '执事', '旁人', '弟子', '证人'}
    for path, field, text in canonical_texts:
        uncertain = re.search(r'(?:是否|未知|不知|不知道|未确认|尚未确认|无法判断|没有依据|无依据|不写|不假定|不得)', text)
        if _WITNESS_CLAIM.search(text) and not uncertain and any(role in text for role in roles if role):
            if not (_WITNESS_CLAIM.search(original) and any(role in original for role in roles if role)):
                violations.append({'path': path, 'field': field, 'value': text,
                                   'rule': 'NPC目睹或确认损毁缺少当前证据或依据；不得进入canonical plan'})

    if violations:
        raise ValueError('canonical/pending字段边界错误：' + json.dumps(violations, ensure_ascii=False))

    scene = data.get('scenePlan') or {}
    for item in scene.get('knowledge', []) if isinstance(scene.get('knowledge'), list) else []:
        if not isinstance(item, dict) or not _positive_destruction_assertion(item.get('statement', '')):
            continue
        sources = item.get('sources') if isinstance(item.get('sources'), list) else []
        quoted = '；'.join(str(source.get('quote', '')) for source in sources if isinstance(source, dict))
        if not _positive_destruction_assertion(quoted):
            raise ValueError('当前持有证据只能证明持有，不能证明道具已经损毁：' + item.get('statement', '')[:80])


def _has_player_movement_intent(requirements, context):
    """Detect an explicit player departure without treating refusal to move as one."""
    text = '；'.join(str(value) for value in requirements.values())
    player_name = context['contract']['persona'].get('name')
    other_names = {character['name'] for character in context['package'].get('characters', [])
                   if character.get('name') and character.get('name') != player_name}
    for clause in re.split(r'[；;。！？!?\n]+', text):
        match = _MOVEMENT_INTENT.search(clause)
        if not match:
            continue
        if any(name in clause for name in other_names) and (not player_name or player_name not in clause):
            continue
        prefix = clause[:match.start()].replace(' ', '')
        if _MOVEMENT_NEGATION.search(prefix[-8:]):
            continue
        return True
    return False


def _closing_phase(context):
    """Return whether a valid closeout intent has reached the closing phase."""
    intent = context.get('closingIntent')
    if not intent:
        return False
    try:
        from .route_closure import closure_projection
        from .route_lifecycle import planning_projection
        nodes = context.get('lineage', [context['parent']])
        closure = closure_projection(context['package'], context['contract'], nodes)
        projection = planning_projection(context['package'], context['contract'], nodes, intent, closure)
        return bool(projection and projection.get('phase') == 'closing')
    except (ValueError, TypeError, KeyError, AttributeError):
        # An invalid or incomplete ledger remains preparing/unknown. Do not
        # silently treat it as a permission to close, but keep normal play
        # available so the caller can repair the ledger with evidence.
        return False


def validate_plan(data, requirements, context):
    if not isinstance(data, dict) or data.get('decision') not in ('ready', 'clarification_needed', 'conflict'):
        raise ValueError('结果规划缺少有效判定')
    if data['decision'] != 'ready':
        message = data.get('message')
        if not isinstance(message, str) or not message.strip():
            raise ValueError('结果规划须说明具体冲突或歧义')
        return data
    if data.get('readingIntent', 'normal') not in ('brief', 'normal'):
        raise ValueError('readingIntent 须为 brief 或 normal')
    entries = data.get('requirements')
    if not isinstance(entries, dict) or set(entries) != set(requirements):
        raise ValueError('结果契约必须覆盖每项原始输入')
    for item in entries.values():
        if not isinstance(item, dict) or item.get('mode') not in ('result', 'attempt', 'constraint') or not isinstance(item.get('summary'), str) or not item['summary'].strip():
            raise ValueError('结果契约缺少行动性质或结果')
    if not isinstance(data.get('method'), str) or not data['method'].strip():
        raise ValueError('结果契约缺少可行的因果方法')
    data = copy.deepcopy(data)
    closing_phase = _closing_phase(context)
    if isinstance(data.get('stateChanges'), list):
        data['stateChanges'] = [c for c in data['stateChanges'] if not (isinstance(c, dict) and 'before' in c and 'value' in c and c['before'] == c['value'])]
    reader_actions.validate_plan(data, context)
    _validate_player_intent_boundaries(data, requirements, context)
    player_id = context['contract']['persona'].get('sourceCharacterId')
    player_location_change = next((change for change in data['stateChanges']
                                   if change.get('entityId') == player_id
                                   and change.get('attribute') == 'locationId'), None)
    if _has_player_movement_intent(requirements, context) and player_location_change is None:
        raise ValueError('玩家明确要求移动，但结果契约没有登记地点变化')
    state = context['parent']['branchState']
    people = {cid for cid, c in reader_actions.registry(context['package'], state, data['introductions']).items() if c['kind'] == 'character'}
    known = reader_actions.registry(context['package'], state, data['introductions'])
    outcomes = data.get('outcomes')
    if not isinstance(outcomes, list) or len(outcomes) > 12:
        raise ValueError('人物后果格式无效')
    seen = set()
    for item in outcomes:
        if not isinstance(item, dict) or not isinstance(item.get('characterId'), str) or item['characterId'] not in people or item['characterId'] in seen:
            raise ValueError('人物后果引用无效或重复')
        cid = item['characterId']
        seen.add(cid)
        if item.get('status') not in ('alive', 'dead', 'departed', 'missing', 'injured') or item.get('permanence') not in ('temporary', 'permanent'):
            raise ValueError('人物后果类型无效')
        if item['status'] in ('missing', 'injured') and item['permanence'] != 'temporary':
            raise ValueError('失踪与受伤记录当前已确认情况，不得推断为永久下线')
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
        if closing_phase and (item['id'] == 'new' or item.get('status') == 'transformed'):
            raise ValueError('收束中不得建立新目标或转化为新的长期目标；只能处理现有账本条目')
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
        item_lifecycle.validate_dependencies(item, goals.get(item['id']), data, known, state,
                                             item['status'] in ('active', 'transformed'))
        unavailable = {cid for cid, o in projected_state(state, data).get('characterOutcomeStates', {}).items() if o.get('permanence') == 'permanent' and o.get('status') in ('dead', 'departed')}
        if item['status'] in ('active', 'transformed') and unavailable.intersection(deps):
            raise ValueError('目标不能依赖永久下线者亲自参与；提及死者不构成行动依赖')
        if item['status'] == 'transformed' and not (isinstance(item.get('successor'), str) and 0 < len(item['successor']) <= 180):
            raise ValueError('目标转化必须给出后续目标')
    reader_threads.validate_updates(data, {**context, '_closing_phase': closing_phase})
    result = copy.deepcopy(data)
    # Repeating an established outcome is history, not a new death to enact.
    result['outcomes'] = [o for o in result['outcomes'] if any(
        state.get('characterOutcomeStates', {}).get(o['characterId'], {}).get(k) != o[k]
        for k in ('status', 'permanence'))]
    result['goalUpdates'] = [g for g in result['goalUpdates'] if g['id'] not in goals
        or any(goals[g['id']].get(k) != g.get(k) for k in ('title', 'status', 'dependencies'))
        or item_lifecycle.dependencies(g, goals[g['id']]) != item_lifecycle.dependencies(goals[g['id']])]
    return result


def projected_state(state, plan, package=None):
    state = reader_actions.project(state, plan, package) if package is not None else copy.deepcopy(state)
    for outcome in plan['outcomes']:
        state.setdefault('characterOutcomeStates', {})[outcome['characterId']] = copy.deepcopy(outcome)
        if outcome['status'] in ('departed', 'missing'):
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
    if result.get('threadUpdates'):
        collections.append(('threadUpdates', 'threadEvidence'))
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
    labels = {'dead': '已死亡', 'departed': '已离队', 'alive': '已确认存活', 'missing': '已确认失踪', 'injured': '已受伤'}
    parts = [names[o['characterId']] + labels[o['status']] + ('，不会再回到这条路线' if o['status'] == 'departed' and o['permanence'] == 'permanent' else '') for o in update['outcomes']]
    parts.extend(names[c['entityId']] + '已永久损毁' for c in update['stateChanges']
                 if c['attribute'] == item_lifecycle.ATTRIBUTE and c['value'] is True)
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
    for item in update['outcomes'] + update['goalUpdates'] + update.get('threadUpdates', []) + update['stateChanges'] + reader_actions.introductions(update):
        if not isinstance(item.get('evidence'), str) or not item['evidence'].strip() or item['evidence'] not in result['narrativeText']:
            raise ValueError('结果证据与提交正文不符')
    next_state = reader_actions.project(state, update, context['package'])
    ledger = next_state.setdefault('characterOutcomeStates', {})
    for item in update['outcomes']:
        cid = item['characterId']
        old = ledger.get(cid)
        if old and old.get('status') == item['status'] and old.get('permanence') == item['permanence']:
            continue  # Preserve the first cause, not a later mention.
        ledger[cid] = {**item, 'causeBranchId': branch_id}
        if item['status'] in ('departed', 'missing'):
            next_state.get('characterLocationIds', {}).pop(cid, None)
    goals = goals_for(context['package'], context['contract'], state)
    for i, item in enumerate(update['goalUpdates']):
        new_id = 'goal-' + hashlib.sha256((branch_id + ':' + str(i)).encode()).hexdigest()[:16]
        record = {**item, 'characterId': context['contract']['persona'].get('sourceCharacterId'),
                  'source': 'player_branch', 'causeBranchId': branch_id}
        prior = next((g for g in goals if g['id'] == item['id']), None)
        if 'itemDependencies' in item or prior and 'itemDependencies' in prior:
            record['itemDependencies'] = copy.deepcopy(item_lifecycle.dependencies(item, prior))
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
    next_state[reader_threads.STATE_KEY] = reader_threads.commit(
        context['package'], context['contract'], state, update.get('threadUpdates', []), branch_id)
    from .branch_ledger import append_branch_ledger
    changes = []
    for key in ('characterOutcomeStates', 'goalLedger', reader_threads.STATE_KEY, reader_actions.STATE_KEY, 'characterLocationIds', 'playerLocationId', 'itemOwnerCharacterIds', 'itemLocationIds'):
        if state.get(key) != next_state.get(key):
            changes.append(dict(kind='event', operation='changed', entityId='state:' + key,
                                summary='已核对正文的行动结果与持续状态变化', before=state.get(key), after=next_state.get(key)))
    append_branch_ledger(next_state, changes, {'kind': 'player_direction', 'ref': branch_id, 'nodeRef': context['parent']['sourceNodeRef']})
    return next_state


def filter_directions(directions, package, state):
    """Conservatively remove source actions depending on unavailable actors."""
    unavailable = {cid for cid, o in state.get('characterOutcomeStates', {}).items()
                   if o.get('status') == 'missing' or o.get('status') in ('dead', 'departed') and o.get('permanence') == 'permanent'}
    names = [c['name'] for c in package.get('characters', []) + state.get('derivedCharacters', []) if c['id'] in unavailable]
    goal_ids = {d['id'] for d in (goal_directions(state) or [])}
    return [d for d in directions if (d.get('id') in goal_ids or not any(name in (d.get('title', '') + d.get('summary', '')) for name in names))
            and (not d.get('id', '').startswith('goal-direction-') or d['id'] in goal_ids)
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
            for g in goals if g['status'] == 'active'
            and not any(item_lifecycle.destroyed(state, iid) for iid in item_lifecycle.dependencies(g))]


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
