"""Bounded public context and paragraph-level agency/background review."""
import re
from .reader_actions import ActionEvidenceError


def scene_knowledge(context, *, include_evidence=True):
    """Keep public evidence distinct from internal state and absent evidence."""
    opening = context['contract'].get('openingContext', {})
    evidence = public_scene_evidence(context)
    return {**({'publicEvidence': evidence} if include_evidence else {}),
            'sourceKinds': {key: 'history_scene' if key.startswith('history-') else 'opening_fact' for key in evidence},
            'unresolvedQuestions': opening.get('unresolvedQuestions', []),
            'evidenceBoundary': '资料未说明的经历、依据、规则条件均为 unknown；不从缺席推断没有或不可能。历史中的已发生动作可承接；人物说法只证明曾如此声称，不自动升级为客观规律或可靠知识。内部权威状态用于约束矛盾，不代表玩家或NPC已知。',
            'allowedNewContent': '本回合授权行动与当场反应；新说出的台词不能使其中的旧事自动成为事实。',
            'speakerBoundary': '开场已知信息属于玩家知识，不能默认NPC也知道。NPC获知必须有其听到、见到或曾明确说过的资料；本回合玩家先告知后，NPC可注明你说的而转述，不能升级为亲眼见证。没有获取路径就不知道。',
            'answerBoundary': '询问依据但资料没有依据时，NPC明确说不知道或尚不能判断；可以说明当前看见什么与无法据此断定什么，不补造亲历、传闻、检测或规律。'}


def scene_boundaries(plan):
    """Pass observation restrictions, never the planner's proposed answers."""
    if not plan:
        return {}
    return {f'O{i+1}': text for i, text in enumerate(plan['observationLimits'])}


def dialogue_units(body):
    """Number quoted spans without guessing their speaker or factual meaning."""
    return {f'D{i+1}': {'quote': match.group(), 'paragraphId': f'P{body[:match.start()].count(chr(10) * 2)+1}',
                      'start': match.start()}
            for i, match in enumerate(re.finditer(r'“[^”]+”|「[^」]+」|『[^』]+』|"[^"\n]+"', body))}


def repair_targets(problem):
    return {f'R{i+1}': issue for i, issue in enumerate(problem.get('issues', []))} if isinstance(problem, dict) else {}


def validate_knowledge_access(data, body, evidence, people, player_id):
    units = dialogue_units(body)
    if not units:
        return
    checks = data.get('knowledgeChecks') if isinstance(data, dict) else None
    if not isinstance(checks, list):
        raise ValueError('对白缺少逐项knowledgeChecks，不能把公开事实默认为NPC已知')
    seen, violations = set(), []
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('id'), str) or check['id'] not in units or check['id'] in seen:
            raise ValueError('对白知识核对编号无效或重复')
        seen.add(check['id'])
        unit = units[check['id']]
        if (not isinstance(check.get('speakerId'), str) or check['speakerId'] not in people
                or check.get('kind') not in ('current', 'unknown', 'background', 'reported', 'inference')
                or check.get('verdict') not in ('supported', 'unsupported', 'contradicted')
                or not isinstance(check.get('reason'), str) or not check['reason'].strip()
                or not isinstance(check.get('accessSources'), list)
                or not isinstance(check.get('missingEvidence'), list)
                or any(not isinstance(s, str) or not s.strip() for s in check['missingEvidence'])):
            raise ValueError('对白核对须注明说话人、知识类型、获知来源及缺证内容')
        reason = check['reason']
        unsupported = check['verdict'] != 'supported' or bool(check['missingEvidence'])
        for source in check['accessSources']:
            if not isinstance(source, dict) or not isinstance(source.get('id'), str) or not isinstance(source.get('quote'), str):
                raise ValueError('人物获知来源格式无效')
            ref, quote = source['id'], source['quote']
            available = evidence.get(ref, '')
            if ref.startswith('draft-P'):
                heard_reply = check['kind'] in ('current', 'unknown') and any(
                    prior['start'] < unit['start'] and ref == 'draft-' + prior['paragraphId']
                    and quote in prior['quote'] for prior in units.values())
                if check['kind'] != 'reported' and not heard_reply:
                    unsupported = True
                    reason += '；当前草稿只能证明先告知后转述，不能证明观察前提或既往事实'
                # Only prior text can communicate something to this speaker.
                prefix = body[:unit['start']].split('\n\n')
                available = {f'draft-P{i+1}': p for i, p in enumerate(prefix)}.get(ref, '')
            if len(quote.strip()) < 4 or quote not in available:
                unsupported = True
                reason += '；人物获知来源不存在、尚未发生或引用了自己的发言'
        if (check['speakerId'] != player_id and check['kind'] in ('background', 'reported', 'inference')
                and not check['accessSources']):
            unsupported = True
            reason += '；未提供该NPC获得信息的途径'
        if unsupported:
            violations.append(dict(paragraphId=unit['paragraphId'], type='background', claim=unit['quote'],
                                   reason=reason + ('；' + '；'.join(check['missingEvidence']) if check['missingEvidence'] else '')))
    if violations:
        raise SceneReviewError(violations)
    if seen != set(units):
        raise ValueError('对白知识核对遗漏编号：' + ','.join(sorted(set(units) - seen)))


