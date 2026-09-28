"""Player-facing narration policy. Source packages and CLI policy stay code-owned."""
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

from .prompts import render_prompt, catalog_version
from . import reader_consequences as consequences
from . import reader_actions

from .cocreation import (LlmPlanner, REPAIR_PLACEHOLDERS, guard_narrative, guard_source_character_names,
                        narrative_character_count, narrative_state_guardrails,
                        plain_model_narrative, narration_outside_dialogue,
                        guard_repeated_paragraphs, plan_result, scripted_followup_directions, empty_branch_additions)
from .llm import LlmError, OpenAICompatibleGateway, parse_json_content
from .context_bundle import ContextBundleError, context_fact_terms
from .action_review_context import action_review_history
from .context_budget import estimate_text_tokens
from .api_journey import player_directions
from .api_openings import RAINY_KNOWN_CLUES
from . import api_routes
from .api_reader_quality import (validate_outcome, continuity_check, reading_history,
                                 action_requirements, validate_action_requirements, scene_pacing)


from .reader_scene_review import public_scene_evidence, validate_scene_review, grounding_claims, grounding_input_evidence, scene_speaker_candidates, validate_grounding, repair_paragraphs, reject_review_issues, SceneReviewError, scene_knowledge, combined_scene_issues, exclusive_requirements

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
            # The gateway has already spent this request's full deadline when
            # both transports failed. Retrying the identical review would add
            # another silent wait without changing the candidate or evidence.
            transport_already_fell_back = any(
                observation.get('retryReason') in ('transport_fallback', 'json_transport_fallback')
                for observation in error.observations
            )
            if attempt or transport_already_fell_back or error.code not in ('transport_error', 'empty_stream', 'empty_json'):
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


REPAIR_EVIDENCE_MAX_ITEMS = 8
REPAIR_EVIDENCE_MAX_CHARS = 6000
REPAIR_FACTS_MAX_ITEMS = 10
REPAIR_FACTS_MAX_CHARS = 3200
CHAPTER_FACTS_MAX_ITEMS = 10
CHAPTER_FACTS_MAX_CHARS = 3200


def projection_observability(projection, *, excluded_fields=(), excluded_reasons=None):
    """Return bounded size and omission metrics for a stage projection."""
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    fields = list(dict.fromkeys(str(field) for field in excluded_fields))
    reasons = excluded_reasons or {field: 'stage_boundary' for field in fields}
    return {
        'contextSha256': projection.get('contextSha256'),
        'serializedChars': len(encoded),
        'estimatedTokens': estimate_text_tokens(encoded),
        'excludedFields': fields,
        'excludedReasons': {field: reasons.get(field, 'stage_boundary') for field in fields},
    }


def _repair_evidence_terms(problem):
    terms = set()
    if not isinstance(problem, dict):
        return terms
    for issue in problem.get('issues', []):
        if not isinstance(issue, dict):
            continue
        text = ' '.join(str(issue.get(key, '')) for key in ('claim', 'quote', 'reason'))
        terms.update(context_fact_terms(text))
    return terms


def select_repair_evidence(context, problem, *, evidence=None):
    """Select a bounded, issue-linked subset of public evidence for repair."""
    evidence = public_scene_evidence(context) if evidence is None else dict(evidence)
    issue_types = set(repair_issue_types(problem))
    terms = _repair_evidence_terms(problem)
    explicit_ids = set()
    for issue in problem.get('issues', []) if isinstance(problem, dict) else []:
        if not isinstance(issue, dict):
            continue
        refs = issue.get('sourceIds') or issue.get('sources') or []
        for ref in refs if isinstance(refs, list) else []:
            if isinstance(ref, str):
                explicit_ids.add(ref)
            elif isinstance(ref, dict) and isinstance(ref.get('id'), str):
                explicit_ids.add(ref['id'])

    scored = []
    history_ids = [source_id for source_id in evidence if source_id.startswith('history-')]
    recent_history = set(history_ids[-2:]) if 'continuity' in issue_types else set()
    for position, (source_id, content) in enumerate(evidence.items()):
        if not isinstance(content, str) or not content:
            continue
        score = 0
        if source_id in explicit_ids:
            score += 10000
        if source_id in recent_history:
            score += 1000
        score += sum(content.count(term) for term in terms)
        if score > 0:
            scored.append((-score, position, source_id, content))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))

    selected = {}
    selected_chars = 0
    for _, _, source_id, content in scored:
        if source_id in selected:
            continue
        if len(selected) >= REPAIR_EVIDENCE_MAX_ITEMS:
            break
        if selected_chars + len(content) > REPAIR_EVIDENCE_MAX_CHARS:
            continue
        selected[source_id] = content
        selected_chars += len(content)
    return selected, {
        'selectedSourceIds': list(selected),
        'selectedChars': selected_chars,
        'omittedSourceCount': max(0, len(evidence) - len(selected)),
        'maxItems': REPAIR_EVIDENCE_MAX_ITEMS,
        'maxChars': REPAIR_EVIDENCE_MAX_CHARS,
    }


def select_repair_fixed_facts(context, selected, problem, *, bundle=None):
    """Select issue-linked hard facts by stable ID and sentence boundary."""
    fact_source = 'legacy_fact_sheet'
    fact_items = []
    if bundle is not None:
        try:
            fact_items = bundle.project('repair').get('hardConstraints', {}).get('immutableFacts', [])
        except (AttributeError, TypeError, ValueError):
            fact_items = []
        if fact_items:
            fact_source = 'context_bundle'
    if not fact_items:
        fact_items = api_routes.fact_sheet_items(
            context['package'], context['parent']['branchState'], bool(selected.get('readerInterlude')),
        )
    if not isinstance(fact_items, list) or not fact_items:
        return [], {
            'selectedFactIds': [], 'selectedChars': 0,
            'omittedFactCount': 0, 'maxItems': REPAIR_FACTS_MAX_ITEMS,
            'maxChars': REPAIR_FACTS_MAX_CHARS, 'source': fact_source,
        }
    terms = _repair_evidence_terms(problem)
    scored = []
    for index, item in enumerate(fact_items):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('text'), str):
            continue
        sentence = item['text'].strip()
        if not sentence:
            continue
        score = sum(sentence.count(term) for term in terms)
        if score > 0:
            scored.append((-score, index, item['id'], sentence, item.get('authority', 'hard_constraint')))
    scored.sort(key=lambda item: (item[0], item[1]))
    chosen = []
    chosen_chars = 0
    for _, index, fact_id, sentence, authority in scored:
        if len(chosen) >= REPAIR_FACTS_MAX_ITEMS:
            break
        if chosen_chars + len(sentence) > REPAIR_FACTS_MAX_CHARS:
            continue
        chosen.append({'id': fact_id, 'text': sentence, 'authority': authority})
        chosen_chars += len(sentence)
    order = {item.get('id'): index for index, item in enumerate(fact_items) if isinstance(item, dict)}
    chosen.sort(key=lambda item: order.get(item['id'], len(order)))
    return chosen, {
        'selectedFactIds': [item['id'] for item in chosen],
        'selectedChars': chosen_chars,
        'omittedFactCount': max(0, len(fact_items) - len(chosen)),
        'maxItems': REPAIR_FACTS_MAX_ITEMS,
        'maxChars': REPAIR_FACTS_MAX_CHARS,
        'source': fact_source,
    }


