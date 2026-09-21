"""Validate a pre-writing scene plan without granting actions or world facts."""
import re

from .reader_actions import ActionEvidenceError


# Player-facing turns share one final visible prose contract.  Source novel
# chapters keep their own authoring limits; this boundary applies to the
# interactive reader scene plan and its repair/expansion path.
# Short, decisive actions do not need filler prose. The planner still has to
# declare a bounded target and the final body must satisfy that target.
MIN_SCENE_CJK = 80
MAX_SCENE_CJK = 1500

_POSITIVE_DESTRUCTION = re.compile(
    r'(?<!未)(?<!尚未)(?<!没有)(?<!不)(?:已|已经|完成|发生|被).{0,8}(?:损毁|销毁|毁掉|毁坏|破坏)|(?:永久|彻底|完全)(?:地)?(?:损毁|销毁|毁掉|毁坏)'
)


def _affirmative_destruction(text):
    if not isinstance(text, str):
        return None
    for match in _POSITIVE_DESTRUCTION.finditer(text):
        prefix = text[max(0, match.start() - 8):match.start()]
        if re.search(r'(?:尚未|未|没有|不曾|不能|无法)\s*$', prefix):
            continue
        return match
    return None


def cjk_character_count(text):
    """Count Han characters used by the player-facing prose contract."""
    return len(re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]', text or ''))


def plan_premises(plan):
    """Number prerequisites for the existing independent authority review."""
    return {**{f'K{i+1}': item for i, item in enumerate(plan.get('knowledge', []))},
            **{f'O{i+1}': {'statement': text} for i, text in enumerate(plan.get('observationLimits', []))}}


def _current_observation_premise(target, check, contract):
    """Allow an authorized observation to produce a transient inference.

    ``scenePlan.knowledge`` normally describes facts already available before
    writing.  A player can also look at the current scene and form a bounded
    inference during this turn.  That inference is not an existing fact and
    must be tied to player steps without any persistent state change.  This
    narrow exception keeps movement/state patches and pre-existing knowledge
    behind the existing authority checks.
    """
    if target.get('status') != 'inference' or check.get('kind') != 'after_step':
        return False
    if any(contract.get(field) for field in ('stateChanges', 'outcomes', 'goalUpdates', 'threadUpdates')):
        return False
    if any((contract.get('introductions') or {}).get(kind) for kind in ('characters', 'items', 'locations')):
        return False
    if not (contract.get('scenePlan') or {}).get('observationLimits'):
        return False
    steps = {step.get('id'): step for step in contract.get('steps', []) if isinstance(step, dict)}
    return bool(check.get('stepIds')) and all(
        isinstance(steps.get(step_id), dict) and steps[step_id].get('authority') == 'player'
        for step_id in check['stepIds']
    )


def validate_plan_premises(data, contract, evidence):
    targets = plan_premises(contract.get('scenePlan') or {})
    if not targets:
        return
    checks = data.get('premiseChecks')
    if (not isinstance(checks, list) or len(checks) != len(targets)
            or any(not isinstance(c, dict) or not isinstance(c.get('id'), str) for c in checks)
            or {c['id'] for c in checks} != set(targets)):
        raise ActionEvidenceError('写前审查必须逐项覆盖知识与观察前提，不能漏检或重复')
    steps = {s['id'] for s in contract['steps']}
    for check in checks:
        key = check['id']
        missing = []
        if check.get('kind') not in ('existing', 'after_step', 'restriction', 'unknown'):
            missing.append('kind')
        if check.get('verdict') not in ('supported', 'unsupported'):
            missing.append('verdict')
        if not isinstance(check.get('sources'), list):
            missing.append('sources')
        if not isinstance(check.get('stepIds'), list):
            missing.append('stepIds')
        if not isinstance(check.get('missingEvidence'), list):
            missing.append('missingEvidence')
        if not isinstance(check.get('reason'), str) or not check['reason'].strip():
            missing.append('reason')
        if missing:
            raise ActionEvidenceError('写前前提核对缺少K/O必填字段：' + key + '；缺少或无效=' + '、'.join(missing)
                                      + '；必须说明性质、依据和缺证内容')
        if check.get('verdict') != 'supported' or check['missingEvidence']:
            raise ValueError('场景规划前提缺证：' + key + ' ' + check['reason'] + ' ' + str(check['missingEvidence']))
        for ref in check['sources']:
            if (not isinstance(ref, dict) or not isinstance(ref.get('id'), str) or ref['id'] not in evidence
                    or not isinstance(ref.get('quote'), str) or len(ref['quote'].strip()) < 4
                    or ref['quote'] not in evidence[ref['id']]):
                raise ActionEvidenceError('写前前提只能引用公开资料，不得引用规划自证：' + key + '；无效来源=' + str(ref) + '。只引用sceneEvidence原文；after_step不需伪造来源，使用stepIds。')
        if any(not isinstance(s, str) or s not in steps for s in check['stepIds']):
            raise ValueError('写前前提引用未授权步骤：' + key)
        kind, target = check['kind'], targets[key]
        if kind == 'existing' and not check['sources']:
            raise ValueError('既有知识或可见条件必须有公开来源：' + key)
        if kind == 'after_step' and not check['stepIds']:
            raise ValueError('本轮才形成的知识或观察必须有前置步骤：' + key)
        if target.get('status') in ('fact', 'reported') and kind != 'existing':
            raise ValueError('不能把知识断言改分类以豁免依据：' + key)
        if (target.get('status') == 'inference' and kind != 'existing'
                and not _current_observation_premise(target, check, contract)):
            raise ValueError('不能把知识断言改分类以豁免依据：' + key)
        if target.get('status') == 'pending' and (kind != 'after_step' or target['afterStepId'] not in check['stepIds']):
            raise ValueError('待获知信息须核对其实际前置步骤：' + key)