def validate_repair_resolution(data, body, targets):
    if not targets:
        return
    checks = data.get('repairChecks') if isinstance(data, dict) else None
    if not isinstance(checks, list):
        raise ValueError('修复后须逐项返回repairChecks，不能仅用全篇通过代替')
    paragraphs = {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}
    seen, violations, unlocated = set(), [], []
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('id'), str) or check['id'] not in targets or check['id'] in seen:
            raise ValueError('修复问题编号无效或重复')
        seen.add(check['id'])
        issue = targets[check['id']]
        ids = check.get('paragraphIds')
        if (check.get('verdict') not in ('resolved', 'retained', 'rephrased', 'uncertain')
                or not isinstance(check.get('reason'), str) or not check['reason'].strip()
                or not isinstance(ids, list) or any(not isinstance(pid, str) or pid not in paragraphs for pid in ids)):
            raise ValueError('修复核对缺少有效判定、当前段落或原因')
        old_quote = issue.get('quote')
        unchanged = ([pid for pid, p in paragraphs.items() if old_quote in p]
                     if old_quote and body.count(old_quote) >= issue.get('beforeOccurrences', 1) else [])
        if check['verdict'] != 'resolved' or unchanged:
            reason = check['reason'] if not unchanged else '修复后仍保留已否定断言：' + str(old_quote)
            for pid in set(ids + unchanged):
                violations.append(dict(paragraphId=pid, type=issue['type'], claim=paragraphs[pid], reason=reason))
            if not ids and not unchanged:
                unlocated.append(reason)
    if violations or unlocated:
        raise SceneReviewError(violations, unlocated)
    if seen != set(targets):
        raise ValueError('修复核对遗漏问题：' + ','.join(sorted(set(targets) - seen)))


def validate_scene_boundaries(data, body, boundaries):
    if not boundaries:
        return
    if not isinstance(data, dict):
        raise ValueError('场景边界核对须返回对象')
    # Reuse exact coverage/location validation, but keep these knowledge findings
    # distinct from player-action violations in the unified repair list.
    try:
        validate_scope({'scopeChecks': data.get('boundaryChecks')},
                       {k: str(v) for k, v in boundaries.items()}, body)
    except SceneReviewError as error:
        raise SceneReviewError([{**v, 'type': 'background'} for v in error.violations], error.unlocated) from error