def repair_scene_context(context, selected, problem, *, result_contract=None, fixed_facts=None):
    """Build a labelled repair context without concatenating unrelated layers."""
    current = api_routes.scene(context['package'], context['parent']['branchState'])
    scene_text = api_routes.turn_material(context['package'], context['parent']['branchState'], selected)
    context_data = {
        'currentScene': scene_text,
        'playerAction': player_action(context, selected),
    }
    if current:
        context_data['currentSceneId'] = current[0]
    issue_types = set(repair_issue_types(problem))
    if issue_types & {'action', 'state'} and result_contract is not None:
        context_data['resultContract'] = result_contract
    if fixed_facts:
        context_data['fixedFacts'] = fixed_facts
    return context_data


def record_repair_failure(record, error, stage, outcome):
    problem = error.repair_problem() if isinstance(error, SceneReviewError) else str(error)
    record.update(outcome=outcome, failureStage=stage,
                  failureCode=error.code if isinstance(error, LlmError) else 'validation_error',
                  failureReason=str(error), failureIssues=problem,
                  failureIssueTypes=repair_issue_types(problem))


def repair_projection_audit(bundle):
    """Return bounded metadata for the projection used by a repair request."""
    if bundle is None:
        return None
    projection = bundle.project('repair')
    audit = {
        'stage': projection['stage'],
        'contextId': projection['contextId'],
        'contextSha256': projection['contextSha256'],
        'allowedEvidenceSourceIds': [
            item['sourceId'] for item in projection.get('allowedEvidence', [])
            if isinstance(item, dict) and isinstance(item.get('sourceId'), str)
        ],
        'continuityWindowCount': len(projection.get('continuityWindow', [])),
        'dynamicMemoryCount': len(projection.get('dynamicMemory', [])),
    }
    audit.update(projection_observability(
        projection,
        excluded_fields=('styleGuide', 'provenance', 'author_truth', 'character_known'),
        excluded_reasons={
            'styleGuide': 'repair_does_not_need_style',
            'provenance': 'repair_keeps_selected_evidence_only',
            'author_truth': 'visibility_boundary',
            'character_known': 'visibility_boundary',
        },
    ))
    return audit


def repair_context_injection(bundle, problem, *, has_result_contract=False):
    """Select only the context layer required by the reported repair issue."""
    if bundle is None:
        return None
    projection = bundle.project('repair')
    issue_types = repair_issue_types(problem)
    selected = {
        'stage': 'repair',
        'contextSha256': projection['contextSha256'],
        'issueTypes': issue_types,
        'selectedLayers': [],
    }
    if 'continuity' in issue_types:
        selected['continuityWindow'] = projection.get('continuityWindow', [])
        selected['selectedLayers'].append('continuityWindow')
    if 'state' in issue_types and not has_result_contract:
        selected['authoritativeState'] = projection.get('authoritativeState', {})
        selected['selectedLayers'].append('authoritativeState')
    if 'action' in issue_types and not has_result_contract:
        action_contract = projection.get('hardConstraints', {}).get('actionContract')
        if isinstance(action_contract, dict) and action_contract:
            selected['actionContract'] = action_contract
            selected['selectedLayers'].append('actionContract')
    return selected


def chapter_continuity_text(bundle):
    """Render only the bounded, source-addressed chapter continuity window."""
    if bundle is None:
        return '', {'source': 'legacy', 'count': 0, 'sourceIds': [], 'reason': 'bundle_unavailable'}
    projection = bundle.project('chapter')
    items = projection.get('continuityWindow', [])
    if not isinstance(items, list):
        raise ContextBundleError('chapter.continuityWindow 必须是列表。')
    lines = []
    source_ids = []
    for item in items:
        if not isinstance(item, dict):
            raise ContextBundleError('chapter.continuityWindow 项必须是对象。')
        source_id = item.get('sourceId')
        content = item.get('content')
        if not isinstance(source_id, str) or not source_id.strip() or not isinstance(content, str) or not content.strip():
            raise ContextBundleError('chapter.continuityWindow 缺少有效来源或内容。')
        source_ids.append(source_id)
        lines.append(f'[{source_id}] {content.strip()}')
    if not lines:
        return '', {'source': 'context_bundle', 'count': 0, 'sourceIds': [], 'reason': 'bundle_window_empty'}
    return '\n'.join(lines), {
        'source': 'context_bundle',
        'count': len(lines),
        'sourceIds': source_ids,
    }


def _chapter_fact_audit(source, facts, chosen, selected_chars, reason=None):
    return {
        'source': source,
        'selectedFactIds': [fact_id for _, _, fact_id, _ in chosen],
        'selectedChars': selected_chars,
        'omittedFactCount': max(0, len(facts) - len(chosen)),
        'maxItems': CHAPTER_FACTS_MAX_ITEMS,
        'maxChars': CHAPTER_FACTS_MAX_CHARS,
        'selectedFacts': [
            {
                'id': fact_id,
                'sourceChapterId': item.get('sourceChapterId'),
                'sourceProgress': item.get('sourceProgress'),
                'lineRange': item.get('lineRange'),
                'selectionBasis': basis,
            }
            for item, _, fact_id, basis in chosen
        ],
        **({'reason': reason} if reason else {}),
    }


def _select_chapter_facts(facts, terms, *, explicit_ids=None):
    """Select only lexical or explicitly addressed facts under hard bounds."""
    explicit_ids = explicit_ids or set()
    scored = []
    for index, item in enumerate(facts):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('text'), str):
            continue
        text = item['text'].strip()
        if not text:
            continue
        overlap = sum(text.count(term) for term in terms)
        fact_id = item['id']
        if overlap <= 0 and fact_id not in explicit_ids:
            continue
        basis = 'explicit_source_ref' if fact_id in explicit_ids and overlap <= 0 else 'term_overlap'
        scored.append((-max(overlap, 1), index, fact_id, text, item, basis))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    chosen = []
    chosen_chars = 0
    for _, index, fact_id, text, item, basis in scored:
        if len(chosen) >= CHAPTER_FACTS_MAX_ITEMS:
            break
        if chosen_chars + len(text) > CHAPTER_FACTS_MAX_CHARS:
            continue
        chosen.append((item, index, fact_id, basis))
        chosen_chars += len(text)
    chosen.sort(key=lambda item: item[1])
    return chosen, chosen_chars


