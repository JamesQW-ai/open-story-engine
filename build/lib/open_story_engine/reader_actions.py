"""Scene-independent action authority and evidence-bound entity changes."""
import math

from .prompts import render_prompt

VERSION = 'reader-actions/1'
STATE_KEY = 'readerEntityStates'

AUTHORITY_RULES = render_prompt('actions.authority')

PLAN_RULES = render_prompt('actions.plan')

OBSERVE_RULES = render_prompt('actions.observe')

REVIEW_RULES = render_prompt('actions.review')


def registry(package, state, introductions=None):
    result = {}
    for plural, derived, kind in (('characters', 'derivedCharacters', 'character'),
                                 ('items', 'derivedItems', 'item'), ('locations', 'derivedLocations', 'location')):
        for item in package[plural] + state.get(derived, []) + (introductions or {}).get(plural, []):
            result[item['id']] = {**item, 'kind': kind}
    return result


def validate_authority(data, plan, user_input, previous):
    if not isinstance(data, dict) or data.get('decision') not in ('allow', 'revise') or not isinstance(data.get('issues'), list):
        raise ValueError('缺少独立行动授权审查')
    problems = [str(issue) for issue in data['issues']]
    for key, id_key, expected in (('checks', 'stepId', {s['id'] for s in plan['steps']}),
                                  ('stateChecks', 'changeId', {c['id'] for c in plan['stateChanges']})):
        entries = data.get(key)
        if not isinstance(entries, list) or len(entries) != len(expected) or any(not isinstance(e, dict) or not isinstance(e.get(id_key), str) for e in entries) or {e[id_key] for e in entries} != expected:
            raise ValueError('授权审查须覆盖每一步骤与持续状态')
        for entry in entries:
            if entry.get('authorized') is not True:
                problems.append(str(entry.get('reason', '未说明授权')))
            if key == 'checks':
                basis, quote = entry.get('basis'), entry.get('quote')
                if basis not in ('player_input', 'prior_fact', 'causal_reaction') or not isinstance(quote, str) or not quote:
                    raise ValueError('行动授权缺少原始证据')
                source = previous if basis == 'prior_fact' else user_input if basis == 'player_input' else user_input + '\n' + previous
                if quote not in source:
                    raise ValueError('行动授权引文不在原输入或前文中')
    if problems or data['decision'] != 'allow':
        raise ValueError('行动计划越权：' + '；'.join(problems[:4]))
    return data


def value_at(state, entity, attribute, kind):
    if attribute == 'locationId' and kind in ('character', 'item'):
        return state.get('characterLocationIds' if kind == 'character' else 'itemLocationIds', {}).get(entity)
    if attribute == 'ownerCharacterId' and kind == 'item':
        return state.get('itemOwnerCharacterIds', {}).get(entity)
    return state.get(STATE_KEY, {}).get(entity, {}).get(attribute)


def introductions(plan):
    return [e for k in ('characters', 'items', 'locations') for e in plan['introductions'][k]]


def attribute_vocabulary(package, state, plan):
    """Expose attribute names, never desired values, to the independent reader."""
    vocabulary = {(c['entityId'], c['attribute']) for c in plan['stateChanges']}
    for entity, item in registry(package, state, plan['introductions']).items():
        fields = ('locationId', 'ownerCharacterId') if item['kind'] == 'item' else ('locationId', 'outcome') if item['kind'] == 'character' else ()
        vocabulary.update((entity, field) for field in fields)
        vocabulary.update((entity, field) for field in state.get(STATE_KEY, {}).get(entity, {}))
    return [dict(entityId=entity, attribute=attribute) for entity, attribute in sorted(vocabulary)]


