"""Official longform regressions for dialogue identity and factual premises."""
import copy
import json
import unittest

from open_story_engine.reader_scene_review import (
    SceneReviewError, grounding_input_evidence, scene_speaker_candidates, validate_knowledge_access,
)
from test_support.context_prose_ab import ROOT, _entry_samples


class DialogueGroundingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample = _entry_samples(1)[0]
        report = json.loads((ROOT / 'docs/evidence/context-management-2026-09-22/context-final-guard-validation-2026-09-25.json').read_text())
        cls.body = report['production']['result']['narrativeText']
        cls.payload = json.loads(report['production']['calls'][5]['messages'][1]['content'])
        cls.evidence = grounding_input_evidence(cls.payload)

    def test_candidates_include_public_unnamed_role_without_registering_it(self):
        context = self.sample['context']
        before = copy.deepcopy((context['contract'], context['parent'], context['package']['characters']))
        people = scene_speaker_candidates(self.sample['context'], self.body, self.evidence)
        self.assertIn('守门弟子', people.values())
        self.assertNotIn('陆沉舟', people.values())
        self.assertTrue(next(k for k, v in people.items() if v == '守门弟子').startswith('scene-speaker:'))
        self.assertEqual((context['contract'], context['parent'], context['package']['characters']), before)
        self.assertEqual(people, scene_speaker_candidates(self.sample['context'], self.body, self.evidence))

    def test_role_needs_public_source_and_cannot_be_created_from_dialogue_alone(self):
        context = self.sample['context']
        self.assertNotIn('守门弟子', scene_speaker_candidates(context, self.body, {}).values())
        self.assertNotIn('守门弟子', scene_speaker_candidates(context, '你问：“守门弟子在哪？”', self.evidence).values())
        self.assertNotIn('巡山使者', scene_speaker_candidates(context, '巡山使者回答。', self.evidence).values())

    def check(self):
        return dict(id='D1', speakerId='guard', speakerName='守门弟子', premises=[],
                    kind='current', verdict='supported', accessSources=[], missingEvidence=[], reason='当场回应')

    def validate(self, check, evidence=None):
        names = {'player': '陆照临', 'guard': '守门弟子', 'father': '陆沉舟'}
        validate_knowledge_access({'knowledgeChecks': [check]}, '守门弟子说：“我去敲传事钟。”',
                                  evidence or {}, names, 'player', speaker_names=names)

    def test_valid_id_does_not_override_mismatched_speaker_name(self):
        check = self.check()
        check['speakerId'] = 'father'
        with self.assertRaisesRegex(ValueError, '姓名与候选编号不一致'):
            self.validate(check)
        check['speakerId'] = 'guard'
        del check['speakerName']
        with self.assertRaisesRegex(ValueError, '姓名与候选编号不一致'):
            self.validate(check)

    def test_current_speech_does_not_override_unsupported_factual_premise(self):
        check = self.check()
        check['premises'] = [dict(quote='敲传事钟', kind='background', verdict='unsupported',
                                 sources=[], reason='没有传事钟存在的公开依据')]
        with self.assertRaisesRegex(SceneReviewError, '传事钟存在'):
            self.validate(check)
        check['premises'][0]['verdict'] = 'supported'
        with self.assertRaisesRegex(SceneReviewError, '没有公开来源'):
            self.validate(check)

    def test_premise_must_bind_current_dialogue_and_exact_source(self):
        check = self.check()
        premise = dict(quote='敲传事钟', kind='background', verdict='supported',
                       claim='存在可供敲击的传事钟',
                       sources=[dict(id='opening', quote='守门弟子身旁有传事钟')], reason='公开材料支持设施存在')
        check['premises'] = [premise]
        # Explicit synthetic source, not a claim that the official book has this bell.
        check['accessSources'] = [dict(id='opening', quote='守门弟子身旁有传事钟')]
        self.validate(check, {'opening': '守门弟子身旁有传事钟。'})
        premise['sources'][0]['id'] = 'invented'
        with self.assertRaisesRegex(ValueError, '来源不在公开资料'):
            self.validate(check, {'opening': '守门弟子身旁有传事钟。'})
        premise['quote'] = '敲另一口钟'
        with self.assertRaisesRegex(ValueError, '未绑定当前台词'):
            self.validate(check)

    def test_missing_premise_inventory_is_not_a_clean_review(self):
        check = self.check()
        del check['premises']
        with self.assertRaisesRegex(ValueError, '事实前提列表'):
            self.validate(check)

    def test_rejected_normalized_premise_repairs_verbatim_dialogue(self):
        check = self.check()
        check['premises'] = [dict(quote='敲传事钟', claim='存在可供敲击的传事钟',
                                 kind='background', verdict='unsupported', sources=[],
                                 reason='没有公开设施依据')]
        with self.assertRaises(SceneReviewError) as caught:
            self.validate(check)
        issue = caught.exception.repair_problem()['issues'][0]
        self.assertEqual(issue['quote'], '敲传事钟')
        self.assertEqual(issue['paragraphId'], 'P1')
        self.assertEqual(issue['reason'], '没有公开设施依据')

    def test_current_label_cannot_hide_npc_access_requirement(self):
        check = self.check()
        check['premises'] = [dict(quote='敲传事钟', kind='background', verdict='supported',
                                  claim='存在可供敲击的传事钟',
                                  sources=[dict(id='opening', quote='此地设有传事钟')], reason='有设施依据')]
        with self.assertRaisesRegex(SceneReviewError, '未提供该NPC获得信息'):
            self.validate(check, {'opening': '此地设有传事钟。'})

    def test_direct_current_observation_has_no_external_premise(self):
        check = self.check()
        check['premises'] = []
        check['reason'] = '当场可见的身体反应'
        self.validate(check)

    def test_player_current_observation_premise_is_tolerated_but_not_grounded(self):
        check = self.check()
        check['speakerId'] = 'player'
        check['speakerName'] = '陆照临'
        check['premises'] = [dict(quote='敲传事钟', kind='current', verdict='supported', sources=[],
                                  reason='当前观察')]
        self.validate(check)

    def test_current_is_not_a_valid_external_premise_kind(self):
        check = self.check()
        check['premises'] = [dict(quote='敲传事钟', claim='存在可供敲击的传事钟', kind='current',
                                  verdict='supported', sources=[], reason='错误把设施前提当当前动作')]
        with self.assertRaisesRegex(ValueError, '事实前提未绑定当前台词或分类无效'):
            self.validate(check)

    def test_unknown_premise_without_source_is_ignored(self):
        check = self.check()
        check['premises'] = [dict(quote='敲传事钟', claim='是否有人来未知', kind='unknown',
                                  verdict='supported', sources=[], reason='当场承认未知')]
        self.validate(check)


if __name__ == '__main__':
    unittest.main()