def validate_scene_plan(contract, evidence, people):
    plan = contract.get('scenePlan')
    if not isinstance(plan, dict):
        raise ValueError('ready结果须在写作前给出scenePlan')
    for field in ('start', 'outcome', 'stop', 'lengthReason'):
        if not isinstance(plan.get(field), str) or not 1 <= len(plan[field].strip()) <= 500:
            raise ValueError('scenePlan缺少有效的' + field)
    target = plan.get('targetCjk')
    if (not isinstance(target, list) or len(target) != 2
            or any(type(n) is not int for n in target)
            or not MIN_SCENE_CJK <= target[0] <= target[1] <= MAX_SCENE_CJK):
        raise ValueError(f'scenePlan.targetCjk须是{MIN_SCENE_CJK}至{MAX_SCENE_CJK}内的动态区间')
    beats = plan.get('beats')
    steps = {s['id'] for s in contract['steps']}
    covered = set()
    if not isinstance(beats, list) or not 1 <= len(beats) <= 8:
        raise ValueError('scenePlan.beats须包含1至8个有独立作用的过程')
    for beat in beats:
        if not isinstance(beat, dict) or not isinstance(beat.get('purpose'), str) or not beat['purpose'].strip():
            raise ValueError('场景过程须说明作用')
        refs = beat.get('stepIds')
        if not isinstance(refs, list) or not refs or any(not isinstance(k, str) or k not in steps for k in refs):
            raise ValueError('场景过程不能增加未授权步骤')
        covered.update(refs)
    if covered != steps:
        raise ValueError('场景过程遗漏已授权步骤')
    knowledge = plan.get('knowledge')
    if not isinstance(knowledge, list) or len(knowledge) > 16:
        raise ValueError('scenePlan.knowledge须明确列出本回合知识边界')
    for item in knowledge:
        if (not isinstance(item, dict) or not isinstance(item.get('speakerId'), str) or item['speakerId'] not in people
                or item.get('status') not in ('fact', 'reported', 'inference', 'unknown', 'pending')
                or not isinstance(item.get('statement'), str) or not item['statement'].strip()
                or not isinstance(item.get('sources'), list)):
            raise ValueError('场景知识须注明人物、内容、认知类型及来源')
        refs = item['sources']
        for ref in refs:
            if (not isinstance(ref, dict) or not isinstance(ref.get('id'), str)
                    or ref['id'] not in evidence or not isinstance(ref.get('quote'), str)
                    or len(ref['quote'].strip()) < 4 or ref['quote'] not in evidence[ref['id']]):
                raise ValueError('场景知识来源必须逐字引用公开资料：' + str(ref) + '；只引用knowledge.publicEvidence已有键和原文。待发生告知应为pending、sources=[]。')
        if item['status'] in ('fact', 'reported', 'inference') and not refs:
            raise ValueError('事实、转述或推断前提须有公开依据；缺依据时标unknown。本回合尚未发生的告知用pending并指定afterStepId，不能提前标reported')
        if _affirmative_destruction(item['statement']):
            quoted = '；'.join(ref.get('quote', '') for ref in refs if isinstance(ref, dict))
            if not _affirmative_destruction(quoted):
                raise ValueError('持有或捏着道具的来源只能证明当前持有，不能证明已经永久损毁：' + item['statement'][:80])
        if item['status'] == 'pending' and (not isinstance(item.get('afterStepId'), str) or item['afterStepId'] not in steps):
            raise ValueError('待获知内容须用afterStepId绑定本回合授权步骤；只有正文实际告知后才可转述')
    limits = plan.get('observationLimits')
    if not isinstance(limits, list) or len(limits) > 12 or any(not isinstance(s, str) or not s.strip() for s in limits):
        raise ValueError('scenePlan.observationLimits须列明本回合可观察条件；没有则空数组')
    options = plan.get('narrativeOptions', [])
    if (not isinstance(options, list) or len(options) > 8
            or any(not isinstance(option, dict)
                   or not isinstance(option.get('text'), str) or not option['text'].strip()
                   or option.get('status') != 'pending'
                   or option.get('requiresConfirmation') is not True for option in options)):
        raise ValueError('scenePlan.narrativeOptions只能登记待确认的具体表现')
    return plan