def validate_plan(plan, context):
    from .cocreation import validate_branch_additions
    steps = plan.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 24:
        raise ValueError('通用行动契约须提供1至24个因果步骤')
    additions = plan.get('introductions')
    if not isinstance(additions, dict) or set(additions) != {'characters', 'items', 'locations'}:
        raise ValueError('通用行动契约缺少完整的introductions')
    validate_branch_additions(context['package'], context['parent']['branchState'], additions)
    if len(introductions(plan)) > 12:
        raise ValueError('单回合引入实体过多')
    known = registry(context['package'], context['parent']['branchState'], additions)
    seen = set()
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get('id'), str) or not step['id'] or step['id'] in seen:
            raise ValueError('行动步骤ID无效或重复')
        refs = step.get('requirementIds')
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in plan['requirements'] for ref in refs):
            raise ValueError('步骤必须追溯到原输入要求')
        if not isinstance(step.get('actorId'), str) or (step['actorId'] != 'environment' and known.get(step['actorId'], {}).get('kind') != 'character'):
            raise ValueError('行动主体未登记')
        if step.get('authority') not in ('player', 'reaction') or not isinstance(step.get('action'), str) or not step['action'].strip():
            raise ValueError('行动缺少授权性质或动作')
        if step['authority'] == 'reaction' and (not isinstance(step.get('causeStepId'), str) or step['causeStepId'] not in seen):
            raise ValueError('NPC或环境反应必须由前序步骤引起')
        if step['authority'] == 'player' and step.get('actorId') != context['contract']['persona'].get('sourceCharacterId'):
            raise ValueError('玩家授权步骤的主体必须是玩家')
        seen.add(step['id'])
    for entity in introductions(plan):
        if not isinstance(entity.get('sourceStepId'), str) or entity['sourceStepId'] not in seen:
            raise ValueError('新增实体必须有实际形成或出现的步骤')
    changes = plan.get('stateChanges')
    if not isinstance(changes, list) or len(changes) > 32:
        raise ValueError('通用状态变化格式无效')
    touched, ids = set(), set()
    for c in changes:
        if not isinstance(c, dict) or not isinstance(c.get('id'), str) or not c['id'] or c['id'] in ids:
            raise ValueError('状态变化ID无效或重复')
        ids.add(c['id'])
        entity, attr = c.get('entityId'), c.get('attribute')
        if not isinstance(entity, str) or entity not in known or not isinstance(attr, str) or not 0 < len(attr) <= 60:
            raise ValueError('状态变化实体或属性无效')
        if attr in ('id', 'kind', 'name', 'characterOutcomeStates', 'goalLedger', STATE_KEY):
            raise ValueError('通用属性不能覆盖保留字段')
        if (entity, attr) in touched or not isinstance(c.get('stepId'), str) or c['stepId'] not in seen:
            raise ValueError('属性重复变化或缺少因果步骤')
        touched.add((entity, attr))
        value = c.get('value')
        if 'value' not in c or 'before' not in c or not (value is None or type(value) in (str, int, float, bool)):
            raise ValueError('状态属性必须是显式标量值')
        if isinstance(value, str) and len(value) > 400 or isinstance(value, float) and not math.isfinite(value):
            raise ValueError('状态属性值超出范围')
        kind = known[entity]['kind']
        if c['before'] != value_at(context['parent']['branchState'], entity, attr, kind):
            raise ValueError('状态变化的before与父分支不符：' + entity + '.' + attr + '应为' + str(value_at(context['parent']['branchState'], entity, attr, kind)) + '。未登记属性的before一律用null；不要从正文推测旧值，不变的属性不要登记。')
        if attr in ('locationId', 'ownerCharacterId'):
            if attr == 'ownerCharacterId' and kind != 'item' or attr == 'locationId' and kind not in ('item', 'character'):
                raise ValueError('结构化属性与实体类型不符')
            target_kind = 'location' if attr == 'locationId' else 'character'
            if value is not None and (not isinstance(value, str) or known.get(value, {}).get('kind') != target_kind):
                raise ValueError('位置或持有人必须引用正确实体')
            outcome = context['parent']['branchState'].get('characterOutcomeStates', {}).get(entity, {})
            if kind == 'character' and outcome.get('permanence') == 'permanent' and outcome.get('status') in ('dead', 'departed'):
                raise ValueError('通用变化不能移动永久下线人物')
            if entity == context['contract']['persona'].get('sourceCharacterId') and value is None:
                raise ValueError('玩家位置不能清空')
    for entity in known:
        spatial = {c['attribute']: c['value'] for c in changes if c['entityId'] == entity}
        if spatial.get('locationId') is not None and spatial.get('ownerCharacterId') is not None:
            raise ValueError('物品不能同时在地面和由人物持有')
    return plan


def project(state, plan, package):
    from .cocreation import apply_branch_patch
    result = apply_branch_patch(package, state, {'derivedAdditions': plan['introductions']}, 'reader-action')
    known = registry(package, result)
    for c in plan['stateChanges']:
        entity, attr, value = c['entityId'], c['attribute'], c['value']
        kind = known[entity]['kind']
        if attr == 'locationId':
            target = result.setdefault('characterLocationIds' if kind == 'character' else 'itemLocationIds', {})
            if entity == state.get('playerCharacterId'):
                result['playerLocationId'] = value
        elif attr == 'ownerCharacterId':
            target = result.setdefault('itemOwnerCharacterIds', {})
        else:
            target = result.setdefault(STATE_KEY, {}).setdefault(entity, {})
        key = entity if attr in ('locationId', 'ownerCharacterId') else attr
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
        if kind == 'item' and attr == 'ownerCharacterId' and value is not None:
            result.setdefault('itemLocationIds', {}).pop(entity, None)
        if kind == 'item' and attr == 'locationId' and value is not None:
            result.setdefault('itemOwnerCharacterIds', {}).pop(entity, None)
    return result


