import copy
import unittest

from open_story_engine.reader_scene_plan import plan_premises, validate_plan_premises, validate_scene_plan
from open_story_engine.api_reader_quality import scene_pacing
from open_story_engine.reader_scene_review import (
    SceneReviewError, grounding_claims, reject_review_issues, repair_paragraphs,
    scene_knowledge, validate_grounding,
    scene_boundaries, validate_scene_boundaries, combined_scene_issues,
)
from tests_api.test_reader_consequences import GU, plan


class ScenePlanTests(unittest.TestCase):
    def setUp(self):
        self.contract = plan({'A1': '请说明依据'})
        self.evidence = {
            'opening-1': '纸包仍然合着。',
            'history-a-P1': '他说自己不清楚来历。',
            'history-a-P2': '石台边缘有粗石板，雨幕遮住远处。',
        }

    def validate(self, contract):
        return validate_scene_plan(contract, self.evidence, {GU})

    def test_unknown_needs_no_invented_source_but_facts_do(self):
        item = dict(speakerId=GU, statement='不知道来历', status='unknown', sources=[])
        self.contract['scenePlan']['knowledge'] = [item]
        self.validate(self.contract)
        for status in ('fact', 'reported', 'inference'):
            item['status'] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.validate(self.contract)
        item.update(status='reported', sources=[dict(id='history-a-P1', quote='自己不清楚来历')])
        self.validate(self.contract)
        item['sources'][0]['quote'] = '过去没有去过那里'
        with self.assertRaises(ValueError):
            self.validate(self.contract)

    def test_prewrite_review_must_cover_prerequisites_and_cannot_self_prove(self):
        self.contract['scenePlan']['observationLimits'] = ['能看见纸里的金屑']
        self.assertEqual(plan_premises(self.contract['scenePlan']), {'O1': {'statement': '能看见纸里的金屑'}})
        check = dict(id='O1', kind='existing', verdict='supported', sources=[], stepIds=[], missingEvidence=[], reason='纸里可见')
        for checks in ([], [check, check], [check]):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                validate_plan_premises({'premiseChecks': checks}, self.contract, self.evidence)
        check['sources'] = [dict(id='O1', quote='能看见纸里的金屑')]
        with self.assertRaisesRegex(ValueError, '不得引用规划自证'):
            validate_plan_premises({'premiseChecks': [check]}, self.contract, self.evidence)
        check.update(kind='restriction', sources=[], missingEvidence=['没有打开纸包的依据'])
        with self.assertRaisesRegex(ValueError, '前提缺证'):
            validate_plan_premises({'premiseChecks': [check]}, self.contract, self.evidence)

    def test_prewrite_review_reports_the_missing_k1_contract_field(self):
        self.contract['scenePlan']['knowledge'] = [dict(
            speakerId=GU, statement='引荐文书已由玩家持有，尚未永久损毁',
            status='reported', sources=[dict(id='opening-1', quote='纸包仍然合着')],
        )]
        check = dict(id='K1', kind='existing', verdict='supported', sources=[
            dict(id='opening-1', quote='纸包仍然合着')], stepIds=[], reason='性质和来源均已核对')
        with self.assertRaisesRegex(ValueError, r'K1.*missingEvidence'):
            validate_plan_premises({'premiseChecks': [check]}, self.contract, self.evidence)

    def test_prewrite_future_knowledge_requires_actual_dependency(self):
        self.contract['scenePlan']['knowledge'] = [dict(status='pending', afterStepId='S1', statement='听完才知道')]
        check = dict(id='K1', kind='after_step', verdict='supported', sources=[], stepIds=['S1'], missingEvidence=[], reason='告知后才听到')
        validate_plan_premises({'premiseChecks': [check]}, self.contract, self.evidence)
        for updates in (dict(stepIds=[]), dict(stepIds=['S99']), dict(kind='unknown')):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_plan_premises({'premiseChecks': [{**check, **updates}]}, self.contract, self.evidence)

    def test_prewrite_existing_claim_cannot_be_downgraded_to_unknown(self):
        self.contract['scenePlan']['knowledge'] = [dict(status='fact', statement='纸包合着')]
        check = dict(id='K1', kind='existing', verdict='supported', sources=[dict(id='opening-1', quote='纸包仍然合着')], stepIds=[], missingEvidence=[], reason='来源一致')
        validate_plan_premises({'premiseChecks': [check]}, self.contract, self.evidence)
        check.update(kind='unknown', sources=[])
        with self.assertRaisesRegex(ValueError, '改分类'):
            validate_plan_premises({'premiseChecks': [check]}, self.contract, self.evidence)

    def test_current_observation_inference_uses_after_step_without_state_change(self):
        self.contract['steps'][0]['action'] = '停在石台边缘观察石台、脚印和道路，不往里走'
        self.contract['scenePlan']['observationLimits'] = ['雨幕限制远处视线，未发现不等于绝对无人']
        self.contract['scenePlan']['knowledge'] = [
            dict(speakerId=GU, status='inference',
                 statement='当前观察范围内未发现明确人迹，路径只辨认出石径来路。',
                 sources=[dict(id='history-a-P2', quote='石台边缘有粗石板，雨幕遮住远处。')]),
            dict(speakerId=GU, status='unknown', statement='不知道石台之外是否有人。', sources=[]),
        ]
        checks = [
            dict(id='K1', kind='after_step', verdict='supported', sources=[], stepIds=['S1'],
                 missingEvidence=[], reason='观察步骤产生的暂时推断'),
            dict(id='K2', kind='unknown', verdict='supported', sources=[], stepIds=[],
                 missingEvidence=[], reason='只保留当前不知道的范围'),
            dict(id='O1', kind='restriction', verdict='supported', sources=[], stepIds=[],
                 missingEvidence=[], reason='只限制远处观察范围'),
        ]
        self.validate(self.contract)
        validate_plan_premises({'premiseChecks': checks}, self.contract, self.evidence)
        self.assertEqual(self.contract['stateChanges'], [])

    def test_observation_exception_does_not_cover_movement_or_unauthorized_reaction(self):
        self.contract['scenePlan']['observationLimits'] = ['雨幕限制远处视线']
        self.contract['scenePlan']['knowledge'] = [dict(
            speakerId=GU, status='inference', statement='当前观察范围内未发现明确人迹',
            sources=[dict(id='history-a-P2', quote='石台边缘有粗石板，雨幕遮住远处。')],
        )]
        check = dict(id='K1', kind='after_step', verdict='supported', sources=[], stepIds=['S1'],
                     missingEvidence=[], reason='观察步骤产生的暂时推断')
        limit = dict(id='O1', kind='restriction', verdict='supported', sources=[], stepIds=[],
                     missingEvidence=[], reason='只限制远处观察范围')
        self.contract['stateChanges'] = [dict(id='C1', entityId=GU, attribute='locationId',
                                              before='location_open_gate', value='location_open_trial', stepId='S1')]
        with self.assertRaisesRegex(ValueError, '改分类'):
            validate_plan_premises({'premiseChecks': [check, limit]}, self.contract, self.evidence)
        self.contract['stateChanges'] = []
        self.contract['outcomes'] = [dict(characterId=GU, status='injured', permanence='temporary')]
        self.contract['steps'][0]['authority'] = 'player'
        self.contract['steps'][0]['causeStepId'] = None
        with self.assertRaisesRegex(ValueError, '改分类'):
            validate_plan_premises({'premiseChecks': [check, limit]}, self.contract, self.evidence)
        self.contract['outcomes'] = []
        self.contract['steps'][0]['authority'] = 'reaction'
        self.contract['steps'][0]['causeStepId'] = 'S0'
        with self.assertRaisesRegex(ValueError, '改分类'):
            validate_plan_premises({'premiseChecks': [check, limit]}, self.contract, self.evidence)

    def test_plan_cannot_omit_or_add_authorized_steps(self):
        for refs in ([], ['S2']):
            self.contract['scenePlan']['beats'][0]['stepIds'] = refs
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.validate(self.contract)
        self.contract['scenePlan']['beats'][0]['stepIds'] = ['S1']
        self.contract['steps'].append({'id': 'S2'})
        with self.assertRaisesRegex(ValueError, '遗漏'):
            self.validate(self.contract)

    def test_future_disclosure_is_pending_not_a_preexisting_fact(self):
        item = dict(speakerId=GU, statement='听完本回合告知才获知', status='pending', sources=[], afterStepId='S1')
        self.contract['scenePlan']['knowledge'] = [item]
        self.validate(self.contract)
        item['afterStepId'] = 'S99'
        with self.assertRaises(ValueError):
            self.validate(self.contract)
        item.update(status='reported', afterStepId='S1')
        with self.assertRaisesRegex(ValueError, 'pending'):
            self.validate(self.contract)

    def test_missing_or_malformed_plan_rejected_before_writing(self):
        scalar = copy.deepcopy(self.contract)
        scalar['scenePlan']['targetCjk'] = 120
        self.validate(scalar)
        self.assertEqual(scalar['scenePlan']['targetCjk'], [120, 120])

        interval = copy.deepcopy(self.contract)
        interval['scenePlan']['targetCjk'] = [80, 1500]
        self.validate(interval)
        self.assertEqual(interval['scenePlan']['targetCjk'], [80, 1500])

        bad = copy.deepcopy(self.contract)
        del bad['scenePlan']
        with self.assertRaises(ValueError):
            self.validate(bad)
        for target in ([True, 600], [700, 300], [0, 200], [200, 2500], 79, 1501, '120', None):
            bad = copy.deepcopy(self.contract)
            bad['scenePlan']['targetCjk'] = target
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.validate(bad)
        missing = copy.deepcopy(self.contract)
        del missing['scenePlan']['targetCjk']
        with self.assertRaises(ValueError):
            self.validate(missing)

    def test_scene_pacing_uses_the_same_normalized_interval(self):
        profile = scene_pacing({'scenePlan': {'targetCjk': 120, 'lengthReason': '单一结果'}}, {}, {})
        self.assertEqual(profile['targetCjk'], [120, 120])

    def test_public_knowledge_does_not_reveal_internal_state(self):
        context = {'contract': {'openingContext': {'knownFacts': ['纸包仍然合着。']}},
                   'parent': {'branchState': {'characterLocationIds': {'幕后人物': '密室'},
                                              'readerEntityStates': {'纸包': {'内容': '隐藏线索'}}}},
                   'lineage': [{'id': 'a', 'narrativeText': '他说自己不清楚来历。'}]}
        knowledge = scene_knowledge(context)
        self.assertNotIn('密室', str(knowledge))
        self.assertNotIn('隐藏线索', str(knowledge))
        self.assertEqual(knowledge['sourceKinds']['history-a-P1'], 'history_scene')

    def test_precise_repair_keeps_valid_neighbor_sentence(self):
        body = '你站在原地等候。他曾经见过古碑。'
        review = {'issues': [dict(paragraphId='P1', type='background', quote='他曾经见过古碑。', reason='经历缺证')]}
        with self.assertRaises(SceneReviewError) as caught:
            reject_review_issues(review, body)
        masked = repair_paragraphs(body, caught.exception)['P1']
        self.assertTrue(masked.startswith('你站在原地等候。'))
        self.assertNotIn('见过古碑', masked)
        # A second finding of the same type cannot vanish behind the first.
        review['issues'].append(dict(paragraphId='P1', type='background', reason='另一处背景也缺证'))
        with self.assertRaises(SceneReviewError) as caught:
            reject_review_issues(review, body)
        self.assertEqual(len(caught.exception.violations), 2)

    def test_reported_claim_requires_real_source(self):
        body = '他先前说自己不清楚来历。'
        checks = {'checks': [dict(id='P1-C1', kind='reported', verdict='supported', reason='保留说法来源',
                                 sources=[dict(id='history-a-P1', quote='自己不清楚来历')])]}
        validate_grounding(checks, grounding_claims(body), evidence=self.evidence)
        checks['checks'][0]['sources'] = []
        with self.assertRaises(SceneReviewError):
            validate_grounding(checks, grounding_claims(body), evidence=self.evidence)

    def test_boundary_check_cannot_skip_observation_limits_or_inherit_planned_answers(self):
        self.contract['scenePlan']['observationLimits'] = ['纸包未打开，不可看见内部颜色', '不能看见木牌背面']
        self.contract['scenePlan']['knowledge'] = [dict(speakerId=GU, status='unknown', statement='来历未知', sources=[])]
        boundaries = scene_boundaries(self.contract['scenePlan'])
        self.assertEqual(set(boundaries), {'O1', 'O2'})
        self.assertNotIn('来历未知', str(boundaries))
        body = '纸包合着。他说不知道。'
        checks = [dict(id=k, verdict='satisfied', paragraphIds=['P1'], reason='保留观察与认知限制') for k in boundaries]
        validate_scene_boundaries({'boundaryChecks': checks}, body, boundaries)
        for bad in (None, {}, {'boundaryChecks': checks[:1]}, {'boundaryChecks': checks + checks[:1]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_scene_boundaries(bad, body, boundaries)

    def test_boundary_violation_survives_other_reviewers_passing(self):
        body = '纸包合着，却看清里面的颜色。'
        grounding = {'checks': [dict(id='P1-C1', verdict='supported')],
                     'boundaryChecks': [dict(id='O1', verdict='violated', paragraphIds=['P1'], reason='增加未授权的可见条件')]}
        with self.assertRaises(SceneReviewError) as caught:
            combined_scene_issues({'issues': []}, body, [], grounding, boundaries={'O1': '纸包合着看不清内部'})
        self.assertEqual(caught.exception.violations[0]['type'], 'background')