def combined_scene_issues(review, body, events, grounding, *, evidence=None, requirements=None, focused=None, boundaries=None,
                          people=None, player_id=None, repair_issues=None):
    """Merge independent semantic findings so one edit fixes the whole round."""
    violations, unlocated, format_errors = [], [], {}
    for index, validate in enumerate((lambda: reject_review_issues(review, body, events),
                     lambda: validate_grounding(grounding, grounding_claims(body), evidence=evidence),
                     lambda: validate_scene_boundaries(grounding, body, boundaries),
                     lambda: validate_knowledge_access(grounding, body, evidence or {}, people, player_id) if people is not None else None,
                     lambda: validate_repair_resolution(grounding, body, repair_targets(repair_issues)),
                     lambda: validate_scope(grounding, requirements, body) if requirements is not None else None,
                     lambda: validate_scope(focused, exclusive_requirements(requirements), body, focused=True) if focused is not None else None)):
        try:
            validate()
        except SceneReviewError as error:
            violations.extend(v for v in error.violations if v not in violations)
            unlocated.extend(error.unlocated)
        except ValueError as error:
            stage = 'action_review' if index == 0 else 'scope_review' if index == 6 else 'scene_grounding'
            format_errors.setdefault(stage, []).append(str(error))
    if violations or unlocated:
        raise SceneReviewError(violations, unlocated)
    if format_errors:
        raise SceneReviewFormatError(format_errors)


class SceneReviewFormatError(ValueError):
    """Malformed audit evidence is not a finding against the prose."""
    def __init__(self, errors):
        self.stages = set(errors)
        super().__init__('；'.join(message for messages in errors.values() for message in messages))


class SceneReviewError(ValueError):
    def __init__(self, violations, unlocated=()):
        self.violations = violations
        self.unlocated = list(unlocated)
        super().__init__('场景审查未通过：' + '；'.join(
            [v['paragraphId'] + '：' + v['reason'] for v in violations] + self.unlocated))

    def repair_problem(self):
        return {'issues': [{**{k: v[k] for k in ('paragraphId', 'type', 'reason')}, 'quote': v['claim']} for v in self.violations],
                'unlocatedIssues': self.unlocated}


class SceneGroundingError(SceneReviewError):
    def __init__(self, violations):
        super().__init__([{**v, 'type': 'background'} for v in violations])


def reject_review_issues(data, body, events=()):
    """Collect semantic rejections before any repairable reference validation.

    Legacy/unlocatable issues remain rejections. Never infer a paragraph from
    words or numbers embedded in a free-form reason.
    """
    if not isinstance(data, dict):
        return  # The existing shape validators reject malformed reviews.
    paragraphs = {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}
    event_paragraphs = {e['id']: e['paragraphId'] for e in events
                        if isinstance(e.get('id'), str) and isinstance(e.get('paragraphId'), str)}
    violations, unlocated = [], []

    def add(pid, kind, reason, quote=None):
        reason = str(reason or '审查报告语义问题，但未说明原因')
        if not isinstance(pid, str) or pid not in paragraphs:
            unlocated.append(reason)
            return
        precise = isinstance(quote, str) and len(quote.strip()) >= 4 and quote in paragraphs[pid]
        issue = dict(paragraphId=pid, type=kind, reason=reason, claim=quote if precise else paragraphs[pid])
        if issue not in violations:
            violations.append(issue)

    issues = data.get('issues', [])
    if isinstance(issues, list):
        for issue in issues:
            if isinstance(issue, dict):
                pid = issue.get('paragraphId')
                if pid is None and isinstance(issue.get('eventId'), str):
                    pid = event_paragraphs.get(issue['eventId'])
                kind = issue.get('type')
                add(pid, kind if kind in ('action', 'background', 'continuity', 'state') else 'continuity',
                    issue.get('reason') or issue.get('issue') or str(issue), issue.get('quote'))
            else:
                add(None, 'continuity', issue)
    checks = data.get('sceneChecks', [])
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        if check.get('playerDecision') == 'overreach':
            add(check.get('paragraphId'), 'action', check.get('issue') or '存在未授权行动', check.get('quote'))
        if check.get('background') in ('unsupported', 'contradicted'):
            add(check.get('paragraphId'), 'background', check.get('issue') or '存在无依据背景', check.get('quote'))
        claims = check.get('backgroundClaims', [])
        for claim in claims if isinstance(claims, list) else []:
            if isinstance(claim, dict) and claim.get('verdict') in ('unsupported', 'contradicted'):
                add(check.get('paragraphId'), 'background', claim.get('reason') or '场景背景存在缺证或矛盾', claim.get('claim'))
    checks = data.get('eventChecks', [])
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, dict) and check.get('verdict') in ('unsupported', 'contradicted'):
            pid = event_paragraphs.get(check.get('id')) if isinstance(check.get('id'), str) else None
            add(pid, 'state', check.get('reason') or '正文越出行动契约')
    if violations or unlocated:
        raise SceneReviewError(violations, unlocated)