def validate_observations(data, body):
    paragraphs = {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}
    events = data.get('events') if isinstance(data, dict) else None
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise ValueError('正文事实提取缺少完整事件')
    seen, covered, grounded = set(), set(), []
    for original in events:
        e = dict(original) if isinstance(original, dict) else original
        if not isinstance(e, dict) or not isinstance(e.get('id'), str) or e['id'] in seen:
            raise ValueError('正文事实编号无效或重复')
        seen.add(e['id'])
        p = paragraphs.get(e.get('paragraphId')) if isinstance(e.get('paragraphId'), str) else None
        if not p:
            raise ValueError('正文事实须引用有效段落：' + str(e.get('paragraphId')))
        # Paragraph text is code-owned evidence; asking the model to transcribe
        # it adds copy errors without proving the event summary is true.
        e['quote'] = p
        grounded.append(e)
        covered.add(e['paragraphId'])
        if e.get('mode') not in ('actual', 'speech', 'intention', 'hypothetical', 'recollection', 'background'):
            raise ValueError('正文事实缺少实际/话语/意图的区分')
        if not isinstance(e.get('changes'), list) or not isinstance(e.get('introduced'), list):
            raise ValueError('正文事实缺少状态与新增实体清单')
        if not isinstance(e.get('summary'), str) or not e['summary'] or not isinstance(e.get('actor'), str):
            raise ValueError('正文事实缺少主体或实际事实说明')
        if any(not isinstance(name, str) or not name for name in e['introduced']):
            raise ValueError('新增实体名称无效')
        for change in e['changes']:
            if not isinstance(change, dict) or not all(isinstance(change.get(k), str) and change[k] for k in ('entityId', 'attribute')) or 'value' not in change:
                raise ValueError('事实状态变化格式无效')
            if change['value'] is not None and type(change['value']) not in (str, int, float, bool):
                raise ValueError('事实状态变化须为标量')
    if covered != set(paragraphs):
        raise ValueError('事实提取必须覆盖每个正文段落')
    return sorted(grounded, key=lambda e: int(e['paragraphId'][1:]))


def validate_events(review, events, plan, state=None, package=None):
    checks = review.get('eventChecks')
    if not isinstance(checks, list) or len(checks) != len(events):
        raise ValueError('通用行动审查未逐项覆盖实际事件')
    by_id = {c.get('id'): c for c in checks if isinstance(c, dict) and isinstance(c.get('id'), str)}
    if set(by_id) != {e['id'] for e in events}:
        raise ValueError('行动审查事件编号不完整')
    steps = {s['id'] for s in plan['steps']}
    changes = {c['id']: c for c in plan['stateChanges']}
    entities = {e['id'] for e in introductions(plan)}
    known = registry(package, state or {}, plan['introductions']) if package else {}
    observed_values = {}
    for event in events:
        check = by_id[event['id']]
        if check.get('verdict') != 'supported':
            raise ValueError('正文越出行动契约：' + str(check.get('reason', event['summary'])))
        for field, allowed in (('stepIds', steps), ('changeIds', changes), ('introductionIds', entities)):
            refs = check.get(field)
            if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
                raise ValueError('事件审查引用未登记的动作、变化或实体')
        if event['introduced'] and not check['introductionIds']:
            raise ValueError('正文引入未登记的关键实体：' + '、'.join(event['introduced']))
        for observed in event['changes']:
            if not isinstance(observed, dict):
                raise ValueError('提取的状态变化格式无效')
            key = (observed.get('entityId'), observed.get('attribute'))
            if key not in observed_values and key[0] in known:
                observed_values[key] = (state or {}).get('characterOutcomeStates', {}).get(key[0], {}).get('status') if key[1] == 'outcome' else value_at(state or {}, key[0], key[1], known[key[0]]['kind'])
            # An opening mention of the inherited state is not a new change.
            # Track in prose order: returning to it AFTER a transfer is a change.
            if key in observed_values and observed_values[key] == observed.get('value'):
                continue
            observed_values[key] = observed.get('value')
            matches = [changes[r] for r in check['changeIds'] if changes[r]['entityId'] == observed.get('entityId') and changes[r]['attribute'] == observed.get('attribute')]
            if observed.get('attribute') in ('locationId', 'ownerCharacterId') or type(observed.get('value')) is not str:
                matches = [c for c in matches if c['value'] == observed.get('value')]
            # Outcomes/goals have their own immutable state and evidence gate.
            specialized = (observed.get('attribute') == 'outcome' and any(o['characterId'] == observed.get('entityId') and o['status'] == observed.get('value') for o in plan['outcomes'])) or (observed.get('attribute') == 'goal' and bool(plan['goalUpdates']))
            if not matches and not specialized:
                raise ValueError('正文存在未登记的持续状态变化：' + str(observed))
    return checks
