import copy
import unittest

from open_story_engine.reader_scene_review import public_scene_evidence, validate_scene_review, validate_grounding, grounding_claims, repair_paragraphs, SceneGroundingError, SceneReviewError, reject_review_issues
from open_story_engine.reader_actions import ActionEvidenceError
from open_story_engine.api_reader_quality import scene_pacing, expand_scene_paragraphs
from tests_api.test_reader_consequences import scene_checked


class SceneReviewTests(unittest.TestCase):
    def test_repair_must_keep_dialogue_references_connected(self):
        from open_story_engine.api_narrative import apply_scene_repairs, repair_dialogue_dependencies
        body = '他说：“后坡不小，你会分神。”\n\n提到“分神”时，他抬起眼。\n\n你站在原地等着。'
        self.assertEqual(repair_dialogue_dependencies(body), [dict(paragraphId='P2', quote='分神', sourceParagraphIds=['P1'])])
        for text in (body, body.replace('提到', '说到')):
            with self.assertRaisesRegex(ValueError, '删除了前文台词'):
                apply_scene_repairs(text, [dict(paragraphId='P1', text='他说：“我听见了。”')])
        result = apply_scene_repairs(body, [dict(paragraphId='P1', text='他说：“我听见了。”'),
                                           dict(paragraphId='P2', text='他说完后抬起眼。')])
        self.assertNotIn('分神', result)
        # Keeping a valid quoted phrase is enough; no forced neighbor edit.
        apply_scene_repairs(body, [dict(paragraphId='P1', text='他说：“你自己留意，别分神。”')])

    def test_dialogue_guard_does_not_treat_new_direct_speech_as_a_reference(self):
        from open_story_engine.api_narrative import repair_dialogue_dependencies
        self.assertEqual(repair_dialogue_dependencies('他说：“我不知道。”\n\n她说“我不知道”，没有犹豫。'), [])

    def test_repair_rejects_new_neighbor_echo_before_full_review(self):
        from open_story_engine.api_narrative import apply_scene_repairs
        body = '他说：“纸包着，我连它的大小、成色都看不准，只能听你说。”\n\n他猜测这是金器留下的。\n\n你仍站在原地，没有继续追问。'
        for text in ['纸包着，我连它的大小、成色都看不准，更没法说来路。',
                     '他摇了摇头。\n\n纸包着，我连它的大小、成色都看不准，更没法说来路。']:
            with self.assertRaisesRegex(ValueError, '新增.*重复'):
                apply_scene_repairs(body, [dict(paragraphId='P2', text=text)])
        fixed = apply_scene_repairs(body, [dict(paragraphId='P2', text='他把后半句话咽了回去，声音也低了下来。')])
        self.assertTrue(fixed.startswith(body.split('\n\n')[0]))
        self.assertTrue(fixed.endswith(body.split('\n\n')[-1]))

    def test_repair_does_not_reject_unchanged_repetition_or_short_shared_words(self):
        from open_story_engine.api_narrative import apply_scene_repairs
        repeated = '你把自己的问题清清楚楚地说了一遍，随后等候回应。'
        body = repeated + '\n\n他看着你，没有立即回答。\n\n' + repeated
        fixed = apply_scene_repairs(body, [dict(paragraphId='P2', text='他看着你，平静地说不知道。')])
        self.assertEqual(fixed.count(repeated), 2)

    def test_sentence_coverage_cannot_hide_history_behind_unknown_answer(self):
        body = '他说：“我不知道来处。我没去过后坡。”\n\n你没有追问。'
        claims = grounding_claims(body)
        self.assertEqual(''.join(c['claim'] for c in claims.values()), body.replace('\n\n', ''))
        self.assertEqual(set(claims), {'P1-C1', 'P1-C2', 'P2-C1'})
        data = {'checks': [dict(id='P1-C1', kind='unknown', verdict='supported', sources=[], reason='只承认不知道'),
                           dict(id='P2-C1', kind='current', verdict='supported', sources=[], reason='没有追问')]}
        with self.assertRaisesRegex(ValueError, '遗漏'):
            validate_grounding(data, claims, evidence={})
        data['checks'].append(dict(id='P1-C2', kind='background', verdict='supported', sources=[], reason='声称从未进后坡'))
        with self.assertRaises(SceneGroundingError) as error:
            validate_grounding(data, claims, evidence={})
        self.assertIn('没去过', error.exception.violations[0]['claim'])

    def test_background_sources_must_be_real_and_bound_to_public_evidence(self):
        claims = grounding_claims('他说日落前要回来。')
        ref = dict(id='opening-1', quote='日落前回来')
        data = {'checks': [dict(id='P1-C1', kind='background', verdict='supported',
                               sources=[ref], reason='复述公开期限') ]}
        validate_grounding(data, claims, evidence={'opening-1': '日落前回来，不得越界。'})
        for bad in [dict(id='draft-P1', quote='日落前回来'), dict(id='opening-1', quote='钟响开始计时')]:
            data['checks'][0]['sources'] = [bad]
            with self.assertRaisesRegex(ValueError, '公开资料'):
                validate_grounding(data, claims, evidence={'opening-1': '日落前回来，不得越界。'})

    def test_bad_source_reference_cannot_mask_another_semantic_rejection(self):
        from tests_api.test_reader_consequences import grounded
        body = '他平静地问你。\n\n他从未进过后坡。'
        data = grounded(body)
        data['checks'][0].update(kind='inference', sources=[dict(id='P1-C1', quote='他平静地问你')])
        data['checks'][1].update(kind='background', verdict='unsupported', reason='否定经历没有依据')
        with self.assertRaises(SceneGroundingError) as error:
            validate_grounding(data, grounding_claims(body), evidence={})
        self.assertEqual(error.exception.violations[0]['paragraphId'], 'P2')

    def test_scope_violation_survives_other_review_approval_and_merges_background(self):
        from open_story_engine.reader_scene_review import combined_scene_issues, validate_scope
        from tests_api.test_reader_consequences import grounded
        body = '他看着木牌刻痕。\n\n他说这里的草很深。'
        req = {'A1': '让他只看纸包', 'A2': '暂不交出物品'}
        data = grounded(body, req)
        data['scopeChecks'][0].update(verdict='violated', paragraphIds=['P1'], reason='看了被排除的木牌')
        data['checks'][1].update(verdict='unsupported', kind='background', reason='草深无依据')
        with self.assertRaises(SceneReviewError) as error:
            combined_scene_issues({'issues': []}, body, [], data, evidence={}, requirements=req)
        self.assertEqual({v['type'] for v in error.exception.violations}, {'action', 'background'})
        self.assertEqual({v['paragraphId'] for v in error.exception.violations}, {'P1', 'P2'})
        data['scopeChecks'] = data['scopeChecks'][1:]
        with self.assertRaisesRegex(ValueError, '遗漏原始要求'):
            validate_scope(data, req, body)

    def test_sentence_scope_violation_cannot_be_overridden_by_satisfied_summary(self):
        from open_story_engine.reader_scene_review import validate_scope
        from tests_api.test_reader_consequences import grounded
        body = '他先看纸包。后来目光转回木牌。'
        data = grounded(body)
        data['checks'][1].update(scopeViolations=['A1'], reason='后半句转看另一物品')
        with self.assertRaises(SceneReviewError) as error:
            validate_scope(data, {'A1': '只看纸包'}, body)
        self.assertEqual(error.exception.violations[0]['claim'], '后来目光转回木牌。')

    def test_focused_counterexamples_must_quote_the_correct_paragraph(self):
        from open_story_engine.reader_scene_review import validate_scope, exclusive_requirements
        body = '他只看纸包。\n\n他没有看木牌。'
        req = {'A1': '只看纸包', 'A2': '听他回答'}
        self.assertEqual(exclusive_requirements(req), {'A1': '只看纸包'})
        data = {'scopeChecks': [dict(id='A1', verdict='violated', paragraphIds=['P2'], reason='错误引用',
                                    counterexamples=[dict(paragraphId='P1', quote='他没有看木牌')])]}
        with self.assertRaisesRegex(ValueError, '逐字引用对应段落'):
            validate_scope(data, {'A1': req['A1']}, body, focused=True)

    def test_malformed_independent_review_is_a_controlled_rejection(self):
        from open_story_engine.reader_scene_review import combined_scene_issues
        for data in [{'checks': None, 'scopeChecks': []}, {'checks': [{'id': []}], 'scopeChecks': []}]:
            with self.assertRaises(ValueError):
                combined_scene_issues({'issues': []}, '你等候。', [], data, evidence={}, requirements={'A1': '等待'})

    def test_single_step_dialogue_uses_planned_budget(self):
        profile = scene_pacing({'scenePlan': {'targetCjk': [350, 600], 'lengthReason': '单次对话但需比较两个依据'}, 'steps': [{}], 'requirements': {'A1': {}}}, {}, {})
        self.assertEqual(profile['targetCjk'], [350, 600])

    def test_independent_omission_merges_with_all_four_issue_types(self):
        from open_story_engine.reader_scene_review import combined_scene_issues
        body = '你答应同行。\n\n他早年见过金砂。\n\n你突然回到山下。\n\n木牌已归他所有。'
        review = {'issues': [dict(paragraphId='P1', type='action', reason='没有授权'),
                             dict(paragraphId='P3', type='continuity', reason='没有转场'),
                             dict(paragraphId='P4', type='state', reason='没有转交')]}
        grounding = {'checks': [dict(id=f'P{i+1}-C1', verdict='unsupported' if i == 1 else 'supported',
                                    reason='经历没有依据' if i == 1 else '') for i in range(4)]}
        with self.assertRaises(SceneReviewError) as error:
            combined_scene_issues(review, body, [], grounding)
        self.assertEqual({v['type'] for v in error.exception.violations}, {'action', 'background', 'continuity', 'state'})

    def test_knowledge_context_never_exposes_hidden_unknown_boundaries(self):
        from open_story_engine.reader_scene_review import scene_knowledge
        knowledge = scene_knowledge({'contract': {'openingContext': {
            'knownFacts': ['你拿着纸包。'], 'unknownBoundaries': ['幕后凶手是某人']}}, 'lineage': []})
        self.assertNotIn('幕后', str(knowledge))
        self.assertIn('不知道', knowledge['answerBoundary'])

    def test_combines_all_semantic_sources_before_reference_errors(self):
        body = '你未经选择便回答。\n\n他说自己见过古碑。\n\n道具忽然归你所有。\n\n你继续追问。'
        review = {'issues': [dict(paragraphId='P1', type='action', reason='未授权回答'),
                             dict(paragraphId='P4', type='action', reason='未授权追问')],
                  'sceneChecks': [dict(paragraphId='P2', background='unsupported', issue='虚构经历', sources='错误格式')],
                  'eventChecks': [dict(id='O3', verdict='unsupported', reason='未登记转交')]}
        with self.assertRaises(SceneReviewError) as error:
            reject_review_issues(review, body, [dict(id='O3', paragraphId='P3')])
        self.assertEqual({v['paragraphId'] for v in error.exception.violations}, {'P1', 'P2', 'P3', 'P4'})
        self.assertEqual(len(error.exception.repair_problem()['issues']), 4)
        self.assertFalse(error.exception.unlocated)
        self.assertTrue(all('待' not in v and '审查问题' in v for v in repair_paragraphs(body, error.exception).values()))

    def test_legacy_invalid_location_and_event_issues_cannot_disappear(self):
        review = {'issues': ['旧格式问题 P2 不得猜测编号',
                             dict(paragraphId='P99', reason='无效段号仍拒绝'),
                             dict(eventId='O1', type='state', reason='事件实际位于第一段')]}
        with self.assertRaises(SceneReviewError) as error:
            reject_review_issues(review, '你停步。\n\n他等待。', [dict(id='O1', paragraphId='P1')])
        self.assertEqual(len(error.exception.unlocated), 2)
        self.assertEqual([v['paragraphId'] for v in error.exception.violations], ['P1'])
        self.assertIn('无效段号仍拒绝', str(error.exception))

    def test_only_public_selected_lineage_and_whole_paragraphs_are_sources(self):
        context = {'contract': {'openingContext': {'knownFacts': ['你已刮出金屑。'], 'unknownBoundaries': ['秘密来历'], 'evidence': ['未来秘密']}},
                   'lineage': [{'id': 'old', 'narrativeText': '旧资料'}, {'id': 'a', 'narrativeText': '你已拿着纸包。'},
                               {'id': 'b', 'narrativeText': '他说不清来历。'}, {'id': 'c', 'narrativeText': '长' * 9100 + '\n\n你等着。'}]}
        evidence = public_scene_evidence(context)
        self.assertEqual(set(evidence.values()), {'你已刮出金屑。', '你已拿着纸包。', '他说不清来历。', '你等着。'})
        self.assertNotIn('秘密', str(evidence))

    def test_no_state_change_does_not_make_new_question_authorized(self):
        body = '你问他后坡哪一带长着青露草。'
        checks = scene_checked(body)
        checks[0].update(playerDecision='overreach', issue='原输入只表明打算，并未授权追问地点')
        with self.assertRaisesRegex(ValueError, '未授权追问') as error:
            validate_scene_review({'sceneChecks': checks}, body, {})
        self.assertNotIsInstance(error.exception, ActionEvidenceError)

    def test_npc_background_needs_prior_evidence_not_new_draft(self):
        body = '他声称看过旧牌。'
        check = dict(paragraphId='P1', playerDecision='none', background='supported', sources=[{'id': 'draft-P1', 'quote': body}])
        with self.assertRaises(ActionEvidenceError):
            validate_scene_review({'sceneChecks': [check]}, body, {'opening-1': '你刚认识他。'})
        check.update(background='unsupported', issue='前情未提供其到达时间和看牌经历')
        with self.assertRaisesRegex(ValueError, '看牌经历') as error:
            validate_scene_review({'sceneChecks': [check]}, body, {})
        self.assertNotIsInstance(error.exception, ActionEvidenceError)
        check.update(background='supported', sources=[{'id': 'H1', 'quote': '他曾说看过旧牌。'}], backgroundClaims=[dict(claim=body, verdict='supported', sources=[{'id': 'H1', 'quote': '他曾说看过旧牌。'}], reason='仅确认他曾自述见过旧牌')])
        validate_scene_review({'sceneChecks': [check]}, body, {'H1': '他曾说看过旧牌。'})
        # Existence is checked here; semantic support is the model review's job.

    def test_missing_duplicate_and_false_references_fail(self):
        body = '你开口。\n\n他不愿回答。'
        good = scene_checked(body)
        for bad in [[], good[:1], [good[0], copy.deepcopy(good[0])]]:
            with self.assertRaises(ActionEvidenceError):
                validate_scene_review({'sceneChecks': bad}, body, {})
        validate_scene_review({'sceneChecks': good}, body, {})

    def test_missing_plan_does_not_invent_budget_from_step_count(self):
        profile = scene_pacing({'steps': [{}], 'requirements': {'A1': {}, 'A2': {}, 'A3': {}}}, {}, {})
        self.assertEqual(profile['level'], 'unplanned')
        self.assertIsNone(profile['targetCjk'])

    def test_expansion_preserves_original_order_and_final_decision(self):
        body = '你展示纸包。\n\n他低头观察。\n\n你等着，尚未作出决定。'
        expanded = expand_scene_paragraphs(body, {'insertions': [
            {'paragraphId': 'P2', 'before': '纸角轻轻颤动。', 'after': '他的目光停在金屑上。'}]})
        self.assertEqual([p for p in expanded.split('\n\n') if p in body], body.split('\n\n'))
        self.assertTrue(expanded.startswith(body.split('\n\n')[0]))
        self.assertTrue(expanded.endswith(body.split('\n\n')[-1]))
        self.assertEqual(expand_scene_paragraphs(body, {'insertions': []}), body)
        for insertion in [
            {'paragraphId': 'P1', 'before': '另一场景', 'after': ''},
            {'paragraphId': 'P3', 'before': '', 'after': '你决定离开。'},
            {'paragraphId': 'P4', 'before': '', 'after': ''},
            {'paragraphId': 'P2', 'before': '长' * 2201, 'after': ''},
        ]:
            with self.assertRaises(ValueError):
                expand_scene_paragraphs(body, {'insertions': [insertion]})

    def test_explicit_brief_request_overrides_complexity(self):
        profile = scene_pacing({'readingIntent': 'brief', 'steps': [{}] * 5}, {}, {'playerLocationId': 'elsewhere'})
        self.assertEqual(profile['level'], 'brief')

    def test_overlong_expansion_keeps_whole_prefix_and_original_ending(self):
        body = '你展示纸包。\n\n他低头观察。\n\n你等着，尚未作出决定。'
        first, second = '纸角轻轻颤动。', '他的目光停在金屑上，没有开口。'
        data = {'insertions': [{'paragraphId': 'P2', 'after': second},
                               {'paragraphId': 'P1', 'after': first}]}
        import re
        budget = len(re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]', body + first))
        result = expand_scene_paragraphs(body, data, max_cjk=budget)
        self.assertEqual(result, '你展示纸包。\n\n' + first + '\n\n他低头观察。\n\n你等着，尚未作出决定。')
        self.assertEqual(expand_scene_paragraphs(body, data, max_cjk=1), body)

    def test_extracted_background_cannot_escape_source_review(self):
        body = '他声称曾在外门看过旧牌。'
        events = [{'paragraphId': 'P1', 'mode': 'background'}]
        with self.assertRaises(ActionEvidenceError):
            validate_scene_review({'sceneChecks': scene_checked(body)}, body, {}, events)

    def test_background_conflict_cannot_be_repaired_as_reference_typo(self):
        body = '他说必须越过白线才能完成试炼。'
        check = dict(paragraphId='P1', playerDecision='none', background='supported', sources=[],
                     backgroundClaims=[dict(claim=body, verdict='contradicted', sources=[], reason='原文禁止越界')])
        with self.assertRaises(ValueError) as error:
            validate_scene_review({'sceneChecks': [check]}, body, {})
        self.assertNotIsInstance(error.exception, ActionEvidenceError)
        check['backgroundClaims'][0].update(verdict='supported', sources=[dict(id='new-draft', quote=body)])
        with self.assertRaises(ActionEvidenceError):
            validate_scene_review({'sceneChecks': [check]}, body, {})

    def test_independent_grounding_rejects_missing_and_unsupported_facts(self):
        claims = {'P2-C1': {'claim': '草药只长在背阴处', 'evidence': ['日落前取回草药。']}}
        for data in [{'checks': []}, {'checks': [{'id': 'P2-C1', 'verdict': 'unsupported', 'reason': '前文未描述生长环境'}]},
                     {'checks': [{'id': 'unknown', 'verdict': 'supported'}]}]:
            with self.assertRaises(ValueError):
                validate_grounding(data, claims)
        self.assertEqual(len(validate_grounding({'checks': [{'id': 'P2-C1', 'verdict': 'supported'}]}, claims)), 1)

    def test_background_review_binds_whole_paragraph_without_model_transcription(self):
        body = '“日落前取回草药。”他说，“别越过白线。”'
        refs = [{'id': 'F1', 'quote': '日落前取回草药，不得越过白线。'}]
        checks = validate_scene_review({'sceneChecks': [dict(paragraphId='P1', playerDecision='none', background='supported', sources=refs)]}, body, {'F1': refs[0]['quote']})
        claims = grounding_claims(body)
        self.assertEqual(''.join(c['claim'] for c in claims.values()), body)
        self.assertEqual(set(claims), {'P1-C1', 'P1-C2'})

    def test_independent_review_covers_paragraphs_first_review_calls_none(self):
        body = '你看着他。\n\n他说这里没有试金石。'
        checks = scene_checked(body)
        self.assertTrue(all(c['background'] == 'none' for c in checks))
        claims = grounding_claims(body)
        self.assertEqual(set(claims), {'P1-C1', 'P2-C1'})
        with self.assertRaises(SceneGroundingError):
            validate_grounding({'checks': [dict(id='P1-C1', verdict='supported'),
                                          dict(id='P2-C1', verdict='unsupported', reason='当地器物配置无依据')]}, claims)

    def test_all_grounding_issues_are_masked_for_one_repair(self):
        from open_story_engine.api_narrative import apply_scene_repairs
        body = '你留在原地。\n\n草只长在阴处。\n\n这里从未下雨。\n\n他静静等候。'
        claims = {'P2-C1': {'claim': '草只长在阴处。'}, 'P3-C1': {'claim': '这里从未下雨。'}}
        with self.assertRaises(SceneGroundingError) as error:
            validate_grounding({'checks': [dict(id=k, verdict='unsupported', reason='前文未提供') for k in claims]}, claims)
        self.assertEqual(len(error.exception.violations), 2)
        masked = repair_paragraphs(body, error.exception)
        self.assertNotIn('草只长在阴处', str(masked))
        self.assertNotIn('这里从未下雨', str(masked))
        self.assertEqual(masked['P1'], '你留在原地。')
        with self.assertRaisesRegex(ValueError, '保留'):
            apply_scene_repairs(body, [{'paragraphId': 'P4', 'text': '他抬头等候。'}], error.exception.violations)
        with self.assertRaisesRegex(ValueError, '标记'):
            apply_scene_repairs(body, [{'paragraphId': 'P2', 'text': masked['P2']}])
        repaired = apply_scene_repairs(body, [{'paragraphId': 'P2', 'text': '草的生境还不清楚。'}, {'paragraphId': 'P3', 'text': '他并未提供天气的线索。'}], error.exception.violations)
        self.assertTrue(repaired.endswith('他静静等候。'))