def chapter_hard_facts_text(bundle, *, fallback_items=None, fallback_query=''):
    """Select a bounded, chapter-relevant subset of immutable facts."""
    fallback_items = fallback_items if isinstance(fallback_items, list) else []
    if bundle is None:
        if fallback_items:
            terms = _repair_evidence_terms({'issues': [{'claim': fallback_query}]})
            chosen, selected_chars = _select_chapter_facts(fallback_items, terms)
            if chosen:
                return '\n'.join(f'[{fact_id}] {text}' for _, _, fact_id, _ in chosen), _chapter_fact_audit(
                    'legacy_fact_sheet', fallback_items, chosen, selected_chars,
                )
        return '', {
            'source': 'legacy', 'selectedFactIds': [], 'selectedChars': 0,
            'omittedFactCount': len(fallback_items), 'maxItems': CHAPTER_FACTS_MAX_ITEMS,
            'maxChars': CHAPTER_FACTS_MAX_CHARS, 'selectedFacts': [],
            'reason': 'bundle_unavailable',
        }
    projection = bundle.project('chapter')
    hard_constraints = projection.get('hardConstraints', {})
    facts = hard_constraints.get('immutableFacts', []) if isinstance(hard_constraints, dict) else []
    if not isinstance(facts, list) or not facts:
        if fallback_items:
            fallback_terms = _repair_evidence_terms({'issues': [{'claim': fallback_query}]})
            chosen, selected_chars = _select_chapter_facts(fallback_items, fallback_terms)
            if chosen:
                return '\n'.join(f'[{fact_id}] {item_text}' for item, _, fact_id, _ in chosen
                                 for item_text in [item['text'].strip()]), _chapter_fact_audit(
                    'legacy_fact_sheet', fallback_items, chosen, selected_chars,
                    reason='bundle_facts_empty',
                )
        return '', {
            'source': 'legacy', 'selectedFactIds': [], 'selectedChars': 0,
            'omittedFactCount': len(fallback_items), 'maxItems': CHAPTER_FACTS_MAX_ITEMS,
            'maxChars': CHAPTER_FACTS_MAX_CHARS, 'selectedFacts': [], 'reason': 'bundle_facts_empty',
        }
    intent = projection.get('turnIntent', {})
    beat = hard_constraints.get('currentBeat', {}) if isinstance(hard_constraints, dict) else {}
    chapter = hard_constraints.get('currentChapter', {}) if isinstance(hard_constraints, dict) else {}
    selected_direction = intent.get('selectedDirection', {}) if isinstance(intent, dict) else {}
    terms_text = ' '.join(str(value) for value in (
        intent.get('rawInput', '') if isinstance(intent, dict) else '',
        selected_direction.get('title', '') if isinstance(selected_direction, dict) else '',
        selected_direction.get('summary', '') if isinstance(selected_direction, dict) else '',
        beat.get('summary', '') if isinstance(beat, dict) else '',
        chapter.get('title', '') if isinstance(chapter, dict) else '',
    ))
    terms = _repair_evidence_terms({'issues': [{'claim': terms_text}]})
    explicit_ids = set()
    context_refs = beat.get('contextRefs', {}) if isinstance(beat, dict) else {}
    if isinstance(context_refs, dict):
        for key in ('factIds', 'immutableFactIds', 'sourceFactIds'):
            values = context_refs.get(key, [])
            if isinstance(values, list):
                explicit_ids.update(value for value in values if isinstance(value, str))
    chosen, selected_chars = _select_chapter_facts(
        facts, terms, explicit_ids=explicit_ids,
    )
    if chosen:
        return '\n'.join(f'[{fact_id}] {item_text}' for item, _, fact_id, _ in chosen
                         for item_text in [item['text'].strip()]), _chapter_fact_audit(
            'context_bundle', facts, chosen, selected_chars,
        )
    if fallback_items:
        fallback_terms = _repair_evidence_terms({'issues': [{'claim': fallback_query or terms_text}]})
        fallback_chosen, fallback_chars = _select_chapter_facts(fallback_items, fallback_terms)
        if fallback_chosen:
            return '\n'.join(f'[{fact_id}] {item_text}' for item, _, fact_id, _ in fallback_chosen
                             for item_text in [item['text'].strip()]), _chapter_fact_audit(
                'legacy_fact_sheet', fallback_items, fallback_chosen, fallback_chars,
                reason='bundle_no_relevant_facts',
            )
    return '', _chapter_fact_audit('legacy', facts, [], 0, reason='no_relevant_facts')


