import unittest
from unittest.mock import Mock

from open_story_engine.api_narrative import apply_scene_repairs, complete_with_retry
from open_story_engine.llm import LlmError
from open_story_engine.reader_scene_review import (
    SceneReviewError, SceneReviewFormatError, combined_scene_issues, dialogue_units, repair_targets,
    validate_knowledge_access, validate_repair_resolution,
)


class EpistemicTests(unittest.TestCase):
    def test_semantic_rejection_precedes_malformed_audit_reference(self):
        body = '他曾见过古碑。'
        review = {'issues': [dict(paragraphId='P1', type='background', quote=body, reason='经历缺证')]}
        with self.assertRaises(SceneReviewError) as caught:
            combined_scene_issues(review, body, [], {'checks': []})
        self.assertNotIsInstance(caught.exception, SceneReviewFormatError)

    def check(self, ref=None, **updates):
        return dict(id='D1', speakerId='npc', kind='reported', verdict='supported',
                    accessSources=[ref] if ref else [], missingEvidence=[], reason='复述听到的信息', **updates)

    def test_npc_cannot_use_self_or_future_speech_as_acquisition(self):
        body = '他说：“我知道来自北坡。”\n\n你说来自北坡。'
        for source in (None, dict(id='draft-P1', quote='我知道来自北坡'), dict(id='draft-P2', quote='你说来自北坡')):
            with self.subTest(source=source), self.assertRaises(SceneReviewError):
                validate_knowledge_access({'knowledgeChecks': [self.check(source)]}, body, {}, {'npc', 'player'}, 'player')

    def test_prior_disclosure_in_same_paragraph_can_be_repeated(self):
        body = '你先说明来自北坡。他说：“按你刚才说的，来自北坡。”'
        data = {'knowledgeChecks': [self.check(dict(id='draft-P1', quote='你先说明来自北坡'))]}
        validate_knowledge_access(data, body, {}, {'npc', 'player'}, 'player')

    def test_new_visibility_narration_cannot_prove_its_own_observation(self):
        body = '纸里透出一点金色。\n\n他说：“我看见金屑了。”'
        check = self.check(dict(id='draft-P1', quote='纸里透出一点金色'))
        for kind in ('current', 'background', 'inference'):
            check['kind'] = kind
            with self.subTest(kind=kind), self.assertRaisesRegex(SceneReviewError, '不能证明观察前提'):
                validate_knowledge_access({'knowledgeChecks': [check]}, body, {}, {'npc'}, 'player')

    def test_current_reply_can_reference_an_earlier_actual_utterance(self):
        body = '你说：“我打算去后坡。”\n\n他说：“这件事我帮不上。”'
        first = self.check()
        first.update(speakerId='player', kind='current', accessSources=[])
        reply = self.check(dict(id='draft-P1', quote='“我打算去后坡。”'))
        reply.update(id='D2', kind='current')
        validate_knowledge_access({'knowledgeChecks': [first, reply]}, body, {}, {'npc', 'player'}, 'player')

    def test_missing_acquisition_cannot_be_erased_by_positive_verdict(self):
        data = {'knowledgeChecks': [self.check(dict(id='opening-1', quote='你捡到了一粒金屑'))]}
        data['knowledgeChecks'][0]['missingEvidence'] = ['没有他看见玩家拾取的来源']
        with self.assertRaisesRegex(SceneReviewError, '没有他看见'):
            validate_knowledge_access(data, '他说：“我看见你捡起金屑。”',
                                      {'opening-1': '你捡到了一粒金屑。'}, {'npc', 'player'}, 'player')

    def test_unknown_answer_requires_no_invented_acquisition(self):
        check = self.check()
        check.update(kind='unknown', reason='明确不知道')
        validate_knowledge_access({'knowledgeChecks': [check]}, '他说：“我不知道。”', {}, {'npc'}, 'player')
        for data in ({}, {'knowledgeChecks': []}, {'knowledgeChecks': [check, check]}):
            with self.assertRaises(ValueError):
                validate_knowledge_access(data, '他说：“我不知道。”', {}, {'npc'}, 'player')

    def test_quote_ids_cover_multiple_styles_and_do_not_guess_speakers(self):
        body = '你说：“先等。”\n\n他答：「好。」她提起“北坡”二字。'
        units = dialogue_units(body)
        self.assertEqual([u['paragraphId'] for u in units.values()], ['P1', 'P2', 'P2'])
        self.assertTrue(all(body[u['start']:].startswith(u['quote']) for u in units.values()))
        self.assertTrue(all('speakerId' not in u for u in units.values()))

    def test_repair_resolution_must_cover_every_old_problem(self):
        targets = repair_targets({'issues': [dict(type='background', quote='木纹必有痕迹', reason='没有物性依据')]})
        body = '他说木纹或许有痕迹。'
        for verdict in ('retained', 'rephrased', 'uncertain'):
            with self.subTest(verdict=verdict), self.assertRaises(SceneReviewError):
                validate_repair_resolution({'repairChecks': [dict(id='R1', verdict=verdict, paragraphIds=['P1'], reason='仍有同一物性前提')]}, body, targets)
        with self.assertRaises(ValueError):
            validate_repair_resolution({'repairChecks': []}, body, targets)
        with self.assertRaises(SceneReviewError):
            validate_repair_resolution({'repairChecks': [dict(id='R1', verdict='resolved', paragraphIds=[], reason='已删除')]},
                                       '他说木纹必有痕迹。', targets)
        validate_repair_resolution({'repairChecks': [dict(id='R1', verdict='resolved', paragraphIds=[], reason='错误段已删除')]},
                                   '他没有依据，不能判断。', targets)

    def test_rephrased_error_overrides_general_background_pass(self):
        body = '木纹或许有痕迹。'
        grounding = {'checks': [dict(id='P1-C1', verdict='supported')],
                     'repairChecks': [dict(id='R1', verdict='rephrased', paragraphIds=['P1'], reason='只改变语气')]}
        with self.assertRaises(SceneReviewError):
            combined_scene_issues({'issues': []}, body, [], grounding,
                                  repair_issues={'issues': [dict(type='background', quote='木纹必有痕迹', reason='没有依据')]})

    def test_fixing_one_bad_occurrence_can_keep_an_earlier_valid_occurrence(self):
        targets = {'R1': dict(type='continuity', quote='他又问了一遍', beforeOccurrences=2)}
        validate_repair_resolution({'repairChecks': [dict(id='R1', verdict='resolved', paragraphIds=['P2'], reason='已去掉错误重演')]},
                                   '他又问了一遍。\n\n他听完后等着你。', targets)

    def test_delete_wrong_middle_paragraph_without_filler(self):
        body = '你询问来历。\n\n他说木纹必有痕迹。\n\n他承认不知道，你听完仍未决定。'
        issue = dict(paragraphId='P2', claim='他说木纹必有痕迹。')
        fixed = apply_scene_repairs(body, [dict(paragraphId='P2', text='')], [issue])
        self.assertEqual(fixed, '你询问来历。\n\n他承认不知道，你听完仍未决定。')
        for pid, issues in [('P1', [dict(paragraphId='P1', claim='你询问来历。')]), ('P3', [issue]), ('P2', [])]:
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                apply_scene_repairs(body, [dict(paragraphId=pid, text='')], issues)

    def test_delete_dialogue_still_requires_fixing_dependent_reference(self):
        body = '你在原地认真听着，没有打断他的声音。\n\n他说：“后坡不小。”\n\n说到“后坡不小”时，他望着你，等待你的决定。'
        with self.assertRaisesRegex(ValueError, '删除了前文台词'):
            apply_scene_repairs(body, [dict(paragraphId='P2', text='')], [dict(paragraphId='P2', claim='他说：“后坡不小。”')])

    def test_transient_failure_records_stage_without_extra_retry(self):
        failure = LlmError('首段超时', 'transport_error')
        failure.observations = [{'outcome': 'failed'}]
        second = LlmError('首段超时', 'transport_error')
        second.observations = [{'outcome': 'failed'}]
        gateway = Mock()
        gateway.complete_json.side_effect = [failure, second]
        with self.assertRaises(LlmError) as caught:
            complete_with_retry(gateway, 'complete_json', [], stage='scene_grounding')
        self.assertEqual(gateway.complete_json.call_count, 2)
        self.assertEqual([o['generationStage'] for o in caught.exception.observations], ['scene_grounding'] * 2)
