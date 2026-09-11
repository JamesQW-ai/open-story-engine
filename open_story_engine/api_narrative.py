"""Player-facing narration policy. Source packages and CLI policy stay code-owned."""
import json
import re

from .cocreation import (LlmPlanner, guard_narrative, guard_source_character_names,
                        narrative_character_count, narrative_state_guardrails,
                        plain_model_narrative, narration_outside_dialogue,
                        guard_repeated_paragraphs, plan_result, scripted_followup_directions, empty_branch_additions)
from .llm import LlmError, parse_json_content
from .api_journey import player_directions
from .api_openings import RAINY_KNOWN_CLUES
from . import api_routes


MAX_CHAPTER_CHARACTERS = 3500


def player_package(package, character_id=None):
    guidelines = {**package['world'].get('narrativeGuidelines', {}),
                  'perspective': 'second_person_limited'}
    if character_id:
        guidelines['focalCharacterId'] = character_id
    if package.get('sourceAnalysis', {}).get('sha256') == api_routes.RAINY_SOURCE:
        guidelines['ledgerItemAvailability'] = True
    return {**package, 'world': {**package['world'], 'narrativeGuidelines': guidelines}}


def perspective_rule(name):
    return (f'玩家角色是{name}。正文全程以第二人称“你”指代这个角色，NPC 使用名字或他/她。'
            '严禁“你是某某，你是一个……”的身份说明，直接从感官、动作、对话切入，不必句句以你开头。'
            '严格限知：只写你当场的感知、行动、心理和已经得知的事。'
            '不得描写不在场的人正在做什么，不得揭示 NPC 内心或原著后续秘密。'
            '用户没读过小说，但角色并未失忆；只通过已提供的角色记忆自然补充必要背景。')


def check_player_voice(text, name):
    prose = narration_outside_dialogue(text)
    if '你' not in prose or re.search(r'你是\s*' + re.escape(name), prose):
        raise ValueError(f'玩家{name}必须称为“你”，禁止用“{name}／他／她”叙述玩家，也禁止“你是{name}”式介绍')
    if re.search(r'(?:^|[。！？\n])\s*' + re.escape(name) + r'(?:抬|低|走|看|听|想|推|伸|把|感到|意识到)', prose):
        raise ValueError('正文将玩家写回了第三人称')


def trim_optional_paragraphs(body, candidates, maximum=MAX_CHAPTER_CHARACTERS):
    paragraphs = body.split('\n\n')
    total = narrative_character_count(body)
    # Only the editor's optional paragraphs are eligible. Protect the entry and
    # final outcome; choose a subset by exact size instead of chopping the ending.
    subsets = {0: []}
    for key in dict.fromkeys(key for key in candidates[:20] if isinstance(key, str)):
        if not isinstance(key, str) or not re.fullmatch(r'P\d+', key):
            continue
        index = int(key[1:]) - 1
        if not 0 < index < len(paragraphs)-2:
            continue
        size = narrative_character_count(paragraphs[index])
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


def complete_with_retry(gateway, kind, messages, stream=None, stream_reset=None):
    """Retry one transient provider failure at the failed call, not the whole turn."""
    previous = []
    for attempt in range(2):
        try:
            result = getattr(gateway, kind)(messages, stream, stream_reset)
            result.observations = previous + result.observations
            return result
        except LlmError as error:
            if attempt or error.code not in ('transport_error', 'empty_stream', 'empty_json'):
                error.observations = previous + error.observations
                raise
            previous = [{**o, 'retryReason': 'transient_provider_failure'} for o in error.observations]
            if stream and stream_reset:
                stream_reset('connection_retry')


