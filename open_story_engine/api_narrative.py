"""Player-facing narration policy. Source packages and CLI policy stay code-owned."""
import hashlib
import json
import re
from difflib import SequenceMatcher

from .prompts import render_prompt, catalog_version
from . import reader_consequences as consequences
from . import reader_actions

from .cocreation import (LlmPlanner, REPAIR_PLACEHOLDERS, guard_narrative, guard_source_character_names,
                        narrative_character_count, narrative_state_guardrails,
                        plain_model_narrative, narration_outside_dialogue,
                        guard_repeated_paragraphs, plan_result, scripted_followup_directions, empty_branch_additions)
from .llm import LlmError, parse_json_content
from .api_journey import player_directions
from .api_openings import RAINY_KNOWN_CLUES
from . import api_routes
from .api_reader_quality import (validate_outcome, continuity_check, reading_history,
                                 action_requirements, validate_action_requirements, scene_pacing)


from .reader_scene_review import public_scene_evidence, validate_scene_review, grounding_claims, validate_grounding, repair_paragraphs, reject_review_issues, SceneReviewError, scene_knowledge, combined_scene_issues, exclusive_requirements

from .reader_scene_plan import (MIN_SCENE_CJK, MAX_SCENE_CJK, plan_premises,
                                validate_plan_premises, validate_scene_plan, cjk_character_count)
from .reader_scene_review import SceneReviewFormatError, scene_boundaries, dialogue_units, repair_targets

MAX_CHAPTER_CHARACTERS = MAX_SCENE_CJK


def player_action(context, selected):
    return context.get('playerDirection') or (selected.get('summary') if selected.get('isFreeText') else None) or selected['title']


def player_package(package, character_id=None):
    guidelines = {**package['world'].get('narrativeGuidelines', {}),
                  'perspective': 'second_person_limited'}
    if character_id:
        guidelines['focalCharacterId'] = character_id
    if package.get('sourceAnalysis', {}).get('sha256') == api_routes.RAINY_SOURCE:
        guidelines['ledgerItemAvailability'] = True
    return {**package, 'world': {**package['world'], 'narrativeGuidelines': guidelines}}


def perspective_rule(name):
    return (render_prompt('reader.perspective',
        name=f'{name}',
    ))


def check_player_voice(text, name):
    prose = narration_outside_dialogue(text)
    if '你' not in prose or re.search(r'你是\s*' + re.escape(name), prose):
        raise ValueError(f'玩家{name}必须称为“你”，禁止用“{name}／他／她”叙述玩家，也禁止“你是{name}”式介绍')
    if re.search(r'(?:^|[。！？\n])\s*' + re.escape(name) + r'(?:抬|低|走|看|听|想|推|伸|把|感到|意识到)', prose):
        raise ValueError('正文将玩家写回了第三人称')


def trim_optional_paragraphs(body, candidates, maximum=MAX_CHAPTER_CHARACTERS):
    paragraphs = body.split('\n\n')
    total = cjk_character_count(body)
    # Only the editor's optional paragraphs are eligible. Protect the entry and
    # final outcome; choose a subset by exact size instead of chopping the ending.
    subsets = {0: []}
    for key in dict.fromkeys(key for key in candidates[:20] if isinstance(key, str)):
        if not isinstance(key, str) or not re.fullmatch(r'P\d+', key):
            continue
        index = int(key[1:]) - 1
        if not 0 < index < len(paragraphs)-2:
            continue
        size = cjk_character_count(paragraphs[index])
        for removed, indexes in list(subsets.items()):
            if removed + size < total:
                subsets.setdefault(removed + size, indexes + [index])
    options = [(removed, indexes) for removed, indexes in subsets.items()
               if 0 < total-removed <= maximum]
    if not options:
        return body
    excluded = set(min(options)[1])
    return '\n\n'.join(p for i, p in enumerate(paragraphs) if i not in excluded)


def check_reader_repetition(text):
    """Catch loops made of short dialogue/action paragraphs as well as long ones."""
    seen, repeated = set(), 0
    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = re.sub(r'\s+', '', paragraph)
        if len(paragraph) >= 15:
            if paragraph in seen:
                repeated += len(paragraph)
            seen.add(paragraph)
    if repeated >= 160:
        raise ValueError('本章重复了已经写过的动作或对话段落；不得再次演出同一段剧情来补足字数')


def complete_with_retry(gateway, kind, messages, stream=None, stream_reset=None, *, stage=None):
    """Retry one transient provider failure at the failed call, not the whole turn."""
    previous = []
    for attempt in range(2):
        try:
            result = getattr(gateway, kind)(messages, stream, stream_reset)
            result.observations = previous + result.observations
            return result
        except LlmError as error:
            if stage:
                error.observations = [{**o, 'generationStage': stage} for o in error.observations]
            if attempt or error.code not in ('transport_error', 'empty_stream', 'empty_json'):
                error.observations = previous + error.observations
                raise
            previous = [{**o, 'retryReason': 'transient_provider_failure'} for o in error.observations]
            if stream and stream_reset:
                stream_reset('connection_retry')


def repair_issue_types(problem):
    if not isinstance(problem, dict):
        return ['unknown']
    kinds = {issue.get('type', 'unknown') for issue in problem.get('issues', [])}
    if problem.get('unlocatedIssues') or not kinds:
        kinds.add('unknown')
    return sorted(kinds)


def record_repair_failure(record, error, stage, outcome):
    problem = error.repair_problem() if isinstance(error, SceneReviewError) else str(error)
    record.update(outcome=outcome, failureStage=stage,
                  failureCode=error.code if isinstance(error, LlmError) else 'validation_error',
                  failureReason=str(error), failureIssues=problem,
                  failureIssueTypes=repair_issue_types(problem))