def repair_paragraphs(body, error):
    paragraphs = {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}
    for violation in getattr(error, 'violations', []):
        pid, claim = violation['paragraphId'], violation['claim']
        if pid in paragraphs and claim in paragraphs[pid]:
            paragraphs[pid] = paragraphs[pid].replace(claim, '[此处存在审查问题，请依据授权与公开资料重写]')
    return paragraphs


def public_scene_evidence(context):
    # Only public opening facts and committed summaries.  Prior narrative prose
    # is validation input, never a later writing prompt or evidence source.
    evidence = {}
    opening = context['contract'].get('openingContext', {})
    for i, text in enumerate(opening.get('knownFacts', [])):
        if isinstance(text, str) and text:
            evidence[f'opening-{i+1}'] = text
    identity = opening.get('identity')
    if isinstance(identity, str) and identity:
        evidence['opening-identity'] = identity
    for node in context.get('lineage', [])[-3:]:
        outcome = node.get('readerOutcome') or {}
        action = outcome.get('action') if isinstance(outcome, dict) else {}
        action = action if isinstance(action, dict) else {}
        summaries = [value.strip() for value in (
            node.get('summary'), node.get('playerDirection'), action.get('summary'))
                     if isinstance(value, str) and value.strip()]
        if summaries:
            evidence[f"history-{node['id']}-P1"] = '；'.join(summaries)[:900]
        narrative = node.get('narrativeText')
        if isinstance(narrative, str):
            for index, paragraph in enumerate(narrative.split('\n\n'), 1):
                paragraph = paragraph.strip()
                # Keep whole, readable paragraphs; oversized generated/source
                # blocks are not useful as a grounding quote.
                if paragraph and len(paragraph) <= 900:
                    evidence[f"history-{node['id']}-P{index}"] = paragraph
    return evidence


