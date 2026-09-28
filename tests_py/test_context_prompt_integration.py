"""Audit wiring through real core prompts for every official longform entrance."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from test_support.longform import longform_cases
from open_story_engine.content import load_runtime_story_package
from open_story_engine import api_routes
from open_story_engine import reader_consequences as consequences
from open_story_engine.context_bundle import ContextBundleBuilder, ContextBundleError
from open_story_engine.cocreation import (
    LlmPlanner,
    apply_branch_patch,
    create_contract,
    entry_node,
    source_fact_protection_context,
)
from open_story_engine.api_narrative import (
    REPAIR_EVIDENCE_MAX_CHARS,
    REPAIR_EVIDENCE_MAX_ITEMS,
    PlayerNarrativePlanner,
    action_requirements,
    chapter_continuity_text,
    chapter_hard_facts_text,
    player_action,
    repair_context_injection,
    repair_projection_audit,
    repair_scene_context,
    select_repair_evidence,
    select_repair_fixed_facts,
)
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.prompts import render_prompt


class ContextPromptIntegrationTests(unittest.TestCase):
    def cases(self):
        for case in longform_cases():
            package = load_runtime_story_package(case['path'], lazy=True)
            for character_id in package['story']['entryModel']['sourceCharacterIds']:
                contract = create_contract(package, 'context-audit-test', {
                    'kind': 'source_character', 'sourceCharacterId': character_id,
                })
                root = entry_node(package, contract)
                selected = {
                    'title': '原地等候', 'summary': '留在当前位置，等待对方回应',
                    'statePatch': {'playerLocationId': root['branchState']['playerLocationId']},
                }
                state = apply_branch_patch(
                    package, root['branchState'], selected['statePatch'], root['sourceNodeRef'],
                )
                projected, _ = ContextBundleBuilder.project_selected_state_patch(selected)
                context = {
                    'package': package, 'contract': contract, 'parent': root, 'lineage': [root],
                    'playerDirection': '我暂时不移动，听听回应。', 'characterDetails': [],
                    'validatedStatePatch': projected['statePatch'],
                    'validatedStatePatchSource': 'apply_branch_patch',
                }
                resolver = ModuleContextResolver.for_package(case['path'], package)
                self.assertIsNotNone(resolver)
                yield case, character_id, context, selected, state, resolver

    def test_module_prompt_records_mode_mapping_without_changing_template_values(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                planner = LlmPlanner(object(), context_resolver=resolver)
                before = copy.deepcopy((context['parent'], selected, state))
                with patch('open_story_engine.cocreation.render_prompt', wraps=render_prompt) as render:
                    prompt = planner._prompt(context, selected, state, None)
                self.assertEqual(render.call_args.args, ('core.module_narrative',))
                values = render.call_args.kwargs
                self.assertEqual(values['title'], selected['title'])
                self.assertEqual(values['summary'], selected['summary'])
                self.assertEqual(json.loads(values['state_json']), planner.writing_scope['characterIdentityEvidence'])
                self.assertEqual(prompt, render_prompt('core.module_narrative', **values))
                self.assertEqual(before, (context['parent'], selected, state))
                audit = planner.last_prompt_context
                self.assertEqual(audit['contextBundle']['status'], 'recorded')
                report = audit['projectionCompatibility']
                self.assertEqual(report['projectionContextSha256'], audit['contextBundle']['contextSha256'])
                mappings = {item['legacyField']: item for item in report['fieldMappings']}
                self.assertEqual(mappings['state_json']['availableProjectionPaths'], ['allowedEvidence'])
                for field in ('title', 'summary'):
                    self.assertEqual(
                        mappings[field]['availableProjectionPaths'],
                        [f'turnIntent.selectedDirection.{field}'],
                    )
                    self.assertEqual(mappings[field]['status'], 'all_declared_paths_present')
                    self.assertNotIn(field, report['legacyOnlyFields'])
                self.assertEqual(report['comparisonLevel'], 'schema_path_presence_only')
                self.assertNotIn(context['playerDirection'], json.dumps(report, ensure_ascii=False))
                self.assertTrue(all(not path.startswith('reader/') for path in audit['modulePaths']))

    def test_package_prompt_reports_unavailable_and_clears_previous_module_bundle(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                planner = LlmPlanner(object(), context_resolver=resolver)
                planner._prompt(context, selected, state, None)
                self.assertIsNotNone(planner.last_context_bundle)
                planner.context_resolver = None
                with patch('open_story_engine.cocreation.render_prompt', wraps=render_prompt) as render:
                    planner._prompt(context, selected, state, None)
                self.assertEqual(render.call_args.args, ('core.narrative',))
                self.assertEqual(json.loads(render.call_args.kwargs['state_json']), {
                    key: value for key, value in state.items() if key != 'branchLedger'
                })
                self.assertIsNone(planner.last_context_bundle)
                self.assertEqual(planner.last_prompt_context['contextBundle']['status'], 'skipped')
                report = planner.last_prompt_context['projectionCompatibility']
                self.assertEqual(report['status'], 'unavailable')
                self.assertNotIn('projectionContextSha256', report)
                self.assertNotIn('fieldMappings', report)

    def test_module_prompt_links_bundle_to_parent_context_id(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                context = copy.deepcopy(context)
                context['parent']['contextId'] = 'ctx-parent-from-branch'
                planner = LlmPlanner(object(), context_resolver=resolver)
                planner._prompt(context, selected, state, None)
                bundle = planner.last_prompt_context['contextBundle']['bundle']
                self.assertEqual(bundle['parentContextId'], 'ctx-parent-from-branch')

    def test_result_contract_uses_bounded_projection_before_model_call(self):
        case, character_id, context, selected, state, resolver = next(self.cases())
        planner = PlayerNarrativePlanner(object(), context_resolver=resolver)
        planner.last_prompt_context = {}
        planner._preflight_context_projection(context, selected, state)
        requirements = action_requirements(player_action(context, selected))
        payload, audit = planner._result_contract_context(context, selected, requirements)
        self.assertEqual(audit['source'], 'context_bundle')
        self.assertEqual(audit['stage'], 'result_contract')
        self.assertEqual(payload['contextProjection']['stage'], 'result_contract')
        self.assertEqual(payload['contextProjection']['contextSha256'], audit['contextSha256'])
        self.assertGreater(audit['serializedChars'], 0)
        self.assertGreater(audit['estimatedTokens'], 0)
        self.assertIn('excludedReasons', audit)
        self.assertNotIn('continuityWindow', payload['contextProjection'])
        self.assertNotIn('styleGuide', payload['contextProjection'])
        self.assertNotIn('criticalHistory', payload['contextProjection']['planningState'])
        self.assertNotIn('history-', json.dumps(payload['knowledge'], ensure_ascii=False))
        self.assertNotIn(context['parent']['narrativeText'], json.dumps(payload, ensure_ascii=False))
        self.assertEqual(sorted(payload['contextProjection']['planningState']), audit['planningStateFields'])

    def test_core_prompt_fact_injection_is_bounded_and_audited(self):
        facts = [
            {'id': f'fact-{index}', 'text': '木牌刻痕与山门记录相关。' * 20}
            for index in range(20)
        ]
        text, audit = LlmPlanner._bounded_prompt_fact_text(facts, '确认木牌记录')
        self.assertLessEqual(len(audit['selectedFactIds']), 10)
        self.assertLessEqual(audit['selectedChars'], 3200)
        self.assertEqual(len(audit['selectedFactIds']), 10)
        self.assertEqual(text.count('\n'), 9)
        self.assertEqual(len(text), sum(len(facts[index]['text']) + 2 for index in range(10)) + 9)

    def test_stage_evidence_keeps_shared_sources_without_restoring_opening_excerpts(self):
        _, _, context, selected, state, resolver = next(self.cases())
        planner = PlayerNarrativePlanner(object(), context_resolver=resolver)
        planner.last_prompt_context = {}
        planner._preflight_context_projection(context, selected, state)
        payload, _ = planner._result_contract_context(context, selected, {})
        evidence = planner._public_review_evidence(context)
        planning_evidence = payload['knowledge']['publicEvidence']
        repair_evidence = planner._public_review_evidence(context, stage='repair')
        self.assertTrue(planning_evidence)
        self.assertEqual(planning_evidence, repair_evidence)
        self.assertTrue(all(evidence[key] == value for key, value in planning_evidence.items()))
        source_id = next(key for key in evidence if ':opening:evidence:' in key)
        self.assertNotIn(source_id, planning_evidence)
        chosen, audit = select_repair_evidence(context, {
            'issues': [{'type': 'background', 'sourceIds': [source_id]}],
        }, evidence=repair_evidence)
        self.assertEqual(chosen, {})
        self.assertEqual(audit['selectedSourceIds'], [])
        current_id = next(key for key in planning_evidence if ':opening:visibleItems:' in key)
        chosen, audit = select_repair_evidence(context, {
            'issues': [{'type': 'background', 'sourceIds': [current_id]}],
        }, evidence=repair_evidence)
        self.assertEqual(chosen, {current_id: evidence[current_id]})
        self.assertEqual(audit['selectedSourceIds'], [current_id])
        self.assertNotIn('history-', json.dumps(evidence))
        self.assertNotIn(context['parent']['narrativeText'], json.dumps(evidence, ensure_ascii=False))

    def test_empty_selected_repair_evidence_cannot_restore_legacy_facts(self):
        context = {'contract': {'openingContext': {'knownFacts': ['木牌刻痕']}}, 'lineage': []}
        chosen, audit = select_repair_evidence(context, {
            'issues': [{'type': 'background', 'claim': '木牌刻痕'}],
        }, evidence={})
        self.assertEqual(chosen, {})
        self.assertEqual(audit['selectedSourceIds'], [])

    def test_continuation_fact_injection_is_bounded_and_guardrail_is_not_duplicated(self):
        case, character_id, context, selected, state, resolver = next(self.cases())
        context = copy.deepcopy(context)
        context['package']['world']['immutableFacts'] = [
            {'id': f'fact-{index}', 'text': '木牌记录与山门登记相关。' * 20}
            for index in range(20)
        ]
        selected = {**selected, 'title': '确认木牌记录', 'summary': '确认木牌记录'}
        planner = LlmPlanner(object())
        with patch('open_story_engine.cocreation.render_prompt', wraps=render_prompt) as render:
            prompt = planner._continuation_prompt(context, selected, state, '前文只写到木牌旁。')
        values = render.call_args.kwargs
        scope = json.loads(values['scope_text'])
        self.assertLessEqual(len(scope['immutableFacts']), 10)
        self.assertLessEqual(sum(len(item['text']) for item in scope['immutableFacts']), 3200)
        self.assertLessEqual(planner.last_prompt_context['continuationFacts']['selectedChars'], 3200)
        scope_audit = planner.last_prompt_context['continuationFacts']['scopeProjection']
        self.assertLessEqual(scope_audit['selectedChars'], 6400)
        self.assertEqual(prompt.count(values['state_guardrail_text']), 1)

    def test_non_fact_scope_projection_is_bounded_under_long_source_material(self):
        scope = {
            'narrativeBrief': [
                {'evidenceParagraphId': f'p-{index}', 'text': '场景材料' * 500}
                for index in range(20)
            ],
            'priorNarrativeBrief': [
                {'evidenceParagraphId': f'prior-{index}', 'text': '前史材料' * 500}
                for index in range(20)
            ],
            'actionContract': {'instruction': '行动契约' * 2000, 'steps': ['步骤' * 300 for _ in range(20)]},
            'characters': [{'id': f'c-{index}', 'name': f'角色{index}'} for index in range(20)],
            'locations': [{'id': f'l-{index}', 'name': f'地点{index}'} for index in range(20)],
            'items': [{'id': f'i-{index}', 'name': f'物件{index}'} for index in range(20)],
            'characterDetails': [{'name': f'角色{index}', 'detail': '角色细节' * 500} for index in range(20)],
            'characterIdentityEvidence': [{'name': f'角色{index}', 'text': '身份证据' * 500} for index in range(20)],
            'sourceDialogueContext': [{'speakerName': f'角色{index}', 'paragraphs': [
                {'text': '对白证据' * 500} for _ in range(10)
            ]} for index in range(20)],
            'continuityText': '连续性摘要' * 1000,
        }
        projection, audit = LlmPlanner._bounded_scope_projection(scope)
        encoded = json.dumps(projection, ensure_ascii=False)
        self.assertLessEqual(len(encoded), 6400)
        self.assertEqual(audit['selectedChars'], len(encoded))
        self.assertGreater(audit['omittedItems'] + len(audit['omittedFields']), 0)
        self.assertTrue(all(len(items) <= 8 for key, items in projection.items() if isinstance(items, list)))
        self.assertLessEqual(len(projection.get('continuityText', '')), 1200)

    def test_fact_review_evidence_is_bounded_and_audited(self):
        case, character_id, context, selected, state, resolver = next(self.cases())
        facts = [
            {'id': f'fact-{index}', 'text': '木牌记录与山门登记相关。' * 20}
            for index in range(20)
        ]
        planner = LlmPlanner(object())
        planner.writing_scope = {'world': {'immutableFacts': facts}}
        evidence = planner._fact_evidence(context, selected, state, query='木牌记录')
        known = [item for item in evidence if item['kind'] == 'knownFacts']
        self.assertLessEqual(len(known), 10)
        self.assertLessEqual(sum(len(item['value'].get('text', '')) for item in known), 3200)
        self.assertLessEqual(planner.last_prompt_context['factEvidence']['selectedChars'], 3200)

    def test_fact_review_projects_non_fact_scope_with_same_budget(self):
        case, character_id, context, selected, state, resolver = next(self.cases())
        planner = LlmPlanner(object())
        planner.writing_scope = {
            'world': {'immutableFacts': []},
            'narrativeBrief': [{'text': '场景材料' * 1000} for _ in range(20)],
            'characterDetails': [{'name': '林遥', 'detail': '角色细节' * 1000} for _ in range(20)],
            'sourceDialogueContext': [{'speakerName': '林遥', 'paragraphs': [{'text': '对白' * 1000}]} for _ in range(20)],
        }
        planner._fact_evidence(context, selected, state, query='角色细节')
        audit = planner.last_prompt_context['factEvidence']['scopeProjection']
        self.assertLessEqual(audit['selectedChars'], 6400)
        self.assertGreater(audit['omittedItems'] + len(audit['omittedFields']), 0)

    def test_source_fact_protection_is_bounded(self):
        package = {
            'characters': [{'name': '林遥'}],
            'world': {'immutableFacts': [
                {'text': '林遥的电话已经无法接通。', 'sourceProgress': 'chapter_001'}
                for _ in range(20)
            ] + [
                {'text': '林遥后来在北门的电话再次无法接通。', 'sourceProgress': 'chapter_002'},
            ]},
        }
        text = source_fact_protection_context(package, {'sourceProgress': 'chapter_001'})
        self.assertLessEqual(text.count('林遥的电话已经无法接通。'), 10)
        self.assertIn('另有 10 条同类来源事实已省略', text)
        self.assertNotIn('后来在北门的电话再次无法接通', text)

    def test_rejected_audit_preserves_prompt_and_does_not_reuse_previous_bundle(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                planner = LlmPlanner(object(), context_resolver=resolver)
                expected_prompt = planner._prompt(context, selected, state, None)
                self.assertIsNotNone(planner.last_context_bundle)
                rejected_context = dict(context)
                rejected_context.pop('validatedStatePatchSource')
                self.assertEqual(planner._prompt(rejected_context, selected, state, None), expected_prompt)
                self.assertIsNone(planner.last_context_bundle)
                self.assertEqual(planner.last_prompt_context['contextBundle']['status'], 'rejected')
                self.assertIn('状态层验证标记', planner.last_prompt_context['contextBundle']['error'])
                self.assertEqual(planner.last_prompt_context['projectionCompatibility']['status'], 'unavailable')

    def test_player_narrative_prompt_records_audit_without_changing_reader_prompt(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                planner = PlayerNarrativePlanner(object(), context_resolver=resolver)
                prompt = planner._prompt(context, selected, state, None)
                self.assertIn('contextBundle', planner.last_prompt_context)
                self.assertEqual(planner.last_prompt_context['contextBundle']['status'], 'recorded')
                self.assertEqual(
                    planner.last_prompt_context['projectionCompatibility']['comparisonLevel'],
                    'schema_path_presence_only',
                )
                report = {
                    item['legacyField']: item
                    for item in planner.last_prompt_context['projectionCompatibility']['fieldMappings']
                }
                self.assertEqual(report['event_brief']['availableProjectionPaths'], ['allowedEvidence', 'continuityWindow'])
                self.assertEqual(report['title']['status'], 'all_declared_paths_present')
                self.assertEqual(report['summary']['status'], 'all_declared_paths_present')
                self.assertIn('停在需要你作出下一步决定的位置', prompt)

    def test_chapter_continuity_uses_bounded_source_addressed_projection(self):
        class Bundle:
            def project(self, stage):
                if stage != 'chapter':
                    raise AssertionError(stage)
                return {'continuityWindow': [
                    {'sourceId': 'branch:lineage:n1', 'content': '已确认停在门边。'},
                    {'sourceId': 'module:previousBeat:b2', 'content': '上一拍只听见门内回应。'},
                ]}

        text, audit = chapter_continuity_text(Bundle())
        self.assertEqual(
            text,
            '[branch:lineage:n1] 已确认停在门边。\n[module:previousBeat:b2] 上一拍只听见门内回应。',
        )
        self.assertEqual(audit, {
            'source': 'context_bundle',
            'count': 2,
            'sourceIds': ['branch:lineage:n1', 'module:previousBeat:b2'],
        })

    def test_empty_chapter_projection_is_distinct_from_missing_bundle(self):
        class Bundle:
            def project(self, stage):
                return {'continuityWindow': []}

        text, audit = chapter_continuity_text(Bundle())
        self.assertEqual(text, '')
        self.assertEqual(audit['source'], 'context_bundle')
        self.assertEqual(audit['reason'], 'bundle_window_empty')
        _, missing = chapter_continuity_text(None)
        self.assertEqual(missing['source'], 'legacy')
        self.assertEqual(missing['reason'], 'bundle_unavailable')

    def test_invalid_chapter_window_cannot_silently_become_empty_or_legacy(self):
        class Bundle:
            def __init__(self, items):
                self.items = items

            def project(self, stage):
                return {'continuityWindow': self.items}

        for items in ({}, [None], [{'sourceId': 'x'}], [{'sourceId': '', 'content': '缺来源'}]):
            with self.subTest(items=items), self.assertRaises(ContextBundleError):
                chapter_continuity_text(Bundle(items))

    def test_chapter_hard_facts_are_bounded_and_current_chapter_linked(self):
        class Bundle:
            def project(self, stage):
                return {
                    'turnIntent': {
                        'rawInput': '确认木牌',
                        'selectedDirection': {'title': '查看木牌', 'summary': '确认木牌刻痕'},
                    },
                    'hardConstraints': {
                        'currentChapter': {'id': 'chapter-1', 'title': '山门'},
                        'currentBeat': {'summary': '木牌停在门边'},
                        'immutableFacts': [
                            {'id': 'old', 'text': '木牌曾在旧屋。', 'sourceChapterId': 'chapter-0'},
                            {'id': 'current', 'text': '木牌刻痕来自当前山门。', 'sourceChapterId': 'chapter-1'},
                            {'id': 'same-chapter-unrelated', 'text': '远处竹影摇动。', 'sourceChapterId': 'chapter-1'},
                            {'id': 'unrelated', 'text': '无关事实。', 'sourceChapterId': 'chapter-9'},
                        ],
                    },
                }

        text, audit = chapter_hard_facts_text(Bundle())
        self.assertIn('[current] 木牌刻痕来自当前山门。', text)
        self.assertNotIn('[same-chapter-unrelated]', text)
        self.assertNotIn('[unrelated]', text)
        self.assertEqual(audit['source'], 'context_bundle')
        self.assertEqual(audit['selectedFactIds'], ['old', 'current'])
        self.assertEqual(audit['selectedFacts'][0]['selectionBasis'], 'term_overlap')

    def test_chapter_hard_facts_fallback_is_bounded_and_addressable(self):
        class Bundle:
            def project(self, stage):
                return {'hardConstraints': {'immutableFacts': []}}

        items = [
            {'id': 'legacy-wood', 'text': '木牌刻痕来自旧门。', 'authority': 'hard_constraint'},
            {'id': 'legacy-unrelated', 'text': '无关事实。', 'authority': 'hard_constraint'},
        ]
        text, audit = chapter_hard_facts_text(
            Bundle(), fallback_items=items, fallback_query='确认木牌刻痕',
        )
        self.assertEqual(text, '[legacy-wood] 木牌刻痕来自旧门。')
        self.assertEqual(audit['source'], 'legacy_fact_sheet')
        self.assertEqual(audit['selectedFactIds'], ['legacy-wood'])
        self.assertEqual(audit['selectedFacts'][0]['selectionBasis'], 'term_overlap')

    def test_generic_terms_do_not_select_unrelated_hard_fact(self):
        class Bundle:
            def project(self, stage):
                return {
                    'turnIntent': {
                        'rawInput': '确认当前人物位置',
                        'selectedDirection': {'title': '确认位置', 'summary': '确认当前场景中的人物位置'},
                    },
                    'hardConstraints': {
                        'currentChapter': {'id': 'chapter-1', 'title': '山门'},
                        'currentBeat': {'summary': '确认当前人物位置'},
                        'immutableFacts': [
                            {'id': 'generic', 'text': '人物位置已经登记。', 'sourceChapterId': 'chapter-1'},
                            {'id': 'specific', 'text': '木牌仍在门边。', 'sourceChapterId': 'chapter-1'},
                        ],
                    },
                }

        text, audit = chapter_hard_facts_text(Bundle())
        self.assertNotIn('[generic]', text)
        self.assertEqual(audit['selectedFactIds'], [])

    def test_explicit_current_beat_fact_reference_survives_without_term_overlap(self):
        class Bundle:
            def project(self, stage):
                return {
                    'turnIntent': {'rawInput': '等待', 'selectedDirection': {'title': '等待', 'summary': '保持原地'}},
                    'hardConstraints': {
                        'currentChapter': {'id': 'chapter-1', 'title': '山门'},
                        'currentBeat': {'summary': '等待', 'contextRefs': {'factIds': ['fact-explicit']}},
                        'immutableFacts': [
                            {'id': 'fact-explicit', 'text': '此前已经登记过木牌。', 'sourceChapterId': 'chapter-1'},
                            {'id': 'fact-unrelated', 'text': '远处竹影摇动。', 'sourceChapterId': 'chapter-1'},
                        ],
                    },
                }

        text, audit = chapter_hard_facts_text(Bundle())
        self.assertIn('[fact-explicit] 此前已经登记过木牌。', text)
        self.assertNotIn('[fact-unrelated]', text)
        selected = next(item for item in audit['selectedFacts'] if item['id'] == 'fact-explicit')
        self.assertEqual(selected['selectionBasis'], 'explicit_source_ref')

    def test_official_taixu_non_root_facts_reach_context_bundle(self):
        case = next(case for case in longform_cases() if case['package_id'] == 'taixu-relics-part1')
        package = load_runtime_story_package(case['path'], lazy=True)
        entry = list(package['story']['entryModel']['entryPoints'])[0]
        contract = create_contract(package, 'context-audit-official', {
            'entryPointId': entry['id'], 'kind': 'source_character',
            'sourceCharacterId': entry['sourceCharacterIds'][0],
        })
        target = next(
            beat for beat in package['story']['narrativeGraph']['beats']
            if beat['branchState'].get('sourceProgress') == 'chapter_052'
        )
        state = target['branchState']
        selected = {
            'title': '确认灯芯记录', 'summary': '确认灯芯与木牌留下的记录', 'statePatch': {},
        }
        parent = {
            'kind': 'branch', 'branchState': state, 'sourceNodeRef': target['nodeId'],
            'summary': target['summary'],
        }
        context = {
            'package': package, 'contract': contract, 'parent': parent, 'lineage': [parent],
            'playerDirection': selected['summary'],
        }
        resolver = ModuleContextResolver.for_package(Path(case['path']), package)
        module_context = resolver.resolve(context, selected, state)
        self.assertTrue(module_context['world']['immutableFacts'])
        self.assertTrue(all('lineRange' in fact for fact in module_context['world']['immutableFacts']))
        self.assertTrue(module_context['currentBeat']['contextRefs']['factIds'])
        current_progress_fact_ids = {
            fact['id'] for fact in module_context['world']['immutableFacts']
            if fact.get('sourceProgress') == 'chapter_052'
        }
        self.assertTrue(
            current_progress_fact_ids & set(module_context['currentBeat']['contextRefs']['factIds'])
        )
        bundle = ContextBundleBuilder(resolver=resolver).build(
            context=context, selected=selected, state=state, branch={'branchId': 'official-audit'},
            state_visibility=ContextBundleBuilder.default_state_visibility(state),
            module_context=module_context,
        )
        chapter_facts = bundle.project('chapter')['hardConstraints']['immutableFacts']
        self.assertTrue(chapter_facts)
        self.assertTrue(any(item.get('sourceChapterId') for item in chapter_facts))
        chapter_text, chapter_audit = chapter_hard_facts_text(bundle)
        self.assertIn(module_context['currentBeat']['contextRefs']['factIds'][0], chapter_text)
        self.assertTrue(any(item['selectionBasis'] == 'explicit_source_ref' for item in chapter_audit['selectedFacts']))

    def test_player_prompt_prefers_bundle_hard_facts_over_legacy_sheet(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                planner = PlayerNarrativePlanner(object(), context_resolver=resolver)
                with patch(
                    'open_story_engine.api_narrative.chapter_hard_facts_text',
                    return_value=('[bundle-fact] 只允许使用这条硬事实。', {
                        'source': 'context_bundle', 'selectedFactIds': ['bundle-fact'],
                        'selectedChars': 13, 'omittedFactCount': 0,
                        'maxItems': 10, 'maxChars': 3200,
                    }),
                ), patch.object(api_routes, 'scene', return_value=('scene-id', '场景', '当前场景', '当前材料')), \
                        patch.object(api_routes, 'fact_sheet', return_value='legacy 不应进入正文 prompt'):
                    prompt = planner._prompt(context, selected, state, None)
                self.assertIn('[bundle-fact] 只允许使用这条硬事实。', prompt)
                self.assertNotIn('legacy 不应进入正文 prompt', prompt)
                self.assertEqual(planner.last_prompt_context['chapterHardFacts']['source'], 'context_bundle')

    def test_player_prompt_consumes_projected_history_for_regular_and_interlude(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                planner = PlayerNarrativePlanner(object(), context_resolver=resolver)
                with patch(
                    'open_story_engine.api_narrative.chapter_continuity_text',
                    return_value=('[bundle-source] 已确认的连续性摘要。', {
                        'source': 'context_bundle', 'count': 1, 'sourceIds': ['bundle-source'],
                    }),
                ):
                    regular = planner._prompt(context, selected, state, None)
                self.assertIn('[bundle-source] 已确认的连续性摘要。', regular)
                self.assertEqual(planner.last_prompt_context['chapterContinuity']['source'], 'context_bundle')
                self.assertEqual(planner.last_prompt_context['chapterProjection']['stage'], 'chapter')
                self.assertEqual(
                    planner.last_prompt_context['chapterProjection']['contextSha256'],
                    planner.last_context_bundle.context_sha256,
                )
                self.assertGreater(planner.last_prompt_context['chapterProjection']['serializedChars'], 0)
                self.assertGreater(planner.last_prompt_context['chapterProjection']['estimatedTokens'], 0)

                interlude_selected = {**selected, 'readerInterlude': True}
                with patch(
                    'open_story_engine.api_narrative.chapter_continuity_text',
                    return_value=('[bundle-source] interlude 连续性摘要。', {
                        'source': 'context_bundle', 'count': 1, 'sourceIds': ['bundle-source'],
                    }),
                ):
                    interlude = planner._prompt(context, interlude_selected, state, None)
                self.assertIn('[bundle-source] interlude 连续性摘要。', interlude)

    def test_empty_bundle_window_never_reads_legacy_history_in_writer_or_interlude(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            for interlude in (False, True):
                with self.subTest(book=case['package_id'], character=character_id, interlude=interlude):
                    planner = PlayerNarrativePlanner(object(), context_resolver=resolver)
                    with patch.object(ContextBundleBuilder, '_continuity_window', return_value=[]), patch(
                        'open_story_engine.api_narrative.reading_history',
                        side_effect=AssertionError('合法空窗口不得调用旧摘要'),
                    ):
                        prompt = planner._prompt(context, {**selected, 'readerInterlude': interlude}, state, None)
                    self.assertIn('"continuityWindow": []', prompt)
                    self.assertEqual(planner.last_prompt_context['chapterContinuity']['source'], 'context_bundle')
                    self.assertEqual(planner.last_context_bundle.project('chapter')['continuityWindow'], [])

    def test_missing_bundle_writer_compatibility_reads_history_once(self):
        _, _, context, selected, state, _ = next(self.cases())
        for interlude in (False, True):
            with self.subTest(interlude=interlude):
                planner = PlayerNarrativePlanner(object())
                with patch('open_story_engine.api_narrative.reading_history', return_value='兼容历史标记') as history:
                    prompt = planner._prompt(context, {**selected, 'readerInterlude': interlude}, state, None)
                history.assert_called_once_with(context['lineage'])
                self.assertIn('兼容历史标记', prompt)
                self.assertEqual(planner.last_prompt_context['chapterContinuity']['reason'], 'bundle_unavailable')

    def test_repair_projection_audit_is_hash_bound_and_bounded(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                planner = PlayerNarrativePlanner(object(), context_resolver=resolver)
                planner._prompt(context, selected, state, None)
                projection = planner.last_context_bundle.project('repair')
                audit = repair_projection_audit(planner.last_context_bundle)
                self.assertEqual(audit['stage'], 'repair')
                self.assertEqual(audit['contextId'], projection['contextId'])
                self.assertEqual(audit['contextSha256'], projection['contextSha256'])
                self.assertEqual(
                    audit['allowedEvidenceSourceIds'],
                    [item['sourceId'] for item in projection['allowedEvidence']],
                )
                self.assertEqual(audit['continuityWindowCount'], len(projection['continuityWindow']))
                self.assertEqual(audit['dynamicMemoryCount'], len(projection['dynamicMemory']))
                self.assertNotIn('provenance', audit)
                self.assertNotIn('content', json.dumps(audit, ensure_ascii=False))

    def test_repair_context_injection_selects_only_reported_issue_layers(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                planner = PlayerNarrativePlanner(object(), context_resolver=resolver)
                planner._prompt(context, selected, state, None)
                bundle = planner.last_context_bundle
                continuity = repair_context_injection(bundle, {'issues': [{'type': 'continuity'}]})
                self.assertEqual(continuity['selectedLayers'], ['continuityWindow'])
                self.assertIn('continuityWindow', continuity)
                self.assertNotIn('authoritativeState', continuity)
                self.assertNotIn('actionContract', continuity)
                state_only = repair_context_injection(bundle, {'issues': [{'type': 'state'}]})
                self.assertEqual(state_only['selectedLayers'], ['authoritativeState'])
                self.assertNotIn('package', state_only)
                self.assertNotIn('branch', state_only)
                self.assertNotIn('allowedEvidence', state_only)
                contract_state = repair_context_injection(
                    bundle, {'issues': [{'type': 'state'}]}, has_result_contract=True,
                )
                self.assertEqual(contract_state['selectedLayers'], [])
                self.assertNotIn('authoritativeState', contract_state)

    def test_repair_evidence_is_issue_linked_and_bounded(self):
        context = {
            'contract': {'openingContext': {'knownFacts': ['木牌刻痕来自旧门，只有这条资料可公开引用。']}},
            'lineage': [
                {'id': f'n{i}', 'summary': ('木牌刻痕' if i == 0 else '无关的结构化历史摘要') + ('。' * 880),
                 'playerDirection': '继续核对。',
                 'readerOutcome': {'action': {'summary': '已完成上一回合。'}}}
                for i in range(3)
            ],
        }
        evidence, audit = select_repair_evidence(context, {
            'issues': [{'type': 'background', 'claim': '木牌刻痕', 'reason': '缺少依据'}],
        })
        self.assertLessEqual(len(evidence), REPAIR_EVIDENCE_MAX_ITEMS)
        self.assertLessEqual(sum(len(value) for value in evidence.values()), REPAIR_EVIDENCE_MAX_CHARS)
        self.assertEqual(audit['selectedSourceIds'], list(evidence))
        self.assertGreater(audit['omittedSourceCount'], 0)
        self.assertTrue(any('木牌刻痕' in value for value in evidence.values()))

    def test_repair_scene_context_keeps_layers_structured_and_issue_scoped(self):
        for case, character_id, context, selected, state, resolver in self.cases():
            with self.subTest(book=case['package_id'], character=character_id):
                contract = {'requirements': {'A1': {'summary': '完成当前行动'}}}
                background = repair_scene_context(
                    context, selected, {'issues': [{'type': 'background'}]},
                    result_contract=contract,
                )
                self.assertIn('currentScene', background)
                self.assertIn('playerAction', background)
                self.assertNotIn('resultContract', background)
                action = repair_scene_context(
                    context, selected, {'issues': [{'type': 'action'}]},
                    result_contract=contract,
                )
                self.assertEqual(action['resultContract'], contract)
                self.assertNotIn('authoritativeState', action)

    def test_repair_fixed_facts_are_sentence_bounded_and_issue_linked(self):
        context = {'package': {}, 'parent': {'branchState': {}}, 'contract': {}}
        selected = {'readerInterlude': False, 'title': '等待'}
        facts = [
            {'id': 'hard-fact-identity', 'text': '固定身份。', 'authority': 'hard_constraint'},
            {'id': 'hard-fact-wood', 'text': '木牌刻痕来自旧门。', 'authority': 'hard_constraint'},
        ] + [
            {'id': f'hard-fact-{i}', 'text': '无关事实。', 'authority': 'hard_constraint'}
            for i in range(900)
        ]
        with patch('open_story_engine.api_narrative.api_routes.fact_sheet_items', return_value=facts):
            selected_facts, audit = select_repair_fixed_facts(
                context, selected, {'issues': [{'type': 'background', 'claim': '木牌刻痕'}]},
            )
        self.assertLessEqual(sum(len(item['text']) for item in selected_facts), 3200)
        self.assertLessEqual(len(selected_facts), 10)
        self.assertIn('hard-fact-wood', audit['selectedFactIds'])
        self.assertNotIn('hard-fact-identity', audit['selectedFactIds'])
        self.assertGreater(audit['omittedFactCount'], 0)

    def test_fact_sheet_items_have_stable_ids(self):
        with patch.object(api_routes, 'fact_sheet', return_value='固定身份；木牌刻痕来自旧门；固定身份；'):
            first = api_routes.fact_sheet_items({}, {}, False)
            second = api_routes.fact_sheet_items({}, {}, False)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        self.assertTrue(all(item['id'].startswith('hard-fact-') for item in first))
        self.assertEqual(len({item['id'] for item in first}), 3)
        self.assertTrue(all(item['authority'] == 'hard_constraint' for item in first))
        self.assertEqual(first[0]['text'], '固定身份；')
        self.assertTrue(first[2]['id'].endswith('-2'))

    def test_repair_fixed_facts_prefers_bundle_hard_constraints(self):
        class Bundle:
            def project(self, stage):
                self.assert_stage = stage
                return {'hardConstraints': {'immutableFacts': [
                    {'id': 'bundle-fact-1', 'text': '木牌来自当前门边。', 'authority': 'hard_constraint'},
                ]}}

        context = {'package': {}, 'parent': {'branchState': {}}}
        selected, audit = select_repair_fixed_facts(
            context, {'readerInterlude': False},
            {'issues': [{'type': 'background', 'claim': '木牌'}]}, bundle=Bundle(),
        )
        self.assertEqual(audit['source'], 'context_bundle')
        self.assertEqual([item['id'] for item in selected], ['bundle-fact-1'])