def apply_scene_repairs(body, replacements, violations=()):
    paragraphs = body.split('\n\n')
    if not isinstance(replacements, list) or not 1 <= len(replacements) <= 8:
        raise ValueError('局部修订必须提供1—8个段落')
    edits = {}
    for replacement in replacements:
        if not isinstance(replacement, dict):
            raise ValueError('局部修订格式无效')
        key, text = replacement.get('paragraphId'), replacement.get('text')
        if not isinstance(key, str) or not re.fullmatch(r'P\d+', key) or not isinstance(text, str):
            raise ValueError('局部修订缺少有效的段落编号或正文')
        index = int(key[1:])-1
        if not 0 <= index < len(paragraphs) or index in edits:
            raise ValueError('局部修订的段落不存在或重复')
        if not text.strip() and (index in (0, len(paragraphs)-1) or not any(v['paragraphId'] == key for v in violations)):
            raise ValueError('只能删除已定位错误的中间段，不能删除开头、结尾或未报错段')
        edits[index] = text.strip()
    if all(paragraphs[i].strip() == text for i, text in edits.items()):
        raise ValueError('局部修订原样返回了有问题的段落，没有实际修正')
    if sum(narrative_character_count(paragraphs[i]) for i in edits) > max(200, narrative_character_count(body)//2):
        raise ValueError('局部修订超出原文一半，不能伪装成局部修正')
    result = '\n\n'.join(text for i, p in enumerate(paragraphs) if (text := edits.get(i, p)))
    if any(marker in result for marker in REPAIR_PLACEHOLDERS):
        raise ValueError('局部修订未替换待纠正标记')
    for v in violations:
        if v['claim'] in body and result.count(v['claim']) >= body.count(v['claim']):
            raise ValueError('局部修订仍保留已否定的背景段落：' + v['paragraphId'])
    for dependency in repair_dialogue_dependencies(body):
        index = int(dependency['paragraphId'][1:])-1
        quote = dependency['quote']
        if (quote in dialogue_references(edits.get(index, paragraphs[index]))
                and not any(quote in edits.get(i, p) for i, p in enumerate(paragraphs[:index]))):
            raise ValueError('局部修订删除了前文台词，却保留' + dependency['paragraphId'] + '对该台词的引用；须同步衔接')
    edited_paragraphs, offset = [], 0
    for i, original in enumerate(paragraphs):
        size = len(edits.get(i, original).split('\n\n')) if edits.get(i, original) else 0
        if i in edits:
            edited_paragraphs.extend(range(offset, offset + size))
        offset += size
    check_repair_repetition(body, result, edited_paragraphs)
    return result


def dialogue_references(paragraph):
    # Only explicit references to spoken words; no inference about intent.
    return [first or second for first, second in re.findall(
        r'(?:说完|说到|提到|问完|答完|那句|那声)[“「]([^”」\n]{2,80})[”」]|说[“「]([^”」\n]{2,80})[”」]时', paragraph)]


def repair_dialogue_dependencies(body):
    paragraphs = body.split('\n\n')
    dependencies = []
    for i, paragraph in enumerate(paragraphs):
        for quote in dialogue_references(paragraph):
            sources = [f'P{j+1}' for j, p in enumerate(paragraphs[:i]) if quote in p]
            if sources:
                dependencies.append(dict(paragraphId=f'P{i+1}', quote=quote, sourceParagraphIds=sources))
    return dependencies


def check_repair_repetition(before, after, edited):
    """Reject newly copied prose before paying for another full model review."""
    compact = lambda text: ''.join(re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]', text))
    prior, current = compact(before), compact(after)
    paragraphs = [compact(p) for p in after.split('\n\n')]
    for index in edited:
        for other, paragraph in enumerate(paragraphs):
            if other == index:
                continue
            for match in SequenceMatcher(None, paragraphs[index], paragraph, autojunk=False).get_matching_blocks():
                if match.size < 15:
                    continue
                phrase = paragraphs[index][match.a:match.a + match.size]
                if current.count(phrase) > max(1, prior.count(phrase)):
                    raise ValueError(f'局部修订新增与P{other+1}重复的长句；保留原句，重写修订段的独立作用')


class PlayerNarrativePlanner(LlmPlanner):
    """Reuse core fact guards and metadata; expose only established player history."""
    interactive_reader = True
    published_directions = staticmethod(api_routes.directions)
    prepare_direction = staticmethod(api_routes.prepare_direction)
    system_instruction = (
        render_prompt('reader.narrative_system')
    )

    def validation_scope(self, context, scope):
        current = api_routes.scene(context['package'], context['parent']['branchState'])
        if current:
            return {**scope, 'narrativeBrief': [*scope.get('narrativeBrief', []), {'text': current[3]}]}
        return scope

    def _check_action_authority(self, context, action, contract, raw, observations):
        messages = [
            {'role': 'system', 'content': reader_actions.AUTHORITY_RULES},
            {'role': 'user', 'content': json.dumps({
                'input': action, 'previous': context['parent']['narrativeText'],
                'playerId': context['contract']['persona'].get('sourceCharacterId'),
                'priorState': consequences.prompt_state(context['parent']['branchState']),
                'steps': contract['steps'], 'stateChanges': contract['stateChanges'],
                'scenePlan': contract.get('scenePlan'),
                'premises': plan_premises(contract.get('scenePlan') or {}),
                'sceneEvidence': public_scene_evidence(context),
            }, ensure_ascii=False)},
        ]
        for attempt in range(2):
            completion = complete_with_retry(self.gateway, 'complete_json', messages, stage='action_authority')
            raw.append(completion.raw_response)
            observations.extend({**o, 'generationStage': 'action_authority', 'evidenceAttempt': attempt} for o in completion.observations)
            try:
                review = reader_actions.validate_authority(parse_json_content(completion.content), contract, action, context['parent']['narrativeText'])
                validate_plan_premises(review, contract, public_scene_evidence(context))
                return review
            except reader_actions.ActionEvidenceError as error:
                if attempt:
                    # Do not regenerate an unchanged plan because its reviewer
                    # cannot quote the input. Fail within this bounded stage.
                    raise LlmError(str(error), 'model_output_rejected') from error
                messages += [{'role': 'assistant', 'content': completion.content},
                             {'role': 'user', 'content': render_prompt('reader.authority_repair', error=str(error))}]

    def _prompt(self, context, selected, state, repair, terminal_source_branch=False):
        self.writing_scope = None
        self.last_prompt_context = {'mode': 'player_history', 'perspective': 'second_person_limited',
                                    'terminalSourceBranch': bool(terminal_source_branch)}
        persona = context['contract']['persona']
        parent = context['parent']['branchState']
        changes = {k: {'from': parent.get(k), 'to': v} for k, v in state.items()
                   if parent.get(k) != v and k not in ('branchLedger', 'freeTextProgress')}
        history = reading_history(context['lineage'])
        names = [c['name'] for c in context['package']['characters'] + state.get('derivedCharacters', [])]
        places = [p['name'] for p in context['package']['locations'] + state.get('derivedLocations', [])]
        place_names = {p['id']: p['name'] for p in context['package']['locations'] + state.get('derivedLocations', [])}
        location_changed = parent.get('playerLocationId') != state.get('playerLocationId')
        scene_boundary = ('完成本回合已授权的转场后，用自然动作交代同行者也已抵达。' if location_changed else
                          '本回合没有授权转场。玩家始终在当前场景完成所选行动，不跟随他人离开、不押送下山、不前往新的场所。NPC可以提出下一步安排，是否接受及实际出发留给玩家下一回合决定。')
        final_people = {c['name']: place_names.get(state.get('characterLocationIds', {}).get(c['id']))
                        for c in context['package']['characters'] + state.get('derivedCharacters', []) if c['id'] in state.get('characterLocationIds', {})
                        and state.get('characterOutcomeStates', {}).get(c['id'], {}).get('status') not in ('dead', 'departed')}
        current = api_routes.scene(context['package'], parent)
        if current:
            # Earlier prototype prose over-elaborated unconfirmed scenery.
            # Use code-confirmed history and a short handoff, not pages of that
            # stylistic repetition as the next chapter's imitation template.
            known = RAINY_KNOWN_CLUES.get(persona['name'], [])
            completed = [e['summary'] for e in parent.get('derivedEvents', []) if e['id'].startswith(api_routes.PREFIX)]
            history = ('\n'.join(known + completed) + '\n' + reading_history(context['lineage'])
                       + '\n只承接以上已确认摘要；历史原始正文不进入本回合写作上下文。')
        event_brief = api_routes.turn_material(context['package'], parent, selected)
        if context['contract'].get('openingContext'):
            opening = {k: v for k, v in context['contract']['openingContext'].items()
                       if k not in ('evidence', 'sourceSha256', 'sourceCutoff', 'sourceCutoffLine')}
            event_brief += ('\n入场时的角色知识与关系（是前史，不得用于重置本局后来已确认的变化）：'
                            + json.dumps(opening, ensure_ascii=False)
                            + '\n当前物品与人物状态以本回合状态为准：' + json.dumps(consequences.prompt_state(state), ensure_ascii=False)
                            + '\n前史与未来契约：' + json.dumps({**context['contract']['continuityContract'], 'outcomeUpdatesEnabled': True, 'implementationBoundary': consequences.VERSION}, ensure_ascii=False))
        if context.get('resultContract'):
            event_brief += '\nknowledge（公开事实与未知边界，旧台词只证明曾经说过）：' + json.dumps(scene_knowledge(context), ensure_ascii=False)
            event_brief += '\n必须逐项保持的原始要求（对象、范围、先后与停止点不得被计划改写）：' + json.dumps(action_requirements(player_action(context, selected)), ensure_ascii=False)
            event_brief += '\n本回合必须兑现的结果契约：' + json.dumps(context['resultContract'], ensure_ascii=False)
            event_brief += '\n当前角色目标（允许玩家主动改变，不能强迫回原著）：' + json.dumps(consequences.goals_for(context['package'], context['contract'], parent), ensure_ascii=False)
            event_brief += '\n已发生重大结果的结构化摘要（复述必须与实际经过一致）：' + json.dumps(consequences.planning_context(context)['criticalHistory'], ensure_ascii=False)
        if current:
            event_brief += '\n' + api_routes.fact_sheet(context['package'], parent, bool(selected.get('readerInterlude')))
        if selected.get('readerInterlude'):
            place = place_names.get(parent.get('playerLocationId'), '')
            return render_prompt('legacy_rainy.interlude',
                perspective_rule=f"{perspective_rule(persona['name'])}",
                latest=f'{reading_history(context["lineage"])}',
                reading_history=f"{reading_history(context['lineage'])}",
                event_brief=f'{event_brief}',
                player_action=f'{player_action(context, selected)}',
                place=f'{place}',
                place_2=f'{place}',
                repair_instruction=f"{('必须修正：' + repair if repair else '')}",
            )
        ending = bool(current and not selected.get('readerInterlude') and api_routes.completed_steps(parent) == api_routes.STEPS-1)
        self.last_prompt_context['scene'] = current[2] if current else selected['title']
        pacing = scene_pacing(context.get('resultContract'), parent, state)
        self.last_prompt_context['pacing'] = pacing
        return render_prompt('reader.narrative',
            pacing_json=json.dumps(pacing, ensure_ascii=False),
            perspective_rule=f"{perspective_rule(persona['name'])}",
            title=player_action(context, selected),
            summary=f"{selected['summary']}",
            history=f'{history}',
            changes_json=f'{json.dumps(changes, ensure_ascii=False)}',
            narrative_state_guardrails=f"{narrative_state_guardrails(context['package'], state)}",
            player_location_id=f"{state.get('playerLocationId', state.get('currentLocationId'))}",
            final_people_json=f'{json.dumps(final_people, ensure_ascii=False)}',
            scene_boundary=f'{scene_boundary}',
            place_names_json=f'{json.dumps(place_names, ensure_ascii=False)}',
            character_names=f"{'、'.join(names)}",
            location_names=f"{'、'.join(places)}",
            event_brief=f'{event_brief}',
            ending_instruction=f"{('这是本路线的结局，明确回应最初目标，自然收束，不再悬置同一个问题。' if ending or terminal_source_branch else '停在需要你作出下一步决定的位置。')}",
            repair_instruction=f"{('修正要求：' + repair if repair else '')}",
        )

    def _scene_draft(self, context, selected, state, stream, stream_reset, raw, observations, attempt, repair):
        completion = complete_with_retry(self.gateway, 'complete_text', [
            {'role': 'system', 'content': render_prompt('reader.turn_system',
                system_instruction=self.system_instruction,
                name=context['contract']['persona']['name'],
                player_action=player_action(context, selected),
                interlude_material=api_routes.turn_material(context['package'], context['parent']['branchState'], selected) if selected.get('readerInterlude') else '',
            )},
            {'role': 'user', 'content': self._prompt(context, selected, state, repair)},
        ], stream, stream_reset)
        raw.append(completion.raw_response)
        observations.extend({**o, 'generationStage': 'chapter', 'revision': attempt} for o in completion.observations)
        return plain_model_narrative(completion.content), completion.body_was_streamed

    def plan(self, context, selected, resolved_state, stream=None, stream_reset=None, **kwargs):
        name = context['contract']['persona']['name']
        body = ''
        retained_body = ''
        grounding_cache = {}
        grounding_inputs = {}
        scope_cache = {}
        repair_record = None
        pending_repair_issues = None
        reader_outcome = None
        interlude = bool(selected.get('readerInterlude'))
        observations, raw = [], []
        turn_base_state = resolved_state
        try:
            result_contract = None
            consequence_update = None
            if consequences.enabled(context['package']):
                requirements = action_requirements(player_action(context, selected))
                plan_messages = [
                    {'role': 'system', 'content': consequences.PLAN_RULES},
                    {'role': 'user', 'content': json.dumps({**consequences.planning_context(context),
                        'input': player_action(context, selected), 'requirements': requirements,
                        'knowledge': scene_knowledge(context)}, ensure_ascii=False)},
                ]
                for contract_attempt in range(2):
                    planning = complete_with_retry(self.gateway, 'complete_json', plan_messages, stage='result_contract')
                    raw.append(planning.raw_response)
                    observations.extend({**o, 'generationStage': 'result_contract', 'revision': contract_attempt} for o in planning.observations)
                    try:
                        result_contract = consequences.validate_plan(parse_json_content(planning.content), requirements, context)
                        if result_contract['decision'] == 'ready':
                            validate_scene_plan(result_contract, public_scene_evidence(context),
                                                {key for key, entity in reader_actions.registry(
                                                    context['package'], context['parent']['branchState'],
                                                    result_contract['introductions']).items() if entity['kind'] == 'character'})
                            observations.append({'generationStage': 'scene_plan', 'plan': result_contract['scenePlan']})
                            authority_review = self._check_action_authority(context, player_action(context, selected), result_contract, raw, observations)
                        break
                    except ValueError as error:
                        if contract_attempt:
                            raise LlmError(str(error), 'model_output_rejected') from error
                        plan_messages += [{'role': 'assistant', 'content': planning.content},
                                          {'role': 'user', 'content': render_prompt('reader.plan_repair',
                                              error=str(error),
                                          )}]
                if result_contract['decision'] != 'ready':
                    raise LlmError(result_contract['message'], 'action_' + result_contract['decision'])
                context = {**context, 'resultContract': result_contract}
                resolved_state = consequences.projected_state(resolved_state, result_contract, context['package'])
            failure = ''
            repaired_body = None
            for attempt in range(3):
                if attempt and stream_reset:
                    stream_reset('chapter_revision')
                if repaired_body is None:
                    body, body_was_streamed = self._scene_draft(context, selected, resolved_state, stream, stream_reset, raw, observations, attempt, failure)
                else:
                    body, repaired_body, body_was_streamed = repaired_body, None, True
                    if stream:
                        stream(body)
                count = cjk_character_count(body)
                if count and not any(marker in body for marker in REPAIR_PLACEHOLDERS):
                    retained_body = body
                observations.append({'generationStage': 'scene_draft_metrics', 'revision': attempt,
                    'actualCjk': cjk_character_count(body),
                                     'paragraphs': len(body.split('\n\n')),
                                     'pacing': self.last_prompt_context.get('pacing', {})})
                if count > MAX_CHAPTER_CHARACTERS:
                    edit = complete_with_retry(self.gateway, 'complete_json', [
                        {'role': 'system', 'content': render_prompt('reader.trim')},
                        {'role': 'user', 'content': json.dumps({f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}, ensure_ascii=False)},
                    ])
                    raw.append(edit.raw_response)
                    observations.extend({**item, 'generationStage': 'length_edit', 'revision': attempt} for item in edit.observations)
                    try:
                        candidates = parse_json_content(edit.content).get('removable', [])
                        if isinstance(candidates, list):
                            body = trim_optional_paragraphs(body, candidates)
                    except (ValueError, AttributeError, LlmError):
                        pass
                    count = cjk_character_count(body)
                    if count <= MAX_CHAPTER_CHARACTERS and stream_reset and stream:
                        stream_reset('chapter_edited')
                        stream(body)
                        body_was_streamed = True
                if repair_record is not None:
                    repair_record['afterBody'] = body
                try:
                    check_player_voice(body, name)
                    if api_routes.role_name(context['package'], context['parent']['branchState']):
                        continuity_check(body, name, reading_history(context['lineage']))
                    guard_repeated_paragraphs(body)
                    check_reader_repetition(body)
                    count = cjk_character_count(body)
                    if not count:
                        raise ValueError('本章未返回正文，必须写出实际发生的行动与结果')
                    # The scene plan's lower bound is a planning budget, not a
                    # hard visible-prose quota. A short action may be complete
                    # in fewer characters; only an empty draft is invalid.
                    if count > MAX_CHAPTER_CHARACTERS:
                        raise ValueError(f'本章为{count}字，超过单次{MAX_CHAPTER_CHARACTERS}字上限。删除非必要渲染，保留完整因果与阶段结果，不要截断结尾。')
                    guard_narrative(body, resolved_state, context['characterDetails'],
                                    context['package']['world']['narrativeGuidelines'], context['package'])
                    guard_source_character_names(body, context['package'], resolved_state,
                                                 session_persona=context['contract']['persona'])
                    api_routes.check_scene_result(context['package'], context['parent']['branchState'], body, interlude)
                    current = api_routes.scene(context['package'], context['parent']['branchState'])
                    if current:
                        ending_review = complete_with_retry(self.gateway, 'complete_json', [
                            {'role': 'system', 'content': render_prompt('legacy_rainy.ending_review')},
                            {'role': 'user', 'content': json.dumps({'player': name, 'locations': [p['name'] for p in context['package']['locations']], 'draft': body}, ensure_ascii=False)},
                        ])
                        raw.append(ending_review.raw_response)
                        observations.extend({**o, 'generationStage': 'ending_review', 'revision': attempt} for o in ending_review.observations)
                        try:
                            ending = parse_json_content(ending_review.content)
                        except LlmError as error:
                            raise ValueError('章末核对没有返回有效 JSON') from error
                        api_routes.check_reviewed_ending(context['package'], context['parent']['branchState'], body, ending, interlude)
                        review = complete_with_retry(self.gateway, 'complete_json', [
                            {'role': 'system', 'content': render_prompt('legacy_rainy.fact_review')},
                            {'role': 'user', 'content': json.dumps({'facts': '正文中的你指' + name + '。' + api_routes.fact_sheet(context['package'], context['parent']['branchState'], interlude)
                                + '\n此前已完成的事件：' + '；'.join(e['summary'] for e in context['parent']['branchState'].get('derivedEvents', []) if e['id'].startswith(api_routes.PREFIX)),
                                'scene': api_routes.turn_material(context['package'], context['parent']['branchState'], selected),
                                'review_boundary': render_prompt('legacy_rainy.review_boundary'),
                                'player_action': player_action(context, selected), 'previous': reading_history(context['lineage']),
                                'previous_actions': reading_history(context['lineage']), 'draft': body}, ensure_ascii=False)},
                        ])
                        raw.append(review.raw_response)
                        observations.extend({**item, 'generationStage': 'scene_review', 'revision': attempt} for item in review.observations)
                        try:
                            review_data = parse_json_content(review.content)
                        except LlmError as error:
                            raise ValueError('场景核对没有返回有效 JSON，需要重新核对') from error
                        issues = review_data.get('issues') if isinstance(review_data, dict) else None
                        if isinstance(issues, list):
                            normalized = []
                            for issue in issues:
                                if isinstance(issue, dict):
                                    message = issue.get('issue', issue.get('message', issue.get('explanation')))
                                    if not isinstance(message, str) or not message:
                                        raise ValueError('事实核对的问题描述格式无效')
                                    issue = (str(issue.get('quote', issue.get('sentence', ''))) + ' ' + message).strip()
                                normalized.append(issue)
                            issues = normalized
                        if not isinstance(issues, list) or any(not isinstance(i, str) or not i for i in issues):
                            raise ValueError('场景核对格式无效，需要重新核对本回合事件')
                        if issues:
                            raise ValueError('场景未闭合：' + '；'.join(issues[:3]))
                        reader_outcome = validate_outcome(review_data, body, selected['title'], [c['name'] for c in context['package']['characters']])
                        if not interlude and reader_outcome['action']['status'] != 'performed':
                            raise ValueError('本阶段所选行动尚未完成，不能登记目标节点')
                    if not current and context['contract'].get('continuityContract'):
                        requirements = action_requirements(player_action(context, selected))
                        events = []
                        if result_contract:
                            observation_messages = [
                                {'role': 'system', 'content': reader_actions.OBSERVE_RULES + render_prompt('reader.scene_observe')},
                                {'role': 'user', 'content': json.dumps({
                                    'viewpoint': {'id': context['contract']['persona'].get('sourceCharacterId'), 'name': name},
                                    'registry': {cid: {key: entity[key] for key in ('id', 'name', 'kind', 'aliases') if key in entity}
                                        for cid, entity in reader_actions.registry(context['package'], context['parent']['branchState'], result_contract['introductions']).items()},
                                    'state': consequences.prompt_state(context['parent']['branchState']),
                                    'previous': reading_history(context['lineage']),
                                    'attributeVocabulary': reader_actions.attribute_vocabulary(context['package'], context['parent']['branchState'], result_contract),
                                    'draft': {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}}, ensure_ascii=False)},
                            ]
                            partial_events, missing_paragraphs = None, None
                            for extraction_attempt in range(2):
                                observation = complete_with_retry(self.gateway, 'complete_json', observation_messages, stage='fact_extraction')
                                raw.append(observation.raw_response)
                                observations.extend({**o, 'generationStage': 'fact_extraction', 'revision': attempt, 'extractionAttempt': extraction_attempt} for o in observation.observations)
                                try:
                                    events = reader_actions.validate_observations(parse_json_content(observation.content), body, missing_paragraphs)
                                    if partial_events is not None:
                                        events = reader_actions.validate_observations(reader_actions.merge_observations(partial_events, events), body)
                                    break
                                except ValueError as error:
                                    if extraction_attempt:
                                        raise LlmError(str(error), 'model_output_rejected') from error
                                    if isinstance(error, reader_actions.ObservationCoverageError):
                                        partial_events, missing_paragraphs = error.events, error.missing
                                        original_input = json.loads(observation_messages[1]['content'])
                                        original_input['contextOnlyDraft'] = original_input['draft']
                                        original_input['draft'] = {pid: original_input['draft'][pid] for pid in missing_paragraphs}
                                        original_input['requiredParagraphIds'] = missing_paragraphs
                                        observation_messages = [observation_messages[0],
                                            {'role': 'user', 'content': json.dumps(original_input, ensure_ascii=False)},
                                            {'role': 'user', 'content': render_prompt('reader.observation_repair', error=str(error))}]
                                        observations.append({'generationStage': 'observation_gap_repair', 'paragraphIds': missing_paragraphs,
                                                             'retainedEvents': len(partial_events), 'revision': attempt})
                                        continue
                                    observation_messages += [
                                        {'role': 'assistant', 'content': observation.content},
                                        {'role': 'user', 'content': render_prompt('reader.observation_repair',
                                            error=str(error),
                                        )},
                                    ]
                        review_messages = [
                            {'role': 'system', 'content': render_prompt('reader.action_review') + (consequences.REVIEW_RULES + render_prompt('reader.scene_review') if result_contract else '')},
                            {'role': 'user', 'content': json.dumps({'player': name, 'requirements': requirements,
                                'priorRepairIssues': pending_repair_issues,
                                'input': player_action(context, selected), 'resultContract': result_contract, 'observedEvents': events,
                                'sceneEvidence': public_scene_evidence(context), 'knowledge': scene_knowledge(context, include_evidence=False),
                                'continuity': {k: v for k, v in consequences.planning_context(context).items()
                                               if k not in ('state', 'history', 'goals')},
                                'authoritativeState': consequences.prompt_state(context['parent']['branchState']),
                                'goals': consequences.goals_for(context['package'], context['contract'], context['parent']['branchState']),
                                'previous': reading_history(context['lineage']), 'draft': {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}}, ensure_ascii=False)},
                        ]
                        action_review = complete_with_retry(self.gateway, 'complete_json', review_messages, stage='action_review')
                        raw.append(action_review.raw_response)
                        observations.extend({**o, 'generationStage': 'action_review', 'revision': attempt} for o in action_review.observations)
                        review_data = parse_json_content(action_review.content)
                        if result_contract:
                            # Coverage is unconditional: first-pass labels and
                            # linguistic heuristics cannot suppress this check.
                            # Cache only byte-identical prose within this turn.
                            if body not in grounding_cache:
                                grounding_messages = [
                                    {'role': 'system', 'content': render_prompt('reader.scene_grounding')},
                                    {'role': 'user', 'content': json.dumps({'paragraphs': grounding_claims(body),
                                        'priorRepairIssues': pending_repair_issues,
                                        'repairTargets': repair_targets(pending_repair_issues),
                                        'dialogueUnits': dialogue_units(body),
                                        'playerId': context['contract']['persona'].get('sourceCharacterId'),
                                        'people': {key: entity['name'] for key, entity in reader_actions.registry(
                                            context['package'], context['parent']['branchState'], result_contract['introductions']).items()
                                            if entity['kind'] == 'character'},
                                        'draft': {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))},
                                        'input': player_action(context, selected), 'requirements': requirements,
                                        'sceneEvidence': public_scene_evidence(context),
                                        'knowledge': scene_knowledge(context, include_evidence=False),
                                        'boundaries': scene_boundaries(result_contract.get('scenePlan'))}, ensure_ascii=False)},
                                ]
                                grounding = complete_with_retry(self.gateway, 'complete_json', grounding_messages, stage='scene_grounding')
                                grounding_inputs[body] = grounding_messages
                                raw.append(grounding.raw_response)
                                observations.extend({**o, 'generationStage': 'scene_grounding', 'revision': attempt} for o in grounding.observations)
                                grounding_cache[body] = parse_json_content(grounding.content)
                            else:
                                observations.append({'generationStage': 'scene_grounding', 'revision': attempt, 'outcome': 'reused_identical_body'})
                            observations.append({'generationStage': 'independent_review_result', 'revision': attempt,
                                                 'review': grounding_cache[body]})
                            focus = exclusive_requirements(requirements)
                            if focus and body not in scope_cache:
                                scoped = complete_with_retry(self.gateway, 'complete_json', [
                                    {'role': 'system', 'content': render_prompt('reader.scope_review')},
                                    {'role': 'user', 'content': json.dumps({'input': player_action(context, selected),
                                        'requirements': focus,
                                        'draft': {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}}, ensure_ascii=False)},
                                ])
                                raw.append(scoped.raw_response)
                                observations.extend({**o, 'generationStage': 'scope_review', 'revision': attempt} for o in scoped.observations)
                                scope_cache[body] = parse_json_content(scoped.content)
                                observations.append({'generationStage': 'scope_review_result', 'revision': attempt,
                                                     'review': scope_cache[body]})
                            for evidence_attempt in range(2):
                                try:
                                    combined_scene_issues(review_data, body, events, grounding_cache[body],
                                                         evidence=public_scene_evidence(context), requirements=requirements,
                                                         focused=scope_cache.get(body),
                                                         boundaries=scene_boundaries(result_contract.get('scenePlan')),
                                                         people={key for key, entity in reader_actions.registry(context['package'],
                                                             context['parent']['branchState'], result_contract['introductions']).items() if entity['kind'] == 'character'},
                                                         player_id=context['contract']['persona'].get('sourceCharacterId'),
                                                         repair_issues=pending_repair_issues)
                                    break
                                except SceneReviewFormatError as error:
                                    # Keep the prose, extraction and other independent
                                    # audits. A broken reference is not a prose defect.
                                    observations.append({'generationStage': 'scene_evidence_repair', 'revision': attempt,
                                                         'evidenceAttempt': evidence_attempt, 'error': str(error)})
                                    if evidence_attempt or error.stages != {'scene_grounding'}:
                                        raise LlmError(str(error), 'model_output_rejected') from error
                                    corrected = complete_with_retry(self.gateway, 'complete_json', grounding_inputs[body] + [
                                        {'role': 'assistant', 'content': json.dumps(grounding_cache[body], ensure_ascii=False)},
                                        {'role': 'user', 'content': render_prompt('reader.grounding_repair', error=str(error))},
                                    ], stage='scene_evidence_repair')
                                    raw.append(corrected.raw_response)
                                    observations.extend({**o, 'generationStage': 'scene_evidence_repair', 'revision': attempt} for o in corrected.observations)
                                    grounding_cache[body] = parse_json_content(corrected.content)
                                    observations.append({'generationStage': 'independent_review_result', 'revision': attempt,
                                                         'evidenceAttempt': evidence_attempt + 1, 'review': grounding_cache[body]})
                        else:
                            reject_review_issues(review_data, body, events)
                        reader_outcome = validate_action_requirements(review_data, requirements, body)
                        if result_contract:
                            candidate_contract, candidate_state, candidate_authority = result_contract, resolved_state, authority_review
                            additions = review_data.get('additionalChanges', [])
                            if not isinstance(additions, list) or len(additions) > 12:
                                raise ValueError('正文后果补记格式无效')
                            if additions:
                                candidate_contract = consequences.validate_plan({**result_contract, 'stateChanges': result_contract['stateChanges'] + additions}, requirements, context)
                                candidate_authority = self._check_action_authority(context, player_action(context, selected), candidate_contract, raw, observations)
                                candidate_state = consequences.projected_state(turn_base_state, candidate_contract, context['package'])
                            try:
                                scene_checks = validate_scene_review(review_data, body, public_scene_evidence(context), events)
                                reader_actions.validate_events(review_data, events, candidate_contract, context['parent']['branchState'], context['package'])
                                consequences.validate_final_state(review_data, candidate_state)
                                consequence_update = consequences.validate_review(review_data, candidate_contract, body, reader_outcome)
                            except (consequences.ConsequenceEvidenceError, reader_actions.ActionEvidenceError) as error:
                                # Fix evidence coverage without regenerating valid prose.
                                repaired_review = complete_with_retry(self.gateway, 'complete_json', review_messages + [
                                    {'role': 'assistant', 'content': action_review.content},
                                    {'role': 'user', 'content': render_prompt('reader.evidence_repair',
                                        error=str(error),
                                        candidate_contract_json=json.dumps(candidate_contract, ensure_ascii=False),
                                    )},
                                ])
                                raw.append(repaired_review.raw_response)
                                observations.extend({**o, 'generationStage': 'consequence_evidence_repair', 'revision': attempt} for o in repaired_review.observations)
                                review_data = parse_json_content(repaired_review.content)
                                reject_review_issues(review_data, body, events)
                                reader_outcome = validate_action_requirements(review_data, requirements, body)
                                try:
                                    scene_checks = validate_scene_review(review_data, body, public_scene_evidence(context), events)
                                    reader_actions.validate_events(review_data, events, candidate_contract, context['parent']['branchState'], context['package'])
                                    consequences.validate_final_state(review_data, candidate_state)
                                    consequence_update = consequences.validate_review(review_data, candidate_contract, body, reader_outcome)
                                except (consequences.ConsequenceEvidenceError, reader_actions.ActionEvidenceError) as error:
                                    raise LlmError(str(error), 'model_output_rejected') from error

                            result_contract, resolved_state, authority_review = candidate_contract, candidate_state, candidate_authority

                except ValueError as error:
                    if repair_record is not None:
                        record_repair_failure(repair_record, error, 'full_review', 'failed_full_review')
                        repair_record = None
                    observations.append({'generationStage': 'validation', 'revision': attempt,
                                         'outcome': 'failed', 'error': str(error),
                                         **({'repairIssues': error.repair_problem()} if isinstance(error, SceneReviewError) else {})})
                    if attempt == 2:
                        raise LlmError(str(error), 'model_output_rejected') from error
                    problem = error.repair_problem() if isinstance(error, SceneReviewError) else str(error)
                    if isinstance(error, SceneReviewError):
                        for issue in problem['issues']:
                            issue['beforeOccurrences'] = body.count(issue['quote'])
                        pending_repair_issues = problem
                        grounding_cache.clear()
                    failure = json.dumps(problem, ensure_ascii=False) if isinstance(problem, dict) else problem
                    if isinstance(error, SceneReviewError) and error.unlocated:
                        observations.append({'generationStage': 'local_repair', 'outcome': 'skipped',
                                             'reason': 'unlocated_semantic_issues', 'problem': problem})
                        continue
                    if isinstance(error, SceneReviewError):
                        rejected_ids = {v['paragraphId'] for v in error.violations}
                        rejected_size = sum(narrative_character_count(p) for pid, p in
                            {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}.items() if pid in rejected_ids)
                        if len(rejected_ids) > 8 or rejected_size > max(200, count // 2):
                            observations.append({'generationStage': 'local_repair', 'outcome': 'skipped',
                                                 'reason': 'repair_scope_exceeded', 'problem': problem, 'beforeBody': body})
                            continue
                    if 0 < count <= MAX_CHAPTER_CHARACTERS:
                        current = api_routes.scene(context['package'], context['parent']['branchState'])
                        material = api_routes.turn_material(context['package'], context['parent']['branchState'], selected)
                        if current:
                            material += api_routes.fact_sheet(context['package'], context['parent']['branchState'], interlude)
                        material += '\n必须兑现的玩家行动：' + player_action(context, selected)
                        if result_contract:
                            material += '\n结果契约与永久状态：' + json.dumps({'contract': result_contract, 'state': consequences.prompt_state(resolved_state)}, ensure_ascii=False)
                        repair_record = {'generationStage': 'local_repair_record', 'revision': attempt,
                                         'beforeBody': body, 'afterBody': None, 'issues': problem,
                                         'issueTypes': repair_issue_types(problem), 'repairResponse': None,
                                         'outcome': 'requested', 'failureReason': None,
                                         'failureStage': None, 'failureCode': None,
                                         'failureIssues': None, 'failureIssueTypes': []}
                        observations.append(repair_record)
                        fixed = complete_with_retry(self.gateway, 'complete_json', [
                            {'role': 'system', 'content': perspective_rule(name) + render_prompt('reader.narrative_repair')},
                            {'role': 'user', 'content': json.dumps({'problem': {
                                **problem, 'issues': [{k: v for k, v in issue.items() if k != 'quote'} for issue in problem['issues']]
                            } if isinstance(problem, dict) else problem, 'scene': material, 'sceneEvidence': public_scene_evidence(context),
                                'pacing': self.last_prompt_context.get('pacing', {}),
                                'dialogueDependencies': repair_dialogue_dependencies(body),
                                'originalParagraphCjk': {f'P{i+1}': len(re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]', p))
                                                         for i, p in enumerate(body.split('\n\n'))},
                                'paragraphs': repair_paragraphs(body, error)}, ensure_ascii=False)},
                        ], stage='local_repair')
                        raw.append(fixed.raw_response)
                        observations.extend({**o, 'generationStage': 'local_repair', 'revision': attempt} for o in fixed.observations)
                        repair_record['repairResponse'] = fixed.content
                        try:
                            repaired_body = apply_scene_repairs(body, parse_json_content(fixed.content).get('replacements'), getattr(error, 'violations', ()))
                            repair_record.update(afterBody=repaired_body, outcome='pending_full_review')
                            observations.append({'generationStage': 'local_repair_validation', 'revision': attempt,
                                                 'outcome': 'pending_full_review'})
                        except (ValueError, LlmError) as repair_error:
                            record_repair_failure(repair_record, repair_error, 'repair_validation', 'rejected')
                            repair_record = None
                            observations.append({'generationStage': 'local_repair_validation', 'revision': attempt,
                                                 'outcome': 'rejected', 'error': str(repair_error)})
                            repaired_body = None
                    continue
                if repair_record is not None:
                    repair_record.update(outcome='passed_full_review')
                    repair_record = None
                if stream and not body_was_streamed:
                    stream(body)
                break
            directions = scripted_followup_directions(context, selected, resolved_state)
            if api_routes.role_name(context['package'], resolved_state):
                directions = api_routes.directions(context['package'], resolved_state)
            elif resolved_state.get('storyScope') == 'source':
                directions = player_directions(context['package'], resolved_state) or directions
            directions = consequences.filter_directions(directions, context['package'], resolved_state)
            result = plan_result(context, selected, body, selected['title'], directions, 'medium')
            result['readingProfile'] = {**self.last_prompt_context.get('pacing', {}),
                                        'actualCjk': cjk_character_count(body),
                                        'expansionApplied': False}
            result['naturalEnding'] = bool(self.last_prompt_context.get('terminalSourceBranch'))
            result['branchAdditions'] = empty_branch_additions()
            if consequence_update:
                result['sceneChecks'] = scene_checks
                result['actionIntent'] = {'input': player_action(context, selected), 'requirements': result_contract['requirements']}
                result['consequenceUpdate'] = consequence_update
                result['consequenceReview'] = consequences.VERSION
                result['reviewedNarrativeSha256'] = hashlib.sha256(body.encode()).hexdigest()
                result['authorityReview'] = authority_review
                result['observedEvents'] = events
                result['eventChecks'] = review_data['eventChecks']
                summary = consequences.public_summary(consequence_update, context['package'], resolved_state)
                if summary and reader_outcome:
                    reader_outcome['action']['summary'] = summary
                    reader_outcome['action']['evidence'] = (consequence_update['outcomes'] + consequence_update['goalUpdates'])[0]['evidence']
            if reader_outcome:
                result['readerOutcome'] = reader_outcome
            current = api_routes.scene(context['package'], context['parent']['branchState'])
            if current:
                result['summary'] = selected['title'] if interlude else current[2]
                if reader_outcome:
                    result['readerOutcome'] = reader_outcome
                result['openThreads'] = [d['title'] for d in directions]
                result['factDeltas'] = [{'id': 'fact_player_scene_' + str(api_routes.completed_steps(resolved_state)),
                                         'source': 'source', 'summary': result['summary']}]
                result['storyArc']['chapter'] = {'title': result['summary'], 'status': 'continuing' if directions else 'completed'}
            return result, {'operation': 'branch_planner', 'model': self.gateway.model, 'promptVersion': 'web-player-v3+' + catalog_version(),
                            'requestSummary': selected['title'], 'rawResponse': '\n'.join(raw),
                            'callObservations': observations, 'promptContext': self.last_prompt_context}
        except LlmError as error:
            if repair_record is not None:
                reviewing = repair_record['outcome'] == 'pending_full_review'
                record_repair_failure(repair_record, error,
                                      'full_review' if reviewing else 'repair_request',
                                      'failed_full_review' if reviewing else 'failed')
            # A readable draft is not an accepted branch. Preserve the last
            # complete candidate across reset/transport failure, privately.
            error.retained_body = retained_body
            observations.extend(error.observations)
            error.audit = {'operation': 'branch_planner', 'model': self.gateway.model, 'promptVersion': 'web-player-v3+' + catalog_version(),
                           'requestSummary': selected['title'], 'rawResponse': '\n'.join(raw), 'error': str(error),
                           'callObservations': observations, 'promptContext': self.last_prompt_context,
                           'retainedDraft': {'text': retained_body, 'status': 'unconfirmed'}}
            raise

    def opening(self, package, root, name, stream=None, stream_reset=None):
        character = next((c for c in package['characters'] if c['name'] == name), {})
        known_identity = character.get('menuDescription') or character.get('description', '')
        if root.get('openingContext'):
            # Evidence quotes may describe earlier ownership; the current
            # snapshot takes precedence and alone belongs in the prose prompt.
            known_identity = json.dumps({k: v for k, v in root['openingContext'].items()
                                         if k not in ('evidence', 'sourceSha256', 'sourceCutoff', 'sourceCutoffLine')}, ensure_ascii=False)
        prompt = (render_prompt('reader.opening',
            perspective_rule=perspective_rule(name),
            known_identity=known_identity,
            source_opening=root['narrativeText'],
        ))
        if root.get('openingContext'):
            prompt += (render_prompt('reader.opening_polish'))
        failure = ''
        for attempt in range(2):
            if attempt and stream_reset:
                stream_reset('opening_revision')
            completion = complete_with_retry(self.gateway, 'complete_text', [
                {'role': 'system', 'content': self.system_instruction + '开场只扩写给定片段，不写后续行动，不增加往事。'},
                {'role': 'user', 'content': prompt + failure},
            ], stream, stream_reset)
            text = plain_model_narrative(completion.content)
            try:
                opening_count = cjk_character_count(text)
                if not text.strip() or opening_count < MIN_SCENE_CJK or opening_count > MAX_CHAPTER_CHARACTERS:
                    raise ValueError(f'开场须有{MIN_SCENE_CJK}至{MAX_CHAPTER_CHARACTERS}个非空白字符')
                check_player_voice(text, name)
                if api_routes.role_name(package, root['branchState']):
                    continuity_check(text, name, root['narrativeText'])
                guard_narrative(text, root['branchState'], [], package['world']['narrativeGuidelines'], package)
                guard_source_character_names(text, package, root['branchState'], session_persona={'name': name})
                if root.get('openingContext'):
                    review = complete_with_retry(self.gateway, 'complete_json', [
                        {'role': 'system', 'content': render_prompt('reader.opening_review')},
                        {'role': 'user', 'content': json.dumps({'openingContext': json.loads(known_identity),
                         'state': root['branchState'], 'sourceOpening': root['narrativeText'], 'draft': text}, ensure_ascii=False)},
                    ])
                    verdict = parse_json_content(review.content)
                    if not isinstance(verdict, dict) or verdict.get('passed') is not True or verdict.get('issues') != []:
                        raise ValueError('开局事实复核未通过：' + str(verdict))
            except ValueError as error:
                if attempt:
                    raise LlmError(str(error), 'model_output_rejected') from error
                failure = '\n上次草稿未通过：' + str(error) + '。重新按素材写一份开场，不使用被指出的物品或虚构事实，不能沿用错误设定。'
                continue
            if stream and not completion.body_was_streamed:
                stream(text)
            if root.get('openingContext'):
                root['openingGeneration'] = {'mode': 'live_model', 'attempts': attempt + 1, 'factReview': 'passed', 'promptVersion': catalog_version()}
            return text