def validate_scene_review(data, body, evidence, events=()):
    reject_review_issues(data, body, events)
    checks = data.get('sceneChecks')
    expected = {f'P{i+1}' for i, _ in enumerate(body.split('\n\n'))}
    if not isinstance(checks, list):
        raise ActionEvidenceError('sceneChecks 须逐段覆盖正文中的行动边界与背景断言')
    background_paragraphs = {e['paragraphId'] for e in events if e['mode'] in ('background', 'recollection')}
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('paragraphId'), str):
            raise ActionEvidenceError('sceneChecks 缺少有效段落编号')
        pid = check['paragraphId']
        if pid not in expected or pid in seen:
            raise ActionEvidenceError('sceneChecks 段落不存在或重复：' + pid)
        seen.add(pid)
        if check.get('playerDecision') not in ('none', 'authorized') or check.get('background') not in ('none', 'supported'):
            raise ActionEvidenceError('sceneChecks 的行动或背景判定无效：' + pid)
        if pid in background_paragraphs and check['background'] == 'none':
            raise ActionEvidenceError('不能略过已提取的背景断言：' + pid)
        claims = check.get('backgroundClaims', [])
        if not isinstance(claims, list) or any(not isinstance(c, dict) for c in claims):
            raise ActionEvidenceError('背景断言格式无效：' + pid)
        if any(c.get('verdict') in ('unsupported', 'contradicted') for c in claims):
            raise ValueError('场景背景存在缺证或矛盾：' + pid)
        if check['background'] == 'none' and claims:
            raise ActionEvidenceError('不能将已有背景断言标为 none：' + pid)
        refs = check.get('sources')
        if not isinstance(refs, list) or (check['background'] == 'supported' and not refs):
            raise ActionEvidenceError('已支持的背景须引用 sceneEvidence 原文：' + pid)
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get('id'), str) or not isinstance(ref.get('quote'), str):
                raise ActionEvidenceError('背景引用格式无效：' + pid)
            quote = ref['quote']
            if len(quote.strip()) < 4 or ref['id'] not in evidence or quote not in evidence[ref['id']]:
                raise ActionEvidenceError('背景引用不在公开 sceneEvidence 中：' + pid)
        if check['background'] == 'supported':
            # Bind the complete paragraph in code. The independent pass checks
            # all background assertions, including any omitted by the first pass.
            check['backgroundClaims'] = [{'claim': body.split('\n\n')[int(pid[1:])-1], 'sources': refs}]
    if seen != expected:
        raise ActionEvidenceError('sceneChecks 未覆盖段落：' + ','.join(sorted(expected-seen)))
    return checks


def grounding_claims(body):
    # Independent coverage must not inherit the first review's classifications.
    # Split mechanically, without trying to infer which sentences are factual.
    # Keep paragraph ids stable for bounded repairs and include full draft in the
    # review input so quotes spanning sentence boundaries retain their speaker.
    return {f'P{i+1}-C{j+1}': {'claim': sentence, 'paragraphId': f'P{i+1}'}
            for i, paragraph in enumerate(body.split('\n\n'))
            for j, sentence in enumerate(re.findall(r'[^。！？]+[。！？]*[”」』]*', paragraph))
            if sentence.strip()}


def validate_grounding(data, claims, *, evidence=None):
    checks = data.get('checks') if isinstance(data, dict) else None
    if not isinstance(checks, list):
        raise ValueError('背景独立核对缺少 checks')
    # A missing/failed independent judgment never grants a background fact.
    seen, violations, format_errors = set(), [], []
    for check in checks:
        if (not isinstance(check, dict) or not isinstance(check.get('id'), str)
                or check['id'] not in claims or check['id'] in seen):
            raise ValueError('背景独立核对编号无效')
        seen.add(check['id'])
        if evidence is not None:
            kind, sources = check.get('kind'), check.get('sources')
            if kind not in ('current', 'unknown', 'background', 'reported', 'inference') or not isinstance(sources, list):
                raise ValueError('独立核对须逐句区分当前反应、未知、背景或推断并列明来源')
            if not isinstance(check.get('reason'), str) or not check['reason'].strip():
                raise ValueError('独立核对须说明本句主体与依据')
            for ref in sources:
                if (not isinstance(ref, dict) or not isinstance(ref.get('id'), str)
                        or not isinstance(ref.get('quote'), str) or len(ref['quote'].strip()) < 4
                        or ref['id'] not in evidence or ref['quote'] not in evidence[ref['id']]):
                    format_errors.append('独立核对来源不在公开资料中：' + check['id'])
            if check.get('verdict') == 'supported' and kind in ('background', 'reported', 'inference') and not sources:
                violations.append({'paragraphId': check['id'].split('-C')[0], 'claim': claims[check['id']]['claim'],
                                   'reason': '既有背景或推断前提没有公开来源：' + check['reason']})
        if check.get('verdict') != 'supported':
            violations.append({'paragraphId': check['id'].split('-C')[0], 'claim': claims[check['id']]['claim'],
                               'reason': str(check.get('reason', '未获明确支持'))})
    if violations:
        raise SceneGroundingError(violations)
    if seen != set(claims):
        format_errors.append('背景独立核对遗漏断言')
    if format_errors:
        raise ValueError('；'.join(format_errors))
    return checks