def apply_scene_repairs(body, replacements):
    paragraphs = body.split('\n\n')
    if not isinstance(replacements, list) or not 1 <= len(replacements) <= 8:
        raise ValueError('局部修订必须提供1—8个段落')
    edits = {}
    for replacement in replacements:
        if not isinstance(replacement, dict):
            raise ValueError('局部修订格式无效')
        key, text = replacement.get('paragraphId'), replacement.get('text')
        if not isinstance(key, str) or not re.fullmatch(r'P\d+', key) or not isinstance(text, str) or not text.strip():
            raise ValueError('局部修订缺少有效的段落编号或正文')
        index = int(key[1:])-1
        if not 0 <= index < len(paragraphs) or index in edits:
            raise ValueError('局部修订的段落不存在或重复')
        edits[index] = text.strip()
    if all(paragraphs[i].strip() == text for i, text in edits.items()):
        raise ValueError('局部修订原样返回了有问题的段落，没有实际修正')
    if sum(narrative_character_count(paragraphs[i]) for i in edits) > max(200, narrative_character_count(body)//2):
        raise ValueError('局部修订超出原文一半，不能伪装成局部修正')
    return '\n\n'.join(edits.get(i, p) for i, p in enumerate(paragraphs))


class PlayerNarrativePlanner(LlmPlanner):
    """Reuse core fact guards and metadata; expose only established player history."""
    interactive_reader = True
    published_directions = staticmethod(api_routes.directions)
    prepare_direction = staticmethod(api_routes.prepare_direction)
    system_instruction = (
        '你是中文互动小说叙事者，玩家全程称为“你”，严格采用限知视角。'
        '行动必须得到具体回应，按本回合已确认的事件顺序推进，人物说话有目的、前后有因果。'
        '玩家未选的重大决定不能代做；环境和NPC可以按已登记事件回应玩家。'
        '不得擅自增加道具、线索、远程消息、场外行动或下一阶段事件。'
        '禁止自编过去的会面、电话、短信、调查经历；没有提供的往事不写。'
        '不得通过新划痕、脚印、神秘人影、突然出现的工具制造线索。'
        '已完成的动作不要重新执行，已确认的线索不要重新发现。'
        '按本章行动复杂度自行安排篇幅：简单回应简洁写，救援、转场或多人交锋按因果展开。'
        '不设最低字数或固定段数，不为篇幅扩写。复杂章节最多3500个非空白字符，保留完整结果后自然停笔。'
        '对话与相邻动作合成自然段，不要每句台词单独成段；不输出段号。'
        '用行动、阻碍、不同应对和结果支撑篇幅，不重复雨声、门板、呼吸和犹豫凑字数。'
        '不要标题、菜单、解释或游戏系统术语。'
    )

    def validation_scope(self, context, scope):
        current = api_routes.scene(context['package'], context['parent']['branchState'])
        if current:
            return {**scope, 'narrativeBrief': [*scope.get('narrativeBrief', []), {'text': current[3]}]}
        return scope

    def _prompt(self, context, selected, state, repair, terminal_source_branch=False):
        self.writing_scope = None
        self.last_prompt_context = {'mode': 'player_history', 'perspective': 'second_person_limited'}
        persona = context['contract']['persona']
        parent = context['parent']['branchState']
        changes = {k: {'from': parent.get(k), 'to': v} for k, v in state.items()
                   if parent.get(k) != v and k not in ('branchLedger', 'freeTextProgress')}
        latest = context['parent'].get('narrativeText', '')
        history = latest if len(latest) <= 1400 else latest[:650] + '\n[中间省略]\n' + latest[-650:]
        names = [c['name'] for c in context['package']['characters']]
        places = [p['name'] for p in context['package']['locations']]
        place_names = {p['id']: p['name'] for p in context['package']['locations']}
        final_people = {c['name']: place_names.get(state.get('characterLocationIds', {}).get(c['id']))
                        for c in context['package']['characters'] if c['id'] in state.get('characterLocationIds', {})}
        current = api_routes.scene(context['package'], parent)
        if current:
            # Earlier prototype prose over-elaborated unconfirmed scenery.
            # Use code-confirmed history and a short handoff, not pages of that
            # stylistic repetition as the next chapter's imitation template.
            known = RAINY_KNOWN_CLUES.get(persona['name'], [])
            completed = [e['summary'] for e in parent.get('derivedEvents', []) if e['id'].startswith(api_routes.PREFIX)]
            history = '\n'.join(known + completed) + '\n上一页的收尾（未确认的猜测依然只是猜测）：\n' + latest[-280:]
        event_brief = (current[3] + '\n' + api_routes.fact_sheet(context['package'], parent)) if current else selected['summary']
        ending = bool(current and api_routes.completed_steps(parent) == api_routes.STEPS-1)
        self.last_prompt_context['scene'] = current[2] if current else selected['title']
        return f'''{perspective_rule(persona['name'])}
玩家选择的行动：{selected['title']}。{selected['summary']}
已经读到的故事（唯一可向玩家复述的背景；历史中的第三人称在本回合改为你）：
{history}
本回合经规则确认的变化：{json.dumps(changes, ensure_ascii=False)}
内部事实约束（仅用于避免矛盾，不得向玩家介绍、复述或泄露未知信息）：
{narrative_state_guardrails(context['package'], state)}
最终地点 ID：{state.get('playerLocationId', state.get('currentLocationId'))}
本章收尾时已在场人物的地点：{json.dumps(final_people, ensure_ascii=False)}。完成转场后用自然动作交代同行者也已抵达，不把他们最后留在途经地点。
地点名与 ID：{json.dumps({p['id']: p['name'] for p in context['package']['locations']}, ensure_ascii=False)}
已登记姓名：{'、'.join(names)}。地点：{'、'.join(places)}。登记不表示你认识此人或知道此地。
本回合事件材料（按这个顺序展开，内容已由规则登记；不要把指令本身写入小说）：
{event_brief}
除此之外的事实不变。不得自创新物品、新证据、新通信结果或替你执行下一步行动。
通过具体动作、感官、对话与犹豫展开当前行动；只写当场可见的后果，未知问题保持未知。
先按本回合的事件数量、转场和冲突复杂度规划篇幅，无需输出计划。简单剧情简洁完成，复杂剧情完整写清因果，最多3500个非空白字符。没有最低字数，不强行延长对话或补环境描写。将对话和相邻动作组成自然段。
只输出正文，不含标题、字数、选项、系统说明或插图标记。{'这是本路线的结局，明确回应最初目标，自然收束，不再悬置同一个问题。' if ending or terminal_source_branch else '停在需要你作出下一步决定的位置。'}
{('修正要求：' + repair) if repair else ''}'''

    def _scene_draft(self, context, selected, state, stream, stream_reset, raw, observations, attempt, repair):
        completion = complete_with_retry(self.gateway, 'complete_text', [
            {'role': 'system', 'content': self.system_instruction},
            {'role': 'user', 'content': self._prompt(context, selected, state, repair)},
        ], stream, stream_reset)
        raw.append(completion.raw_response)
        observations.extend({**o, 'generationStage': 'chapter', 'revision': attempt} for o in completion.observations)
        return plain_model_narrative(completion.content), completion.body_was_streamed

    def plan(self, context, selected, resolved_state, stream=None, stream_reset=None, **kwargs):
        name = context['contract']['persona']['name']
        body = ''
        observations, raw = [], []
        try:
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
                count = narrative_character_count(body)
                if count > MAX_CHAPTER_CHARACTERS:
                    edit = complete_with_retry(self.gateway, 'complete_json', [
                        {'role': 'system', 'content': '给小说做删节建议，只返回JSON：{"removable":["P3","P6"]}。从提供的段落中选出可以整段删除的重复渲染、纯气氛或已经说明过的心理，最多20段。严禁选关键行动、线索、首次出现的人物、对话的必要回应或结局。不要改写正文，不要选择首段和最后两段。'},
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
                    count = narrative_character_count(body)
                    if count <= MAX_CHAPTER_CHARACTERS and stream_reset and stream:
                        stream_reset('chapter_edited')
                        stream(body)
                        body_was_streamed = True
                try:
                    check_player_voice(body, name)
                    guard_repeated_paragraphs(body)
                    check_reader_repetition(body)
                    count = narrative_character_count(body)
                    if not count:
                        raise ValueError('本章未返回正文，必须写出实际发生的行动与结果')
                    if count > MAX_CHAPTER_CHARACTERS:
                        raise ValueError(f'本章为{count}字，超过单次{MAX_CHAPTER_CHARACTERS}字上限。删除非必要渲染，保留完整因果与阶段结果，不要截断结尾。')
                    guard_narrative(body, resolved_state, context['characterDetails'],
                                    context['package']['world']['narrativeGuidelines'], context['package'])
                    guard_source_character_names(body, context['package'], resolved_state,
                                                 session_persona=context['contract']['persona'])
                    api_routes.check_scene_result(context['package'], context['parent']['branchState'], body)
                    current = api_routes.scene(context['package'], context['parent']['branchState'])
                    if current:
                        ending_review = complete_with_retry(self.gateway, 'complete_json', [
                            {'role': 'system', 'content': '只根据小说正文提取章末实际状态，输出JSON对象：final_location为玩家最后所在的已登记地点中文名；door_open为信号室铁门是否已打开（布尔或null）；tang_safe为唐栖是否脱困（布尔或null）；handoff_confirmed为司机是否明确接手事故上报（布尔）；evidence为正文结尾直接支持状态的一句原文，逐字引用。玩家用你叙述。沿隧道走但未到候车厅，最后地点仍是维修隧道。'},
                            {'role': 'user', 'content': json.dumps({'player': name, 'locations': [p['name'] for p in context['package']['locations']], 'draft': body}, ensure_ascii=False)},
                        ])
                        raw.append(ending_review.raw_response)
                        observations.extend({**o, 'generationStage': 'ending_review', 'revision': attempt} for o in ending_review.observations)
                        try:
                            ending = parse_json_content(ending_review.content)
                        except LlmError as error:
                            raise ValueError('章末核对没有返回有效 JSON') from error
                        api_routes.check_reviewed_ending(context['package'], context['parent']['branchState'], body, ending)
                        review = complete_with_retry(self.gateway, 'complete_json', [
                            {'role': 'system', 'content': '根据唯一允许的事实审查正文。不评价文风，只查编造的过去会面、记录内容、工具、证据或直接矛盾。返回JSON对象 {"issues":["问题原句及违反的事实"]}，每项引用原句并解释违反哪条事实，没有问题返回空数组。'},
                            {'role': 'user', 'content': json.dumps({'facts': '正文中的你指' + name + '。' + api_routes.fact_sheet(context['package'], context['parent']['branchState'])
                                + '\n此前已完成的事件：' + '；'.join(e['summary'] for e in context['parent']['branchState'].get('derivedEvents', []) if e['id'].startswith(api_routes.PREFIX)),
                                'scene': current[3], 'draft': body}, ensure_ascii=False)},
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
                except ValueError as error:
                    observations.append({'generationStage': 'validation', 'revision': attempt,
                                         'outcome': 'failed', 'error': str(error)})
                    if attempt == 2:
                        raise LlmError(str(error), 'model_output_rejected') from error
                    failure = str(error)
                    if 0 < count <= MAX_CHAPTER_CHARACTERS:
                        current = api_routes.scene(context['package'], context['parent']['branchState'])
                        material = (current[3] + api_routes.fact_sheet(context['package'], context['parent']['branchState'])) if current else selected['summary']
                        fixed = complete_with_retry(self.gateway, 'complete_json', [
                            {'role': 'system', 'content': perspective_rule(name) + '只修复指出的剧情硬问题，保留其余正文。输出JSON：{"replacements":[{"paragraphId":"P3","text":"该段修复后的完整正文"}]}。最多8段、不能超过原文一半。必须实质消除错误，严禁原样返回问题段落。如果问题是未取得钥匙，则删除钥匙和开锁行为，换成素材允许的保管方式，不能继续写任何钥匙。按修复需要调整段落长短，无字数下限，整章最多3500字，不重写整章，不加入新的往事、道具或线索。'},
                            {'role': 'user', 'content': json.dumps({'problem': failure, 'scene': material,
                                'paragraphs': {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}}, ensure_ascii=False)},
                        ])
                        raw.append(fixed.raw_response)
                        observations.extend({**o, 'generationStage': 'local_repair', 'revision': attempt} for o in fixed.observations)
                        try:
                            repaired_body = apply_scene_repairs(body, parse_json_content(fixed.content).get('replacements'))
                        except (ValueError, LlmError):
                            repaired_body = None
                    continue
                if stream and not body_was_streamed:
                    stream(body)
                break
            directions = scripted_followup_directions(context, selected, resolved_state)
            if api_routes.role_name(context['package'], resolved_state):
                directions = api_routes.directions(context['package'], resolved_state)
            elif resolved_state.get('storyScope') == 'source':
                directions = player_directions(context['package'], resolved_state) or directions
            result = plan_result(context, selected, body, selected['title'], directions, 'medium')
            result['branchAdditions'] = empty_branch_additions()
            current = api_routes.scene(context['package'], context['parent']['branchState'])
            if current:
                result['summary'] = current[2]
                result['openThreads'] = [d['title'] for d in directions]
                result['factDeltas'] = [{'id': 'fact_player_scene_' + str(api_routes.completed_steps(resolved_state)),
                                         'source': 'source', 'summary': current[2]}]
                result['storyArc']['chapter'] = {'title': current[2], 'status': 'continuing' if directions else 'completed'}
            return result, {'operation': 'branch_planner', 'model': self.gateway.model, 'promptVersion': 'web-player-v2',
                            'requestSummary': selected['title'], 'rawResponse': '\n'.join(raw),
                            'callObservations': observations, 'promptContext': self.last_prompt_context}
        except LlmError as error:
            observations.extend(error.observations)
            error.audit = {'operation': 'branch_planner', 'model': self.gateway.model, 'promptVersion': 'web-player-v2',
                           'requestSummary': selected['title'], 'rawResponse': '\n'.join(raw), 'error': str(error),
                           'callObservations': observations, 'promptContext': self.last_prompt_context}
            raise

    def opening(self, package, root, name, stream=None, stream_reset=None):
        character = next((c for c in package['characters'] if c['name'] == name), {})
        known_identity = character.get('menuDescription') or character.get('description', '')
        prompt = (perspective_rule(name) + '\n角色自知的身份资料（融入动作与记忆，不要照抄身份介绍）：' + known_identity + '\n根据场景复杂度写出自然的身份开场，不设字数下限，最多3500字。'
                  '只展开现有场景的感受和行动，不新增线索、物品、NPC或通信，不执行末尾的待选行动。'
                  '只输出正文，不含身份说明、标题或选项。\n' + root['narrativeText'])
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
                if not text.strip() or narrative_character_count(text) > MAX_CHAPTER_CHARACTERS:
                    raise ValueError('开场须有完整正文，且不超过3500个非空白字符')
                check_player_voice(text, name)
                guard_narrative(text, root['branchState'], [], package['world']['narrativeGuidelines'], package)
                guard_source_character_names(text, package, root['branchState'], session_persona={'name': name})
            except ValueError as error:
                if attempt:
                    raise LlmError(str(error), 'model_output_rejected') from error
                failure = '\n上次草稿未通过：' + str(error) + '。重新按素材写一份开场，不使用被指出的物品或虚构事实，不能沿用错误设定。'
                continue
            if stream and not completion.body_was_streamed:
                stream(text)
            return text