def repair_rejection_record(body, error, revision, context_projection=None):
    """Return a separate audit record for a repaired body rejected on recheck.

    The original repair record keeps the first problem and the later failure
    details. This follow-up record makes the rejected repaired candidate
    independently inspectable without pretending it was ever committed.
    """
    problem = error.repair_problem() if isinstance(error, SceneReviewError) else str(error)
    record = {
        'generationStage': 'local_repair_record',
        'revision': revision,
        'beforeBody': body,
        'afterBody': None,
        'issues': problem,
        'issueTypes': repair_issue_types(problem),
        'repairResponse': None,
        'outcome': 'rejected',
        'failureReason': str(error),
        'failureStage': 'second_review',
        'failureCode': error.code if isinstance(error, LlmError) else 'validation_error',
        'failureIssues': problem,
        'failureIssueTypes': repair_issue_types(problem),
    }
    if context_projection is not None:
        record['contextProjection'] = context_projection
    return record


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

    def _parallel_review_completions(self, jobs):
        """Overlap independent JSON reviews without changing fake-gateway tests.

        The production gateway opens an independent HTTP connection per request.
        Test doubles often consume a shared ordered response queue, so they stay
        sequential to keep deterministic fixtures and call-count assertions.
        """
        if (
            len(jobs) < 2
            or not isinstance(self.gateway, OpenAICompatibleGateway)
            or os.environ.get('STORY_LLM_PARALLEL_REVIEWS', 'true').strip().lower() in {'0', 'false', 'no', 'off'}
        ):
            return [job() for job in jobs]
        with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix='story-review') as pool:
            futures = [pool.submit(job) for job in jobs]
            return [future.result() for future in futures]

    def _public_review_evidence(self, context, *, stage='grounding_review'):
        """Read public source IDs only from the requested frozen stage."""
        if self.last_context_bundle is None:
            return public_scene_evidence(context)
        evidence = grounding_input_evidence({
            'contextProjection': self.last_context_bundle.project('grounding_review'),
        })
        if stage != 'grounding_review':
            projection = self.last_context_bundle.project(stage)
            allowed = {item['sourceId'] for item in projection['allowedEvidence']}
            # Repair selects the needed memory facts into sceneEvidence; its
            # compact context injection does not repeat dynamicMemory text.
            allowed.update(source for memory in projection.get('dynamicMemory', [])
                           for source in memory.get('sourceIds', []))
            evidence = {key: value for key, value in evidence.items() if key in allowed}
        return evidence

    def _check_action_authority(self, context, action, contract, raw, observations):
        evidence = self._public_review_evidence(context)
        messages = [
            {'role': 'system', 'content': reader_actions.AUTHORITY_RULES},
            {'role': 'user', 'content': json.dumps({
                'input': action, 'previous': context['parent']['narrativeText'],
                'playerId': context['contract']['persona'].get('sourceCharacterId'),
                'priorState': consequences.prompt_state(context['parent']['branchState']),
                'steps': contract['steps'], 'stateChanges': contract['stateChanges'],
                'scenePlan': contract.get('scenePlan'),
                'premises': plan_premises(contract.get('scenePlan') or {}),
                'sceneEvidence': evidence,
            }, ensure_ascii=False)},
        ]
        for attempt in range(2):
            completion = complete_with_retry(self.gateway, 'complete_json', messages, stage='action_authority')
            raw.append(completion.raw_response)
            observations.extend({**o, 'generationStage': 'action_authority', 'evidenceAttempt': attempt} for o in completion.observations)
            try:
                review = reader_actions.validate_authority(parse_json_content(completion.content), contract, action, context['parent']['narrativeText'])
                validate_plan_premises(review, contract, evidence)
                return review
            except reader_actions.ActionEvidenceError as error:
                if attempt:
                    # Do not regenerate an unchanged plan because its reviewer
                    # cannot quote the input. Fail within this bounded stage.
                    raise LlmError(str(error), 'model_output_rejected') from error
                messages += [{'role': 'assistant', 'content': completion.content},
                             {'role': 'user', 'content': render_prompt('reader.authority_repair', error=str(error))}]

    def _result_contract_context(self, context, selected, requirements):
        """Return the bounded result-contract projection and its audit."""
        bundle = self.last_context_bundle
        legacy = consequences.planning_context(context)
        if bundle is None:
            return {
                **legacy,
                'input': player_action(context, selected),
                'requirements': requirements,
                'knowledge': scene_knowledge(context),
            }, {'status': 'legacy', 'source': 'planning_context'}
        projection = bundle.project('result_contract')
        projection['planningState'] = {
            key: legacy[key]
            for key in ('goals', 'threads', 'closure', 'closing')
            if key in legacy
        }
        projected_evidence = {
            item['sourceId']: item['content']
            for item in projection.get('allowedEvidence', [])
            if isinstance(item, dict)
            and isinstance(item.get('sourceId'), str)
            and isinstance(item.get('content'), str)
        }
        projected_knowledge = scene_knowledge(context, evidence=projected_evidence)
        projected_knowledge['sourceKinds'] = {
            item['sourceId']: item.get('kind', 'public_fact')
            for item in projection.get('allowedEvidence', [])
            if isinstance(item, dict) and isinstance(item.get('sourceId'), str)
        }
        payload = {
            'contextProjection': projection,
            'input': player_action(context, selected),
            'requirements': requirements,
            'knowledge': projected_knowledge,
        }
        # Keep the small set of legacy planner keys that the consequence
        # contract reads directly. Their values are sourced from the same
        # bounded planningState, so callers do not rebuild context layers.
        for key in ('goals', 'threads', 'closure', 'closing'):
            if key in projection['planningState']:
                payload[key] = projection['planningState'][key]
        audit = {
            'status': 'recorded',
            'source': 'context_bundle',
            'stage': 'result_contract',
            'contextId': projection['contextId'],
            'contextSha256': projection['contextSha256'],
            'allowedEvidenceCount': len(projection.get('allowedEvidence', [])),
            'planningStateFields': sorted(projection['planningState']),
        }
        audit.update(projection_observability(
            projection,
            excluded_fields=('styleGuide', 'continuityWindow', 'provenance', 'dynamicMemory'),
            excluded_reasons={
                'styleGuide': 'result_contract_does_not_write_prose',
                'continuityWindow': 'result_contract_uses_current_intent_only',
                'provenance': 'internal_source_trace_not_needed_for_planning',
                'dynamicMemory': 'only_confirmed_state_contract is exposed',
            },
        ))
        return payload, audit

    def _prompt(self, context, selected, state, repair, terminal_source_branch=False):
        self._preflight_context_projection(context, selected, state)
        module_context = self.writing_scope
        self.last_prompt_context.update(mode='player_history', perspective='second_person_limited',
                                        terminalSourceBranch=bool(terminal_source_branch))
        if module_context is not None:
            self.last_prompt_context['currentChapterId'] = module_context.get('currentChapter', {}).get('id')
            self.last_prompt_context['currentBeatId'] = module_context.get('currentBeat', {}).get('id')
            self.last_prompt_context['modulePaths'] = module_context.get('modulePaths', [])
            self._record_chapter_projection_compatibility(
                self.last_context_bundle.project('chapter') if self.last_context_bundle else None,
                [
                    'pacing_json', 'perspective_rule', 'title', 'summary', 'history', 'changes_json',
                    'narrative_state_guardrails', 'player_location_id', 'final_people_json',
                    'scene_boundary', 'place_names_json', 'character_names', 'location_names',
                    'event_brief', 'ending_instruction', 'repair_instruction',
                ],
                True,
                {
                    'title': ('turnIntent.selectedDirection.title',),
                    'summary': ('turnIntent.selectedDirection.summary',),
                    'history': ('continuityWindow',),
                    'changes_json': ('authoritativeState',),
                    'narrative_state_guardrails': ('hardConstraints.globalConstraints',),
                    'player_location_id': ('authoritativeState.playerLocationId',),
                    'final_people_json': ('authoritativeState.characterLocationIds', 'authoritativeState.characterOutcomeStates'),
                    'scene_boundary': ('hardConstraints.actionContract',),
                    'place_names_json': ('hardConstraints.entityContext.locations',),
                    'character_names': ('hardConstraints.entityContext.characters',),
                    'location_names': ('hardConstraints.entityContext.locations',),
                    'event_brief': ('allowedEvidence', 'continuityWindow'),
                },
            )
        else:
            self.last_prompt_context['projectionCompatibility'] = {'status': 'unavailable'}
        persona = context['contract']['persona']
        parent = context['parent']['branchState']
        changes = {k: {'from': parent.get(k), 'to': v} for k, v in state.items()
                   if parent.get(k) != v and k not in ('branchLedger', 'freeTextProgress')}
        history = reading_history(context['lineage']) if self.last_context_bundle is None else ''
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
        player_location_id = state.get('playerLocationId', state.get('currentLocationId'))
        state_guardrails_text = narrative_state_guardrails(context['package'], state)
        chapter_history, chapter_history_audit = chapter_continuity_text(self.last_context_bundle)
        self.last_prompt_context['chapterContinuity'] = chapter_history_audit
        if chapter_history:
            history = chapter_history + '\n只承接以上带来源的已确认摘要；历史原始正文不进入本回合写作上下文。'
        elif current:
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
        legacy_fact_items = api_routes.fact_sheet_items(
            context['package'], parent, bool(selected.get('readerInterlude')),
        )
        legacy_fact_query = ' '.join(str(value) for value in (
            player_action(context, selected), selected.get('title', ''), selected.get('summary', ''),
        ))
        chapter_facts, chapter_facts_audit = chapter_hard_facts_text(
            self.last_context_bundle,
            fallback_items=legacy_fact_items,
            fallback_query=legacy_fact_query,
        )
        self.last_prompt_context['chapterHardFacts'] = chapter_facts_audit
        if current:
            if chapter_facts:
                label = ('当前 bundle 硬事实' if chapter_facts_audit.get('source') == 'context_bundle'
                         else '兼容性硬事实')
                event_brief += f'\n{label}（只使用带 ID 的筛选项）：' + chapter_facts
        chapter_projection = self.last_context_bundle.project('chapter') if self.last_context_bundle else None
        if chapter_projection is not None:
            hard = chapter_projection.get('hardConstraints', {})
            entities = hard.get('entityContext', {}) if isinstance(hard.get('entityContext'), dict) else {}
            visible_state = chapter_projection.get('authoritativeState', {})
            visible_character_locations = visible_state.get('characterLocationIds', {})
            projected_characters = entities.get('characters', []) if isinstance(entities.get('characters'), list) else []
            projected_locations = entities.get('locations', []) if isinstance(entities.get('locations'), list) else []
            projected_items = entities.get('items', []) if isinstance(entities.get('items'), list) else []
            names = [item.get('name') for item in projected_characters if isinstance(item, dict) and item.get('name')]
            places = [item.get('name') for item in projected_locations if isinstance(item, dict) and item.get('name')]
            place_names = {item.get('id'): item.get('name') for item in projected_locations
                           if isinstance(item, dict) and item.get('id') and item.get('name')}
            final_people = {
                item['name']: place_names.get(visible_character_locations.get(item.get('id')))
                for item in projected_characters
                if isinstance(item, dict) and item.get('id') in visible_character_locations
            }
            changes = {key: value for key, value in visible_state.items()
                       if parent.get(key) != value}
            player_location_id = visible_state.get('playerLocationId', '')
            state_guardrails_text = json.dumps(hard.get('globalConstraints', []), ensure_ascii=False)
            scene_boundary = json.dumps({
                'actionContract': hard.get('actionContract', {}),
                'branchRules': hard.get('branchRules', {}),
                'stopPoint': hard.get('stopPoint'),
            }, ensure_ascii=False)
            history = chapter_history or json.dumps({
                'continuityWindow': chapter_projection.get('continuityWindow', []),
            }, ensure_ascii=False)
            if chapter_projection.get('dynamicMemory'):
                history += '\n已确认动态记忆：' + json.dumps(
                    chapter_projection['dynamicMemory'], ensure_ascii=False,
                )
            event_brief = json.dumps({
                'turnIntent': chapter_projection.get('turnIntent', {}),
                'hardConstraints': {
                    'currentChapter': hard.get('currentChapter', {}),
                    'currentBeat': hard.get('currentBeat', {}),
                    'openingKnowledgeBoundaries': hard.get('openingKnowledgeBoundaries', []),
                    'pendingFollowups': hard.get('pendingFollowups', []),
                    'resolvedFollowups': hard.get('resolvedFollowups', []),
                    'entityFacts': hard.get('entityFacts', {}),
                },
                'allowedEvidence': chapter_projection.get('allowedEvidence', []),
                'outputContract': chapter_projection.get('outputContract', {}),
                **({'continuityText': chapter_history} if context.get('narrativePolicy') != 'context_only' else {}),
                'hardFacts': chapter_facts,
                'items': projected_items,
                'currentItemStates': [dict(id=item['id'], name=item.get('name'),
                    ownerCharacterId=parent.get('itemOwnerCharacterIds', {}).get(item['id'], 'unknown')
                        if item['id'] in visible_state.get('itemOwnerCharacterIds', {}) else 'unknown',
                    locationId=parent.get('itemLocationIds', {}).get(item['id'], 'unknown')
                        if item['id'] in visible_state.get('itemLocationIds', {}) else 'unknown')
                    for item in projected_items if isinstance(item, dict) and isinstance(item.get('id'), str)],
                'resultContract': context.get('resultContract'),
            }, ensure_ascii=False)
            chapter_audit = {
                'status': 'recorded',
                'stage': 'chapter',
                'contextId': chapter_projection['contextId'],
                'contextSha256': chapter_projection['contextSha256'],
                'allowedEvidenceCount': len(chapter_projection.get('allowedEvidence', [])),
                'continuityWindowCount': len(chapter_projection.get('continuityWindow', [])),
                'dynamicMemoryCount': len(chapter_projection.get('dynamicMemory', [])),
                'visibleStateFields': sorted(visible_state),
                'excludedFields': ['provenance', 'author_truth', 'character_known', 'future-beats', 'sibling-branches'],
            }
            chapter_audit.update(projection_observability(
                chapter_projection,
                excluded_fields=('provenance', 'author_truth', 'character_known', 'future-beats', 'sibling-branches'),
                excluded_reasons={
                    'provenance': 'writer_does_not_need_internal_source_trace',
                    'author_truth': 'visibility_boundary',
                    'character_known': 'visibility_boundary',
                    'future-beats': 'branch_boundary',
                    'sibling-branches': 'branch_boundary',
                },
            ))
            self.last_prompt_context['chapterProjection'] = chapter_audit
        if selected.get('readerInterlude'):
            place = place_names.get(parent.get('playerLocationId'), '')
            interlude_history = history
            return render_prompt('legacy_rainy.interlude',
                perspective_rule=f"{perspective_rule(persona['name'])}",
                latest=f'{interlude_history}',
                reading_history=f"{interlude_history}",
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
        return render_prompt(getattr(self, 'narrative_prompt', 'reader.narrative'),
            pacing_json=json.dumps(pacing, ensure_ascii=False),
            perspective_rule=f"{perspective_rule(persona['name'])}",
            title=player_action(context, selected),
            summary=f"{selected['summary']}",
            history=f'{history}',
            changes_json=f'{json.dumps(changes, ensure_ascii=False)}',
            narrative_state_guardrails=f'{state_guardrails_text}',
            player_location_id=f'{player_location_id}',
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
        review_attempts = 0
        repair_attempted = False
        failure_stage = None
        candidate_history = []
        try:
            self._preflight_context_projection(context, selected, resolved_state)
            result_contract = None
            consequence_update = None
            if consequences.enabled(context['package']):
                requirements = action_requirements(player_action(context, selected))
                result_contract_payload, result_contract_audit = self._result_contract_context(
                    context, selected, requirements,
                )
                self.last_prompt_context['resultContractProjection'] = result_contract_audit
                plan_messages = [
                    {'role': 'system', 'content': consequences.PLAN_RULES},
                    {'role': 'user', 'content': json.dumps(result_contract_payload, ensure_ascii=False)},
                ]
                for contract_attempt in range(2):
                    planning = complete_with_retry(self.gateway, 'complete_json', plan_messages, stage='result_contract')
                    raw.append(planning.raw_response)
                    observations.extend({**o, 'generationStage': 'result_contract', 'revision': contract_attempt} for o in planning.observations)
                    try:
                        result_contract = consequences.validate_plan(parse_json_content(planning.content), requirements, context)
                        if result_contract['decision'] == 'ready':
                            validate_scene_plan(
                                result_contract,
                                result_contract_payload['knowledge']['publicEvidence'],
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
                                              error=str(error) + '\n原始要求ID必须严格保留为：' + '、'.join(requirements) + '；删除所有多余ID，不得新增A8或其他要求。',
                                          )}]
                if result_contract['decision'] != 'ready':
                    raise LlmError(result_contract['message'], 'action_' + result_contract['decision'])
                context = {**context, 'resultContract': result_contract}
                resolved_state = consequences.projected_state(resolved_state, result_contract, context['package'])
            failure = ''
            repaired_body = None
            candidate_revisions = set()
            # One initial candidate plus one bounded repair/rewrite. Candidate
            # bodies remain private until the caller applies its explicit
            # post-review selection policy.
            for attempt in range(2):
                review_attempts = max(review_attempts, attempt + 1)
                if attempt:
                    repair_attempted = True
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
                        ending_messages = [
                            {'role': 'system', 'content': render_prompt('legacy_rainy.ending_review')},
                            {'role': 'user', 'content': json.dumps({'player': name, 'locations': [p['name'] for p in context['package']['locations']], 'draft': body}, ensure_ascii=False)},
                        ]
                        review_fact_items = api_routes.fact_sheet_items(
                            context['package'], context['parent']['branchState'], interlude,
                        )
                        review_fact_query = ' '.join(str(value) for value in (
                            player_action(context, selected), selected.get('title', ''), selected.get('summary', ''),
                        ))
                        review_facts, review_facts_audit = chapter_hard_facts_text(
                            self.last_context_bundle,
                            fallback_items=review_fact_items,
                            fallback_query=review_fact_query,
                        )
                        self.last_prompt_context['chapterHardFactsReview'] = review_facts_audit
                        fact_messages = [
                            {'role': 'system', 'content': render_prompt('legacy_rainy.fact_review')},
                            {'role': 'user', 'content': json.dumps({'facts': '正文中的你指' + name + '。' + review_facts
                                + '\n此前已完成的事件：' + '；'.join(e['summary'] for e in context['parent']['branchState'].get('derivedEvents', []) if e['id'].startswith(api_routes.PREFIX)),
                                'scene': api_routes.turn_material(context['package'], context['parent']['branchState'], selected),
                                'review_boundary': render_prompt('legacy_rainy.review_boundary'),
                                'player_action': player_action(context, selected), 'previous': reading_history(context['lineage']),
                                'previous_actions': reading_history(context['lineage']), 'draft': body}, ensure_ascii=False)},
                        ]
                        ending_review, review = self._parallel_review_completions([
                            lambda: complete_with_retry(self.gateway, 'complete_json', ending_messages),
                            lambda: complete_with_retry(self.gateway, 'complete_json', fact_messages),
                        ])
                        raw.append(ending_review.raw_response)
                        observations.extend({**o, 'generationStage': 'ending_review', 'revision': attempt} for o in ending_review.observations)
                        raw.append(review.raw_response)
                        observations.extend({**item, 'generationStage': 'scene_review', 'revision': attempt} for item in review.observations)
                        try:
                            ending = parse_json_content(ending_review.content)
                        except LlmError as error:
                            raise ValueError('章末核对没有返回有效 JSON') from error
                        api_routes.check_reviewed_ending(context['package'], context['parent']['branchState'], body, ending, interlude)
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
                            observation_payload = {
                                'viewpoint': {'id': context['contract']['persona'].get('sourceCharacterId'), 'name': name},
                                'registry': {cid: {key: entity[key] for key in ('id', 'name', 'kind', 'aliases') if key in entity}
                                    for cid, entity in reader_actions.registry(context['package'], context['parent']['branchState'], result_contract['introductions']).items()},
                                'attributeVocabulary': reader_actions.attribute_vocabulary(
                                    context['package'], context['parent']['branchState'], result_contract,
                                ),
                                'draft': {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))},
                            }
                            if self.last_context_bundle is not None:
                                fact_projection = self.last_context_bundle.project('fact_extract')
                                observation_payload['contextProjection'] = fact_projection
                                self.last_prompt_context['factExtractProjection'] = {
                                    'status': 'recorded',
                                    'stage': 'fact_extract',
                                    'contextId': fact_projection['contextId'],
                                    'contextSha256': fact_projection['contextSha256'],
                                    'allowedEvidenceCount': len(fact_projection.get('allowedEvidence', [])),
                                    'provenanceFields': sorted(fact_projection.get('provenance', {})),
                                }
                                self.last_prompt_context['factExtractProjection'].update(projection_observability(
                                    fact_projection,
                                    excluded_fields=('styleGuide', 'continuityWindow', 'dynamicMemory'),
                                    excluded_reasons={
                                        'styleGuide': 'fact_extract_reads_prose_and_state',
                                        'continuityWindow': 'fact_extract_uses_draft_as_source',
                                        'dynamicMemory': 'candidate extraction cannot promote memory',
                                    },
                                ))
                            else:
                                observation_payload.update({
                                    'state': consequences.prompt_state(context['parent']['branchState']),
                                    'previous': reading_history(context['lineage']),
                                })
                            observation_messages = [
                                {'role': 'system', 'content': reader_actions.OBSERVE_RULES + render_prompt('reader.scene_observe')},
                                {'role': 'user', 'content': json.dumps(observation_payload, ensure_ascii=False)},
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
                        review_evidence = self._public_review_evidence(context)
                        review_history, history_audit = action_review_history(
                            context, player_action(context, selected), self.last_context_bundle,
                            review_evidence,
                        )
                        self.last_prompt_context['actionReviewHistory'] = history_audit
                        review_messages = [
                            {'role': 'system', 'content': render_prompt('reader.action_review') + (consequences.REVIEW_RULES + render_prompt('reader.scene_review') if result_contract else '')},
                            {'role': 'user', 'content': json.dumps({'player': name, 'requirements': requirements,
                                'priorRepairIssues': pending_repair_issues,
                                'input': player_action(context, selected), 'resultContract': result_contract, 'observedEvents': events,
                                'sceneEvidence': review_evidence,
                                'knowledge': scene_knowledge(context, include_evidence=False,
                                                             evidence=review_evidence),
                                'continuity': {k: v for k, v in consequences.planning_context(context).items()
                                               if k not in ('state', 'history', 'goals')},
                                'authoritativeState': consequences.prompt_state(context['parent']['branchState']),
                                'goals': consequences.goals_for(context['package'], context['contract'], context['parent']['branchState']),
                                'previous': review_history, 'draft': {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}}, ensure_ascii=False)},
                        ]
                        review_jobs = [('action_review', lambda: complete_with_retry(
                            self.gateway, 'complete_json', review_messages, stage='action_review'))]
                        grounding_messages = None
                        focus = None
                        scope_messages = None
                        if result_contract and body not in grounding_cache:
                            grounding_payload = {
                                'paragraphs': grounding_claims(body),
                                'priorRepairIssues': pending_repair_issues,
                                'repairTargets': repair_targets(pending_repair_issues),
                                'dialogueUnits': dialogue_units(body),
                                'playerId': context['contract']['persona'].get('sourceCharacterId'),
                                'draft': {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))},
                                'input': player_action(context, selected), 'requirements': requirements,
                                'boundaries': scene_boundaries(result_contract.get('scenePlan')),
                            }
                            if self.last_context_bundle is not None:
                                grounding_projection = self.last_context_bundle.project('grounding_review')
                                grounding_payload['contextProjection'] = grounding_projection
                                self.last_prompt_context['groundingReviewProjection'] = {
                                    'status': 'recorded',
                                    'stage': 'grounding_review',
                                    'contextId': grounding_projection['contextId'],
                                    'contextSha256': grounding_projection['contextSha256'],
                                    'allowedEvidenceCount': len(grounding_projection.get('allowedEvidence', [])),
                                    'dynamicMemoryCount': len(grounding_projection.get('dynamicMemory', [])),
                                }
                                self.last_prompt_context['groundingReviewProjection'].update(projection_observability(
                                    grounding_projection,
                                    excluded_fields=('styleGuide', 'continuityWindow'),
                                    excluded_reasons={
                                        'styleGuide': 'grounding_review_checks claims only',
                                        'continuityWindow': 'grounding_review_uses_explicit_evidence',
                                    },
                                ))
                            else:
                                grounding_payload.update({
                                    'sceneEvidence': public_scene_evidence(context),
                                    'knowledge': scene_knowledge(context, include_evidence=False),
                                })
                            grounding_payload['people'] = scene_speaker_candidates(
                                context, body, grounding_input_evidence(grounding_payload), result_contract['introductions'])
                            grounding_messages = [
                                {'role': 'system', 'content': render_prompt('reader.scene_grounding')},
                                {'role': 'user', 'content': json.dumps(grounding_payload, ensure_ascii=False)},
                            ]
                            review_jobs.append(('scene_grounding', lambda messages=grounding_messages: complete_with_retry(
                                self.gateway, 'complete_json', messages, stage='scene_grounding')))
                        if result_contract:
                            focus = exclusive_requirements(requirements)
                            if focus and body not in scope_cache:
                                scope_messages = [
                                    {'role': 'system', 'content': render_prompt('reader.scope_review')},
                                    {'role': 'user', 'content': json.dumps({'input': player_action(context, selected),
                                        'requirements': focus,
                                        'draft': {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}}, ensure_ascii=False)},
                                ]
                                review_jobs.append(('scope_review', lambda messages=scope_messages: complete_with_retry(
                                    self.gateway, 'complete_json', messages)))
                        completed_reviews = self._parallel_review_completions([job for _, job in review_jobs])
                        completions = dict(zip((name_ for name_, _ in review_jobs), completed_reviews))
                        action_review = completions['action_review']
                        raw.append(action_review.raw_response)
                        observations.extend({**o, 'generationStage': 'action_review', 'revision': attempt} for o in action_review.observations)
                        review_data = parse_json_content(action_review.content)
                        if result_contract:
                            # Coverage is unconditional: first-pass labels and
                            # linguistic heuristics cannot suppress this check.
                            # Cache only byte-identical prose within this turn.
                            if grounding_messages is not None:
                                grounding = completions['scene_grounding']
                                grounding_inputs[body] = grounding_messages
                                raw.append(grounding.raw_response)
                                observations.extend({**o, 'generationStage': 'scene_grounding', 'revision': attempt} for o in grounding.observations)
                                grounding_cache[body] = parse_json_content(grounding.content)
                            else:
                                observations.append({'generationStage': 'scene_grounding', 'revision': attempt, 'outcome': 'reused_identical_body'})
                            observations.append({'generationStage': 'independent_review_result', 'revision': attempt,
                                                 'review': grounding_cache[body]})
                            if scope_messages is not None:
                                scoped = completions['scope_review']
                                raw.append(scoped.raw_response)
                                observations.extend({**o, 'generationStage': 'scope_review', 'revision': attempt} for o in scoped.observations)
                                scope_cache[body] = parse_json_content(scoped.content)
                                observations.append({'generationStage': 'scope_review_result', 'revision': attempt,
                                                     'review': scope_cache[body]})
                            # Reuse the evidence IDs actually sent for this body,
                            # including when its independent review is cached.
                            reviewed_input = json.loads(grounding_inputs[body][1]['content'])
                            grounding_evidence = grounding_input_evidence(reviewed_input)
                            for evidence_attempt in range(2):
                                try:
                                    combined_scene_issues(review_data, body, events, grounding_cache[body],
                                                         evidence=grounding_evidence, requirements=requirements,
                                                         focused=scope_cache.get(body),
                                                         boundaries=scene_boundaries(result_contract.get('scenePlan')),
                                                         people=reviewed_input['people'],
                                                         speaker_names=reviewed_input['people'],
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
                                scene_checks = validate_scene_review(review_data, body, self._public_review_evidence(context), events)
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
                                    scene_checks = validate_scene_review(review_data, body, self._public_review_evidence(context), events)
                                    reader_actions.validate_events(review_data, events, candidate_contract, context['parent']['branchState'], context['package'])
                                    consequences.validate_final_state(review_data, candidate_state)
                                    consequence_update = consequences.validate_review(review_data, candidate_contract, body, reader_outcome)
                                except (consequences.ConsequenceEvidenceError, reader_actions.ActionEvidenceError) as error:
                                    raise LlmError(str(error), 'model_output_rejected') from error

                            result_contract, resolved_state, authority_review = candidate_contract, candidate_state, candidate_authority

                except ValueError as error:
                    if repair_record is not None:
                        record_repair_failure(repair_record, error, 'full_review', 'failed_full_review')
                        if attempt == 1 and repair_record.get('afterBody') is not None:
                            observations.append(repair_rejection_record(
                                body, error, attempt, repair_record.get('contextProjection'),
                            ))
                        repair_record = None
                    observations.append({'generationStage': 'validation', 'revision': attempt,
                                         'outcome': 'failed', 'error': str(error),
                                         **({'repairIssues': error.repair_problem()} if isinstance(error, SceneReviewError) else {})})
                    if attempt not in candidate_revisions and isinstance(body, str) and body.strip():
                        problem_snapshot = error.repair_problem() if isinstance(error, SceneReviewError) else str(error)
                        candidate_history.append({
                            'revision': attempt,
                            'body': body,
                            'actualCjk': count,
                            'problem': problem_snapshot,
                            'issueTypes': repair_issue_types(problem_snapshot),
                            'failureStage': 'full_review',
                        })
                        candidate_revisions.add(attempt)
                    if attempt == 1:
                        rejected = LlmError(str(error), 'model_output_rejected')
                        rejected.review_attempts = review_attempts
                        rejected.repair_attempted = repair_attempted
                        rejected.failure_stage = 'second_review'
                        rejected.fallback_mode = 'preserve_previous_branch'
                        rejected.candidate_history = candidate_history
                        raise rejected from error
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
                        repair_context = repair_context_injection(
                            self.last_context_bundle,
                            problem,
                            has_result_contract=result_contract is not None,
                        )
                        repair_scene_evidence, repair_evidence_audit = select_repair_evidence(
                            context, problem, evidence=self._public_review_evidence(context, stage='repair'))
                        repair_fixed_facts, repair_facts_audit = select_repair_fixed_facts(
                            context, selected, problem, bundle=self.last_context_bundle,
                        )
                        repair_scene = repair_scene_context(
                            context, selected, problem,
                            result_contract=result_contract,
                            fixed_facts=repair_fixed_facts,
                        )
                        repair_projection_audit_record = repair_projection_audit(self.last_context_bundle)
                        repair_record = {'generationStage': 'local_repair_record', 'revision': attempt,
                                         'beforeBody': body, 'afterBody': None, 'issues': problem,
                                         'issueTypes': repair_issue_types(problem), 'repairResponse': None,
                                         'outcome': 'requested', 'failureReason': None,
                                         'failureStage': None, 'failureCode': None,
                                         'failureIssues': None, 'failureIssueTypes': [],
                                         'repairEvidenceSelection': repair_evidence_audit,
                                         'repairFactsSelection': repair_facts_audit}
                        if repair_projection_audit_record is not None:
                            repair_record['contextProjection'] = repair_projection_audit_record
                        observations.append(repair_record)
                        repair_attempted = True
                        failure_stage = 'repair_generation'
                        repair_payload = {'problem': {
                                **problem, 'issues': [{k: v for k, v in issue.items() if k != 'quote'} for issue in problem['issues']]
                            } if isinstance(problem, dict) else problem, 'scene': repair_scene, 'sceneEvidence': repair_scene_evidence,
                                'pacing': self.last_prompt_context.get('pacing', {}),
                                'dialogueDependencies': repair_dialogue_dependencies(body),
                                'originalParagraphCjk': {f'P{i+1}': len(re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]', p))
                                                         for i, p in enumerate(body.split('\n\n'))},
                                'paragraphs': repair_paragraphs(body, error)}
                        if repair_context is not None:
                            repair_payload['contextInjection'] = repair_context
                        fixed = complete_with_retry(self.gateway, 'complete_json', [
                            {'role': 'system', 'content': perspective_rule(name) + render_prompt('reader.narrative_repair')},
                            {'role': 'user', 'content': json.dumps(repair_payload, ensure_ascii=False)},
                        ], stage='local_repair')
                        raw.append(fixed.raw_response)
                        observations.extend({**o, 'generationStage': 'local_repair', 'revision': attempt} for o in fixed.observations)
                        repair_record['repairResponse'] = fixed.content
                        try:
                            repaired_body = apply_scene_repairs(body, parse_json_content(fixed.content).get('replacements'), getattr(error, 'violations', ()))
                            failure_stage = None
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
            if (isinstance(body, str) and body.strip() and review_attempts >= 2
                    and len(candidate_history) < review_attempts):
                problem_snapshot = str(error)
                candidate_history.append({
                    'revision': review_attempts - 1,
                    'body': body,
                    'actualCjk': cjk_character_count(body),
                    'problem': problem_snapshot,
                    'issueTypes': ['unknown'],
                    'failureStage': 'full_review',
                })
            if repair_record is not None:
                reviewing = repair_record['outcome'] == 'pending_full_review'
                repaired_body = repair_record.get('afterBody')
                record_repair_failure(repair_record, error,
                                      'full_review' if reviewing else 'repair_request',
                                      'failed_full_review' if reviewing else 'failed')
                if reviewing and error.code not in {'transport_error', 'timeout'}:
                    observations.append(repair_rejection_record(
                        repaired_body, error, repair_record.get('revision', 0) + 1,
                        repair_record.get('contextProjection'),
                    ))
            # A readable draft is not an accepted branch. Preserve the last
            # complete candidate across reset/transport failure, privately.
            error.review_attempts = getattr(error, 'review_attempts', review_attempts)
            error.repair_attempted = getattr(error, 'repair_attempted', repair_attempted)
            error.failure_stage = getattr(error, 'failure_stage', failure_stage or ('second_review' if repair_attempted else 'generation'))
            error.fallback_mode = getattr(error, 'fallback_mode', 'preserve_previous_branch')
            error.retained_body = retained_body
            error.candidate_history = candidate_history
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


class ContextNarrativePlanner(PlayerNarrativePlanner):
    """Current delivery: context-driven prose with structural state extraction.

    The inherited strict planner remains available to historical regression
    tests, but is not selected by the API runtime.
    """
    narrative_prompt = 'reader.context_narrative'
    system_instruction = render_prompt('reader.context_system')

    @staticmethod
    def prepare_direction(package, parent, selected, player_direction=None):
        if selected.get('isFreeText'):
            # Mentioning a destination or companion does not make the trip
            # happen. Only observed prose may change persistent world state.
            progress = parent['branchState'].get('freeTextProgress')
            return {**selected, 'statePatch': {'freeTextProgress': progress + 1}
                    if type(progress) is int else {}}
        return api_routes.prepare_direction(package, parent, selected, player_direction)

    def plan(self, context, selected, resolved_state, stream=None, stream_reset=None, **kwargs):
        from .context_turn import plan_turn
        return plan_turn(self, context, selected, resolved_state, stream, stream_reset)