def exclusive_requirements(requirements):
    # Select an extra focused review, never decide whether prose is legal.
    # All other requirements remain covered by the two existing reviews.
    return {k: text for k, text in requirements.items() if re.search(r'只|仅', text)}


def validate_scope(data, requirements, body, *, focused=False):
    """Require a second, explicit audit of every verbatim input clause."""
    checks = data.get('scopeChecks') if isinstance(data, dict) else None
    if not isinstance(checks, list):
        raise ValueError('独立核对缺少 scopeChecks')
    paragraphs = {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}
    seen, violations, unlocated = set(), [], []
    units = grounding_claims(body)
    sentence_checks = data.get('checks', [])
    if not isinstance(sentence_checks, list):
        raise ValueError('逐句行动核对格式无效')
    for check in sentence_checks:
        if not isinstance(check, dict) or not isinstance(check.get('id'), str) or check['id'] not in units:
            raise ValueError('逐句行动核对编号无效')
        ids = check.get('scopeViolations')
        if not isinstance(ids, list) or any(not isinstance(k, str) or k not in requirements for k in ids):
            raise ValueError('每句须明确列出违反的原始要求 scopeViolations')
        for rid in ids:
            unit = units[check['id']]
            violations.append(dict(paragraphId=unit['paragraphId'], type='action', claim=unit['claim'],
                                   reason=requirements[rid] + '：' + str(check.get('reason', '本句违反原始范围'))))
    for check in checks:
        if (not isinstance(check, dict) or not isinstance(check.get('id'), str)
                or check['id'] not in requirements or check['id'] in seen):
            raise ValueError('独立行动核对编号无效')
        seen.add(check['id'])
        ids, reason = check.get('paragraphIds'), check.get('reason')
        if (not isinstance(ids, list) or any(not isinstance(pid, str) or pid not in paragraphs for pid in ids)
                or len(ids) != len(set(ids)) or not isinstance(reason, str) or not reason.strip()):
            raise ValueError('独立行动核对缺少有效段落或理由')
        if check.get('verdict') not in ('satisfied', 'violated'):
            raise ValueError('独立行动核对判定无效')
        if check['verdict'] == 'satisfied' and not ids:
            raise ValueError('满足原始要求须引用正文段落')
        counterexamples = check.get('counterexamples', [])
        if focused and not isinstance(check.get('counterexamples'), list):
            raise ValueError('范围专项核对须先列出反证')
        for example in counterexamples if focused else []:
            if (not isinstance(example, dict) or not isinstance(example.get('paragraphId'), str)
                    or example['paragraphId'] not in paragraphs or not isinstance(example.get('quote'), str)
                    or len(example['quote'].strip()) < 4 or example['quote'] not in paragraphs[example['paragraphId']]):
                raise ValueError('范围反证必须逐字引用对应段落')
            violations.append(dict(paragraphId=example['paragraphId'], type='action', claim=example['quote'],
                                   reason=requirements[check['id']] + '：' + reason))
        if focused and counterexamples:
            continue  # Never let a positive summary erase concrete counterevidence.
        if check['verdict'] == 'violated':
            reason = requirements[check['id']] + '：' + reason
            if not ids:
                unlocated.append(reason)
            for pid in ids:
                violations.append(dict(paragraphId=pid, type='action', claim=paragraphs[pid], reason=reason))
    if seen != set(requirements):
        raise ValueError('独立行动核对遗漏原始要求：' + ','.join(sorted(set(requirements)-seen)))
    if violations or unlocated:
        raise SceneReviewError(violations, unlocated)
    return checks
