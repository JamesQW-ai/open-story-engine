"""General action/state gates: no story-specific action keyword whitelist."""
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import unittest

from open_story_engine import reader_actions as ra, reader_consequences as rc
from tests_api import test_reader_consequences as fixtures
from tests_api.test_reader_consequences import grounded, GU, LU, plan, observed, checked, seal, authority, scene_checked


class GeneralActionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ConsequenceTests()
        fixture.setUp()
        self.context = fixture.context
        self.state = fixture.root['branchState']
        self.package = fixture.package
        self.plan = plan({'A1': '按当前条件完成我要求的动作'})
        self.context['playerDirection'] = '按当前条件完成我要求的动作'

    def change(self, entity, attr, value, before=None):
        self.plan['stateChanges'].append(dict(id='C' + str(len(self.plan['stateChanges']) + 1), entityId=entity,
                                             attribute=attr, before=before, value=value, stepId='S1', reason='用户的动作导致此变化'))

    def test_permanently_unavailable_actor_cannot_resume_duties(self):
        candidate = copy.deepcopy(self.plan)
        candidate['steps'].append(dict(id='S2', actorId=LU, action='回答你的提问并继续带路',
                                       requirementIds=['A1'], authority='reaction', causeStepId='S1'))
        for status in ('dead', 'departed'):
            with self.subTest(status=status):
                state = copy.deepcopy(self.state)
                state['characterOutcomeStates'][LU] = dict(status=status, permanence='permanent')
                context = {**self.context, 'parent': {**self.context['parent'], 'branchState': state}}
                with self.assertRaisesRegex(ValueError, '永久下线'):
                    ra.validate_plan(candidate, context)
                # A player may still inspect the aftermath without recruiting the actor.
                ra.validate_plan(self.plan, context)
                ra.validate_plan(candidate, self.context)

    def test_current_speech_cannot_bypass_outcome_but_recollection_is_allowed(self):
        body = '陆照临说，让你继续往前走。'
        events = observed(body)['events']
        events[0].update(actor=LU, mode='speech')
        for status in ('dead', 'departed'):
            state = copy.deepcopy(self.state)
            state['characterOutcomeStates'][LU] = dict(status=status, permanence='permanent')
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, '永久下线'):
                ra.validate_events({'eventChecks': checked(body)}, events, self.plan, state, self.package)
            for mode in ('recollection', 'hypothetical', 'background'):
                remembered = [{**events[0], 'mode': mode}]
                ra.validate_events({'eventChecks': checked(body)}, remembered, self.plan, state, self.package)
        ra.validate_events({'eventChecks': checked(body)}, events, self.plan, self.state, self.package)

    def test_derived_actor_is_guarded_without_treating_unknown_as_dead(self):
        state = copy.deepcopy(self.state)
        state['derivedCharacters'] = [dict(id='character_visitor', name='来客', summary='刚遇见的旅人')]
        candidate = copy.deepcopy(self.plan)
        candidate['steps'].append(dict(id='S2', actorId='character_visitor', action='回答你的问题',
                                       requirementIds=['A1'], authority='reaction', causeStepId='S1'))
        context = {**self.context, 'parent': {**self.context['parent'], 'branchState': state}}
        ra.validate_plan(candidate, context)
        state['characterOutcomeStates']['character_visitor'] = dict(status='departed', permanence='permanent')
        with self.assertRaisesRegex(ValueError, '永久下线'):
            ra.validate_plan(candidate, context)
        body = '来客说，让你继续往前走。'
        events = observed(body)['events']
        events[0].update(actor='来客', mode='speech')
        with self.assertRaisesRegex(ValueError, '永久下线'):
            ra.validate_events({'eventChecks': checked(body)}, events, self.plan, state, self.package)

    def test_general_changes_cannot_shadow_specialized_outcomes_or_goals(self):
        for attr in ('outcome', 'goal'):
            with self.subTest(attribute=attr):
                self.plan['stateChanges'] = []
                self.change(LU, attr, 'alive' if attr == 'outcome' else '重新承担旧任务')
                with self.assertRaisesRegex(ValueError, '保留字段'):
                    ra.validate_plan(self.plan, self.context)

    def test_dynamic_attribute_persists_and_releases_without_event_type_catalog(self):
        self.change(GU, '向同行者作出的约定', '今夜守在门外')
        ra.validate_plan(self.plan, self.context)
        next_state = ra.project(self.state, self.plan, self.package)
        self.assertEqual(next_state['readerEntityStates'][GU]['向同行者作出的约定'], '今夜守在门外')
        self.assertNotIn('readerEntityStates', self.state)
        self.plan['stateChanges'][0].update(before='今夜守在门外', value=None)
        context = {**self.context, 'parent': {**self.context['parent'], 'branchState': next_state}}
        ra.validate_plan(self.plan, context)
        released = ra.project(next_state, self.plan, self.package)
        self.assertNotIn('向同行者作出的约定', released['readerEntityStates'][GU])
        with self.assertRaisesRegex(ValueError, 'before'):
            ra.validate_plan(self.plan, self.context)

    def test_structural_transfer_and_put_down_keep_single_location(self):
        item = next(i for i, owner in self.state['itemOwnerCharacterIds'].items() if owner == GU)
        self.change(item, 'ownerCharacterId', LU, GU)
        ra.validate_plan(self.plan, self.context)
        transferred = ra.project(self.state, self.plan, self.package)
        self.assertEqual(transferred['itemOwnerCharacterIds'][item], LU)
        self.assertNotIn(item, transferred['itemLocationIds'])
        self.plan['stateChanges'] = []
        self.change(item, 'locationId', self.state['playerLocationId'])
        dropped = ra.project(transferred, self.plan, self.package)
        self.assertNotIn(item, dropped['itemOwnerCharacterIds'])
        self.assertEqual(dropped['itemLocationIds'][item], self.state['playerLocationId'])
        self.change(item, 'ownerCharacterId', LU, GU)
        with self.assertRaisesRegex(ValueError, '同时'):
            ra.validate_plan(self.plan, self.context)

    def test_character_movement_updates_both_player_and_character_location(self):
        destination = next(p['id'] for p in self.package['locations'] if p['id'] != self.state['playerLocationId'])
        self.change(GU, 'locationId', destination, self.state['characterLocationIds'][GU])
        ra.validate_plan(self.plan, self.context)
        moved = ra.project(self.state, self.plan, self.package)
        self.assertEqual(moved['playerLocationId'], destination)
        self.assertEqual(moved['characterLocationIds'][GU], destination)

    def test_authorized_new_entity_has_source_and_can_be_used_next_turn(self):
        self.plan['introductions']['items'] = [dict(id='item_folded_marker', name='折纸标记', summary='用随身纸张折成的标记', sourceStepId='S1')]
        self.change('item_folded_marker', 'ownerCharacterId', GU)
        ra.validate_plan(self.plan, self.context)
        changed = ra.project(self.state, self.plan, self.package)
        self.assertIn('item_folded_marker', ra.registry(self.package, changed))
        self.assertEqual(changed['itemOwnerCharacterIds']['item_folded_marker'], GU)
        context = {**self.context, 'parent': {**self.context['parent'], 'branchState': changed}}
        self.assertIn('item_folded_marker', str(rc.planning_context(context)['items']))
        self.plan['introductions']['items'][0]['sourceStepId'] = 'missing'
        with self.assertRaisesRegex(ValueError, '步骤'):
            ra.validate_plan(self.plan, self.context)

    def test_unknown_rope_or_unregistered_restraint_cannot_pass_positive_review(self):
        body = '一条细绳缚住你的手腕，双手无法分开。'
        events = observed(body)['events']
        events[0]['introduced'] = ['细绳']
        with self.assertRaisesRegex(ValueError, '关键实体'):
            ra.validate_events({'eventChecks': checked(body)}, events, self.plan)
        events[0].update(introduced=[], changes=[dict(entityId=GU, attribute='双手活动', value='无法分开')])
        with self.assertRaisesRegex(ValueError, '持续状态'):
            ra.validate_events({'eventChecks': checked(body)}, events, self.plan)

    def test_wrong_recipient_and_wrong_outcome_cannot_use_unrelated_evidence(self):
        item = next(iter(self.state['itemOwnerCharacterIds']))
        self.change(item, 'ownerCharacterId', LU, GU)
        body = '你把随身物品交给了眼前的人。'
        events, checks = observed(body)['events'], checked(body)
        checks[0]['changeIds'] = ['C1']
        events[0]['changes'] = [dict(entityId=item, attribute='ownerCharacterId', value=GU)]
        with self.assertRaisesRegex(ValueError, '持续状态'):
            ra.validate_events({'eventChecks': checks}, events, self.plan)
        events[0]['changes'][0]['value'] = LU
        ra.validate_events({'eventChecks': checks}, events, self.plan)
        self.plan['outcomes'] = [dict(characterId=LU, status='departed')]
        events[0]['changes'] = [dict(entityId=LU, attribute='outcome', value='dead')]
        with self.assertRaises(ValueError):
            ra.validate_events({'eventChecks': checks}, events, self.plan)

    def test_inherited_mentions_are_allowed_but_later_reversal_is_not(self):
        item = next(i for i, owner in self.state['itemOwnerCharacterIds'].items() if owner == GU)
        self.change(item, 'ownerCharacterId', LU, GU)
        body = '你拿着纸包。\n\n你把纸包递给他，他接下。\n\n纸包又回到你手里。'
        events, checks = observed(body)['events'], checked(body)
        for event, check, owner in zip(events, checks, (GU, LU, GU)):
            event['changes'] = [dict(entityId=item, attribute='ownerCharacterId', value=owner)]
            check['changeIds'] = ['C1']
        ra.validate_events({'eventChecks': checks[:2]}, events[:2], self.plan, self.state, self.package)
        with self.assertRaisesRegex(ValueError, '未登记'):
            ra.validate_events({'eventChecks': checks}, events, self.plan, self.state, self.package)

    def test_no_change_does_not_pollute_dynamic_state(self):
        self.change(GU, '位置', self.state['playerLocationId'], self.state['playerLocationId'])
        result = rc.validate_plan(self.plan, {'A1': '任意行动'}, self.context)
        self.assertEqual(result['stateChanges'], [])
        self.assertEqual(len(self.plan['stateChanges']), 1)

    def test_observer_requires_every_paragraph_and_literal_evidence(self):
        body = '你轻声发问。\n\n他只要求你在此等候。'
        data = observed(body)
        data['events'][0]['quote'] = '模型错误改写的引文'
        self.assertEqual(ra.validate_observations(data, body)[0]['quote'], body.split('\n\n')[0])
        with self.assertRaisesRegex(ValueError, '覆盖'):
            ra.validate_observations({'events': data['events'][:1]}, body)
        data['events'][1]['paragraphId'] = 'P99'
        with self.assertRaisesRegex(ValueError, '有效段落'):
            ra.validate_observations(data, body)
        with self.assertRaisesRegex(ValueError, '覆盖'):
            ra.validate_events({'eventChecks': []}, observed(body)['events'], self.plan)

    def test_action_authority_and_invalid_ids_fail_as_validation_errors(self):
        for update in (dict(actorId=LU), dict(actorId=[]), dict(requirementIds=[[]]),
                       dict(authority='reaction', causeStepId='S9')):
            bad = copy.deepcopy(self.plan)
            bad['steps'][0].update(update)
            with self.assertRaises(ValueError):
                ra.validate_plan(bad, self.context)
        self.change([], '姿态', '坐下')
        with self.assertRaises(ValueError):
            ra.validate_plan(self.plan, self.context)

    def test_extraction_format_repair_does_not_regenerate_valid_body(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        body = '你站在原地等候，没有迈出下一步。'
        invalid = observed(body)
        invalid['events'][0]['paragraphId'] = 'P99'
        review = dict(sceneChecks=scene_checked(body), issues=[], actions=[dict(id='A1', status='performed', summary='你留在原地等候', paragraphId='P1')],
                      finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                      outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[], eventChecks=checked(body))
        gateway = Mock(model='fixture')
        gateway.complete_text.return_value = Completion(body, '{}', [])
        gateway.complete_json.side_effect = [Completion(json.dumps(x, ensure_ascii=False), '{}', []) for x in (self.plan, authority(self.plan), invalid, observed(body), review, grounded(body))]
        context = {**self.context, 'characterDetails': []}
        selected = dict(id='custom', title=context['playerDirection'], summary=context['playerDirection'], isFreeText=True, statePatch={'freeTextProgress': 1})
        result, _ = PlayerNarrativePlanner(gateway).plan(context, selected, self.state)
        self.assertEqual(result['narrativeText'], body)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 6)
        self.assertEqual(result['sceneChecks'], scene_checked(body))
        review_input = json.loads(gateway.complete_json.call_args.args[0][1]['content'])
        self.assertIn('sceneEvidence', review_input)
        self.assertNotIn('unknownBoundaries', str(review_input['sceneEvidence']))

    def test_extraction_timeout_attaches_readable_body_without_acceptance(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion, LlmError
        body = '你站在原地等候，没有迈出下一步。'
        gateway = Mock(model='fixture', complete_text=Mock(return_value=Completion(body, '{}', [])))
        gateway.complete_json.side_effect = [Completion(json.dumps(x), '{}', []) for x in
                                             (self.plan, authority(self.plan))] + [LlmError('timeout', 'provider_timeout')]
        selected = dict(id='custom', title='原地等候', summary='原地等候', isFreeText=True)
        with self.assertRaises(LlmError) as error:
            PlayerNarrativePlanner(gateway).plan({**self.context, 'characterDetails': []}, selected, self.state)
        self.assertEqual(error.exception.retained_body, body)
        self.assertEqual(error.exception.audit['retainedDraft'], {'text': body, 'status': 'unconfirmed'})

    def test_authority_quote_repair_keeps_plan_and_never_masks_semantic_rejection(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        action = self.context['playerDirection']
        good = authority(self.plan)
        bad = copy.deepcopy(good)
        bad['checks'][0]['quote'] = '这句只在规划中出现'
        gateway = Mock(model='fixture')
        gateway.complete_json.side_effect = [Completion(json.dumps(x), '{}', []) for x in (bad, good)]
        result = PlayerNarrativePlanner(gateway)._check_action_authority(self.context, action, self.plan, [], [])
        self.assertEqual(result, good)
        self.assertEqual(gateway.complete_json.call_count, 2)
        bad['checks'][0].update(authorized=False, reason='未授权承诺')
        with self.assertRaises(ValueError) as caught:
            ra.validate_authority(bad, self.plan, action, '')
        self.assertNotIsInstance(caught.exception, ra.ActionEvidenceError)

    def test_action_authority_allows_observation_inference_without_state_patch(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        action = '我停在石台边缘，不往里走，观察石台、脚印和道路；陆照临也明确表示不知道石台之外是否有人。'
        contract = copy.deepcopy(self.plan)
        contract['requirements'] = {'A1': {'mode': 'result', 'summary': action}}
        contract['steps'][0].update(action=action, requirementIds=['A1'])
        contract['scenePlan']['observationLimits'] = ['雨幕限制远处视线，未发现不等于绝对无人。']
        contract['scenePlan']['knowledge'] = [
            dict(speakerId=GU, status='inference',
                 statement='当前观察范围内未发现明确人迹，路径只辨认出石径来路。',
                 sources=[dict(id='history-branch_31f0efdf-fb83-4f24-b04b-b3aa52d1ab04-P2',
                               quote='这里是玄霄宗半山的试炼场，石壁上留着旧剑痕。')]),
            dict(speakerId=LU, status='unknown', statement='陆照临不知道石台之外是否有人。', sources=[]),
        ]
        review = authority(contract)
        review['premiseChecks'] = [
            dict(id='K1', kind='after_step', verdict='supported', sources=[], stepIds=['S1'],
                 missingEvidence=[], reason='当前观察步骤产生的暂时推断'),
            dict(id='K2', kind='unknown', verdict='supported', sources=[], stepIds=[],
                 missingEvidence=[], reason='NPC明确保留不知道'),
            dict(id='O1', kind='restriction', verdict='supported', sources=[], stepIds=[],
                 missingEvidence=[], reason='只限制远处观察范围'),
        ]
        gateway = Mock(model='fixture')
        gateway.complete_json.return_value = Completion(json.dumps(review, ensure_ascii=False), '{}', [])
        result = PlayerNarrativePlanner(gateway)._check_action_authority(
            self.context, action, contract, [], [])
        self.assertEqual(result, review)
        self.assertEqual(contract['stateChanges'], [])
        self.assertNotIn('locationId', str(contract['stateChanges']))

    def test_missing_scene_plan_rejected_before_body_generation(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion, LlmError
        contract = copy.deepcopy(self.plan)
        del contract['scenePlan']
        gateway = Mock(model='fixture')
        gateway.complete_json.return_value = Completion(json.dumps(contract), '{}', [])
        selected = dict(id='custom', title='等候', summary='等候', isFreeText=True)
        with self.assertRaises(LlmError) as error:
            PlayerNarrativePlanner(gateway).plan({**self.context, 'characterDetails': []}, selected, self.state)
        self.assertIn('scenePlan', str(error.exception))
        self.assertEqual(gateway.complete_json.call_count, 2)
        gateway.complete_text.assert_not_called()

    def test_unsupported_plan_observation_cannot_reach_prose(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion, LlmError
        contract = copy.deepcopy(self.plan)
        contract['scenePlan']['observationLimits'] = ['能看见包内细节']
        review = authority(contract)
        review['premiseChecks'] = [dict(id='O1', kind='existing', verdict='supported', sources=[],
                                       stepIds=[], missingEvidence=['纸包未打开'], reason='观察前提缺失')]
        gateway = Mock(model='fixture')
        gateway.complete_json.side_effect = [Completion(json.dumps(x), '{}', []) for x in (contract, review, contract, review)]
        selected = dict(id='custom', title='等候', summary='等候', isFreeText=True)
        with self.assertRaisesRegex(LlmError, '观察前提缺失'):
            PlayerNarrativePlanner(gateway).plan({**self.context, 'characterDetails': []}, selected, self.state)
        gateway.complete_text.assert_not_called()
        payload = json.loads(gateway.complete_json.call_args_list[1].args[0][1]['content'])
        self.assertIn('O1', payload['premises'])
        self.assertIn('sceneEvidence', payload)
        self.assertEqual(gateway.complete_json.call_count, 4)

    def test_error_repairs_scene_without_discarding_good_detail(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        contract = {**self.plan, 'readingIntent': 'normal'}
        body = '你留在原地，静静听着对方的回应。\n\n对方的话语传到耳边，你听清了每一个字。\n\n他曾见过古碑。\n\n你没有迈步，也没有继续提出新的问题。'
        corrected = body.replace('他曾见过古碑。', '他说自己还不知道。')
        def review(text):
            return dict(sceneChecks=scene_checked(text), issues=[], actions=[dict(id='A1', status='performed', summary='原地等候', paragraphId='P1')],
                        finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                        outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[], eventChecks=checked(text))
        bad_grounding = grounded(body)
        bad_grounding['checks'][2].update(verdict='unsupported', reason='虚构经历')
        gateway = Mock(model='fixture', complete_text=Mock(return_value=Completion(body, '{}', [])))
        gateway.complete_json.side_effect = [Completion(json.dumps(x), '{}', []) for x in
            (contract, authority(contract), observed(body), review(body), bad_grounding,
             {'replacements': [dict(paragraphId='P3', text='他说自己还不知道。')]},
             observed(corrected), review(corrected), grounded(corrected, repair_count=1))]
        selected = dict(id='custom', title='原地等候', summary='原地等候', isFreeText=True)
        result, audit = PlayerNarrativePlanner(gateway).plan({**self.context, 'characterDetails': []}, selected, self.state)
        self.assertEqual(result['narrativeText'], corrected)
        self.assertFalse(result['readingProfile']['expansionApplied'])
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 9)
        self.assertFalse(any(o['generationStage'] == 'scene_expand' for o in audit['callObservations']))

    def test_complete_short_scene_does_not_trigger_expansion_or_rejection(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        contract = copy.deepcopy(self.plan)
        contract['scenePlan']['targetCjk'] = [700, 1100]
        body = '你站在原地认真听完对方说出的每一句话，没有离开。\n\n你没有继续提问，仍然等待着。'
        review = dict(sceneChecks=scene_checked(body), issues=[], actions=[dict(id='A1', status='performed', summary='等候', paragraphId='P1')],
                      finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                      outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[], eventChecks=checked(body))
        gateway = Mock(model='fixture', complete_text=Mock(return_value=Completion(body, '{}', [])))
        gateway.complete_json.side_effect = [Completion(json.dumps(x), '{}', []) for x in
                                            (contract, authority(contract), observed(body), review, grounded(body))]
        selected = dict(id='custom', title='等候', summary='等候', isFreeText=True)
        result, audit = PlayerNarrativePlanner(gateway).plan({**self.context, 'characterDetails': []}, selected, self.state)
        self.assertEqual(result['narrativeText'], body)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 5)
        self.assertFalse(result['readingProfile']['expansionApplied'])
        self.assertEqual([o['revision'] for o in audit['callObservations'] if o['generationStage'] == 'scene_draft_metrics'], [0])
        self.assertFalse(any(o['generationStage'] == 'scene_expand' for o in audit['callObservations']))

    def test_grounding_reference_repair_keeps_prose_and_is_bounded(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion, LlmError
        body = '你站在原地，认真听完对方说出的每一句话，没有继续提问。'
        review = dict(sceneChecks=scene_checked(body), issues=[], actions=[dict(id='A1', status='performed', summary='等候', paragraphId='P1')],
                      finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                      outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[], eventChecks=checked(body))
        invalid = grounded(body)
        invalid['checks'][0]['sources'] = [dict(id='missing', quote='没有这条来源')]
        selected = dict(id='custom', title='等候', summary='等候', isFreeText=True)
        for repaired in (grounded(body), invalid):
            with self.subTest(valid=repaired is not invalid):
                gateway = Mock(model='fixture', complete_text=Mock(return_value=Completion(body, '{}', [])))
                gateway.complete_json.side_effect = [Completion(json.dumps(x), '{}', []) for x in
                    (self.plan, authority(self.plan), observed(body), review, invalid, repaired)]
                planner = PlayerNarrativePlanner(gateway)
                if repaired is invalid:
                    with self.assertRaises(LlmError) as caught:
                        planner.plan({**self.context, 'characterDetails': []}, selected, self.state)
                    self.assertEqual(caught.exception.retained_body, body)
                    audit = caught.exception.audit
                else:
                    result, audit = planner.plan({**self.context, 'characterDetails': []}, selected, self.state)
                    self.assertEqual(result['narrativeText'], body)
                self.assertEqual(gateway.complete_text.call_count, 1)
                self.assertEqual(gateway.complete_json.call_count, 6)
                self.assertFalse(any(o['generationStage'] == 'local_repair_record' for o in audit['callObservations']))

    def test_premise_reference_repair_does_not_replan_or_relax_knowledge(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        contract = copy.deepcopy(self.plan)
        contract['scenePlan']['knowledge'] = [dict(status='pending', afterStepId='S1', statement='听完才知道')]
        good = authority(contract)
        good['premiseChecks'] = [dict(id='K1', kind='after_step', verdict='supported', sources=[],
                                     stepIds=['S1'], missingEvidence=[], reason='实际告知之后')]
        bad = copy.deepcopy(good)
        bad['premiseChecks'][0]['sources'] = [dict(id='S1', quote='未发生的步骤')]
        gateway = Mock(model='fixture')
        gateway.complete_json.side_effect = [Completion(json.dumps(x), '{}', []) for x in (bad, good)]
        result = PlayerNarrativePlanner(gateway)._check_action_authority(self.context, self.context['playerDirection'], contract, [], [])
        self.assertEqual(result, good)
        self.assertEqual(gateway.complete_json.call_count, 2)
        gateway.complete_text.assert_not_called()

    def test_semantic_issues_repair_together_then_run_full_review(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        body = '你留在原地等候，没有离开当前的位置。\n\n你答应同行。\n\n他曾见过古碑。\n\n风掠过场边，他仍站着，没有带你前往别处。'
        corrected = body.replace('你答应同行。', '你仍未决定。').replace('他曾见过古碑。', '他没有提供依据。')
        def review(text):
            return dict(sceneChecks=scene_checked(text), issues=[], actions=[dict(id='A1', status='performed', summary='原地等候', paragraphId='P1')],
                        finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                        outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[], eventChecks=checked(text))
        bad = review(body)
        bad['issues'] = [dict(paragraphId='P2', type='action', reason='未授权承诺')]
        bad['sceneChecks'][2].update(background='unsupported', issue='经历缺证')
        repair = {'replacements': [dict(paragraphId='P2', text='你仍未决定。'), dict(paragraphId='P3', text='他没有提供依据。')]}
        gateway = Mock(model='fixture', complete_text=Mock(return_value=Completion(body, '{}', [])))
        gateway.complete_json.side_effect = [Completion(json.dumps(x, ensure_ascii=False), '{}', [])
            for x in (self.plan, authority(self.plan), observed(body), bad, grounded(body), repair, observed(corrected), review(corrected), grounded(corrected, repair_count=2))]
        context = {**self.context, 'characterDetails': []}
        selected = dict(id='custom', title=context['playerDirection'], summary=context['playerDirection'], isFreeText=True, statePatch={'freeTextProgress': 1})
        result, audit = PlayerNarrativePlanner(gateway).plan(context, selected, self.state)
        payload = json.loads(gateway.complete_json.call_args_list[5].args[0][1]['content'])
        self.assertEqual({v['paragraphId'] for v in payload['problem']['issues']}, {'P2', 'P3'})
        self.assertNotIn('他曾见过古碑', str(payload['paragraphs']))
        self.assertEqual(result['narrativeText'], corrected)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 9)
        record = next(o for o in audit['callObservations'] if o['generationStage'] == 'local_repair_record')
        self.assertEqual((record['beforeBody'], record['afterBody'], record['outcome']), (body, corrected, 'passed_full_review'))
        for index in (7, 8):
            recheck = json.loads(gateway.complete_json.call_args_list[index].args[0][1]['content'])
            self.assertEqual(recheck['priorRepairIssues'], record['issues'])
            self.assertNotIn('beforeBody', recheck)
        self.assertIn('originalParagraphCjk', payload)

    def test_long_problem_paragraph_is_repaired_with_complete_issue_set(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        # The rejected paragraph is deliberately longer than half of the body.
        # Its replacement is small and valid; the old pre-check skipped the
        # repair before the model could receive the complete issue set.
        long_paragraph = ('守门弟子仍按着剑柄站在原地，目光停在门缝和你手边的灯上，'
                          '他没有迈步，也没有碰触任何物件，只是听你把话说完。') * 3
        body = f'你留在原地等候，没有离开当前的位置。\n\n{long_paragraph}\n\n你仍在原地，等待他当场回应。'
        corrected = body.replace(long_paragraph, '守门弟子听完，只说：“这些事我现在不知道。”')

        def review(text):
            return dict(sceneChecks=scene_checked(text), issues=[],
                        actions=[dict(id='A1', status='performed', summary='原地等候', paragraphId='P1')],
                        finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                        outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[],
                        eventChecks=checked(text))

        bad = review(body)
        bad['issues'] = [
            dict(paragraphId='P2', type='background', reason='缺少来源的既往状态'),
            dict(paragraphId='P2', type='continuity', reason='把未知信息说成确定事实'),
            dict(paragraphId='P2', type='state', reason='把当前未确认状态写成已确认'),
        ]
        repair = {'replacements': [dict(paragraphId='P2', text='守门弟子听完，只说：“这些事我现在不知道。”')]}
        gateway = Mock(model='fixture', complete_text=Mock(return_value=Completion(body, '{}', [])))
        gateway.complete_json.side_effect = [Completion(json.dumps(x, ensure_ascii=False), '{}', []) for x in
            (self.plan, authority(self.plan), observed(body), bad, grounded(body), repair,
             observed(corrected), review(corrected), grounded(corrected, repair_count=3))]
        context = {**self.context, 'characterDetails': []}
        selected = dict(id='custom', title=context['playerDirection'], summary=context['playerDirection'],
                        isFreeText=True, statePatch={'freeTextProgress': 1})
        result, audit = PlayerNarrativePlanner(gateway).plan(context, selected, self.state)
        repair_call = gateway.complete_json.call_args_list[5]
        payload = json.loads(repair_call.args[0][1]['content'])
        record = next(o for o in audit['callObservations'] if o['generationStage'] == 'local_repair_record')
        self.assertEqual(record['repairInput']['issueCount'], 3)
        self.assertEqual(record['repairInput']['issueParagraphIds'], ['P2'])
        self.assertEqual(record['repairInput']['inputCharacters'], len(''.join(body.split())))
        self.assertEqual({v['paragraphId'] for v in payload['problem']['issues']}, {'P2'})
        self.assertEqual(result['narrativeText'], corrected)
        self.assertEqual(record['outcome'], 'passed_full_review')
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 9)

    def test_independent_scope_review_repairs_violation_missed_by_first_review(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        action = '让他只看纸包'
        contract = plan({'A1': action})
        body = '你留在原地，请他只看手中的纸包，不把东西递过去。\n\n他看向木牌。\n\n你耐心等候，仍未决定下一步要怎么做。'
        corrected = body.replace('他看向木牌。', '他只看纸包。')
        def review(text):
            return dict(sceneChecks=scene_checked(text), issues=[], actions=[dict(id='A1', status='performed', summary='已按请求观察', paragraphId='P2')],
                        finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                        outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[], eventChecks=checked(text))
        missed = grounded(body)
        focused = {'scopeChecks': [dict(id='A1', verdict='satisfied', paragraphIds=['P1'],
                    counterexamples=[dict(paragraphId='P2', quote='他看向木牌')], reason='只看纸包却转看木牌')]}
        focused_fixed = {'scopeChecks': [dict(id='A1', verdict='satisfied', paragraphIds=['P2'],
                                             counterexamples=[], reason='只观察指定纸包')]}
        gateway = Mock(model='fixture', complete_text=Mock(return_value=Completion(body, '{}', [])))
        gateway.complete_json.side_effect = [Completion(json.dumps(x), '{}', []) for x in
            (contract, authority(contract), observed(body), review(body), missed, focused,
             {'replacements': [dict(paragraphId='P2', text='他只看纸包。')]}, observed(corrected), review(corrected), grounded(corrected, repair_count=1), focused_fixed)]
        context = {**self.context, 'playerDirection': action, 'characterDetails': []}
        selected = dict(id='custom', title=action, summary=action, isFreeText=True)
        result, audit = PlayerNarrativePlanner(gateway).plan(context, selected, self.state)
        self.assertEqual(result['narrativeText'], corrected)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 11)
        payload = json.loads(gateway.complete_json.call_args_list[4].args[0][1]['content'])
        self.assertEqual(payload['requirements'], {'A1': action})
        self.assertNotIn('resultContract', payload)
        self.assertNotIn('scenePlan', payload)
        self.assertEqual(len([o for o in audit['callObservations'] if o['generationStage'] == 'independent_review_result']), 2)

    def test_short_scene_overreach_still_requires_repair_and_full_review(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        body = '你留在原地等候，脚步没有移动。\n\n他没有开口，只抬眼看着你。\n\n你又追问他的身世。\n\n你仍在原地等着他的回应。'
        corrected = body.replace('你又追问他的身世。', '你没有继续提问。')
        def review(text):
            return dict(sceneChecks=scene_checked(text), issues=[],
                        actions=[dict(id='A1', status='performed', summary='原地等候', paragraphId='P1')],
                        finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                        outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[], eventChecks=checked(text))
        bad = review(body)
        bad['sceneChecks'][2].update(playerDecision='overreach', issue='未经授权追加追问')
        gateway = Mock(model='fixture', complete_text=Mock(return_value=Completion(body, '{}', [])))
        gateway.complete_json.side_effect = [Completion(json.dumps(x, ensure_ascii=False), '{}', []) for x in
            (self.plan, authority(self.plan), observed(body), bad, grounded(body),
             {'replacements': [dict(paragraphId='P3', text='你没有继续提问。')]},
             observed(corrected), review(corrected), grounded(corrected, repair_count=1))]
        selected = dict(id='custom', title='原地等候', summary='原地等候', isFreeText=True)
        result, audit = PlayerNarrativePlanner(gateway).plan({**self.context, 'characterDetails': []}, selected, self.state)
        self.assertEqual(result['narrativeText'], corrected)
        self.assertFalse(result['readingProfile']['expansionApplied'])
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 9)
        self.assertFalse(any(o['generationStage'] == 'scene_expand' for o in audit['callObservations']))

    def test_unlocated_rejection_uses_bounded_fresh_draft_not_partial_repair(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        body, corrected = '你留在原地，答应替他跑一趟。', '你留在原地，没有答应任何新安排。'
        good = dict(sceneChecks=scene_checked(corrected), issues=[], actions=[dict(id='A1', status='performed', summary='原地等候', paragraphId='P1')],
                    finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                    outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[], eventChecks=checked(corrected))
        gateway = Mock(model='fixture')
        gateway.complete_text.side_effect = [Completion(x, '{}', []) for x in (body, corrected)]
        gateway.complete_json.side_effect = [Completion(json.dumps(x, ensure_ascii=False), '{}', [])
            for x in (self.plan, authority(self.plan), observed(body), {'issues': ['旧格式：未授权承诺']}, grounded(body), observed(corrected), good, grounded(corrected, repair_count=0))]
        context = {**self.context, 'characterDetails': []}
        selected = dict(id='custom', title=context['playerDirection'], summary=context['playerDirection'], isFreeText=True, statePatch={'freeTextProgress': 1})
        result, audit = PlayerNarrativePlanner(gateway).plan(context, selected, self.state)
        self.assertEqual(result['narrativeText'], corrected)
        self.assertIn('旧格式：未授权承诺', str(gateway.complete_text.call_args))
        self.assertTrue(any(o.get('reason') == 'unlocated_semantic_issues' for o in audit['callObservations']))
        self.assertEqual(gateway.complete_json.call_count, 8)

    def test_review_timeout_retains_body_without_committing_it(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion, LlmError
        body = '你站在原地等候，没有迈出下一步。'
        failure = LlmError('timeout', 'provider_timeout')
        failure.observations = [{'attempt': 1, 'outcome': 'failed', 'error': 'timeout'}]
        gateway = Mock(model='fixture', complete_text=Mock(return_value=Completion(body, '{}', [])))
        gateway.complete_json.side_effect = [Completion(json.dumps(x), '{}', []) for x in
                                            (self.plan, authority(self.plan), observed(body))] + [failure]
        selected = dict(id='custom', title='原地等候', summary='原地等候', isFreeText=True)
        with self.assertRaises(LlmError) as error:
            PlayerNarrativePlanner(gateway).plan({**self.context, 'characterDetails': []}, selected, self.state)
        self.assertEqual(error.exception.retained_body, body)
        self.assertEqual(error.exception.audit['retainedDraft']['status'], 'unconfirmed')
        self.assertEqual(gateway.complete_text.call_count, 1)

    def test_missing_paragraph_repair_keeps_valid_events_and_only_requests_gap(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        body = '你留在原地等候。\n\n他抬眼望着你，没有说话。\n\n你仍没有迈出下一步。'
        incomplete = observed(body)
        missing = incomplete['events'].pop(1)
        missing['id'] = 'O1'  # IDs in the supplemental response may restart.
        review = dict(sceneChecks=scene_checked(body), issues=[], actions=[dict(id='A1', status='performed', summary='你留在原地等候', paragraphId='P1')],
                      finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                      outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[], eventChecks=checked(body))
        gateway = Mock(model='fixture')
        gateway.complete_text.return_value = Completion(body, '{}', [])
        gateway.complete_json.side_effect = [Completion(json.dumps(x, ensure_ascii=False), '{}', [])
            for x in (self.plan, authority(self.plan), incomplete, {'events': [missing]}, review, grounded(body))]
        context = {**self.context, 'characterDetails': []}
        selected = dict(id='custom', title=context['playerDirection'], summary=context['playerDirection'], isFreeText=True, statePatch={'freeTextProgress': 1})
        result, audit = PlayerNarrativePlanner(gateway).plan(context, selected, self.state)
        self.assertEqual(result['narrativeText'], body)
        self.assertEqual(gateway.complete_text.call_count, 1)
        repair = json.loads(gateway.complete_json.call_args_list[3].args[0][1]['content'])
        self.assertEqual(repair['requiredParagraphIds'], ['P2'])
        self.assertEqual(list(repair['draft']), ['P2'])
        self.assertEqual(repair['viewpoint'], {'id': GU, 'name': '顾长离'})
        self.assertTrue(all(set(e) <= {'id', 'name', 'kind', 'aliases'} for e in repair['registry'].values()))
        self.assertEqual([e['paragraphId'] for e in result['observedEvents']], ['P1', 'P2', 'P3'])
        self.assertEqual(len({e['id'] for e in result['observedEvents']}), 3)
        self.assertTrue(any(o['generationStage'] == 'observation_gap_repair' for o in audit['callObservations']))

    def test_gap_repair_cannot_overwrite_existing_paragraph_or_hide_bad_change(self):
        body = '你留在原地。\n\n他独自离开。'
        first = observed(body)['events'][:1]
        with self.assertRaises(ra.ObservationCoverageError) as error:
            ra.validate_observations({'events': first}, body)
        self.assertEqual(error.exception.missing, ['P2'])
        with self.assertRaises(ValueError):
            ra.validate_observations({'events': first}, body, ['P2'])
        malformed = observed(body)
        malformed['events'][0]['changes'] = [{'entityId': GU, 'attribute': '状态', 'value': {'invalid': True}}]
        malformed['events'].pop()
        with self.assertRaises(ValueError) as error:
            ra.validate_observations(malformed, body)
        self.assertNotIsInstance(error.exception, ra.ObservationCoverageError)

    def test_authority_must_cover_states_and_denial_cannot_be_overridden_by_allow(self):
        self.change(GU, '姿态', '伏地')
        data = authority(self.plan)
        data['stateChecks'][0].update(authorized=False, reason='用户只是发问，未选择改变姿态')
        with self.assertRaisesRegex(ValueError, '越权'):
            ra.validate_authority(data, self.plan, self.context['playerDirection'], self.context['parent']['narrativeText'])
        data['stateChecks'] = []
        with self.assertRaisesRegex(ValueError, '覆盖'):
            ra.validate_authority(data, self.plan, self.context['playerDirection'], '')

    def test_legitimate_observed_consequence_gets_authorized_and_persisted(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import Completion
        action = '询问陆照临是否愿意在原地等候我'
        contract = plan({'A1': action})
        body = '你问陆照临是否愿意等候。他点头答应，承诺在原地等你。'
        delta = dict(id='C1', entityId=LU, attribute='承诺', before=None, value='在原地等候你', stepId='S1', reason='因玩家询问而答应等候')
        candidate = {**contract, 'stateChanges': [delta]}
        events, checks = observed(body), checked(body)
        events['events'][0]['changes'] = [dict(entityId=LU, attribute='承诺', value='在原地等候你')]
        checks[0]['changeIds'] = ['C1']
        review = dict(sceneChecks=scene_checked(body), issues=[], actions=[dict(id='A1', status='performed', summary='陆照临答应等候', paragraphId='P1')],
                      finalState=rc.final_state_projection(self.state), checkedConsequences=True,
                      outcomeEvidence=[], goalEvidence=[], changeEvidence=['P1'], introductionEvidence=[],
                      eventChecks=checks, additionalChanges=[delta])
        gateway = Mock(model='fixture')
        gateway.complete_text.return_value = Completion(body, '{}', [])
        gateway.complete_json.side_effect = [Completion(json.dumps(x, ensure_ascii=False), '{}', []) for x in (contract, authority(contract), events, review, grounded(body), authority(candidate))]
        context = {**self.context, 'playerDirection': action, 'characterDetails': []}
        selected = dict(id='custom', title=action, summary=action, isFreeText=True, statePatch={'freeTextProgress': 1})
        result, _ = PlayerNarrativePlanner(gateway).plan(context, selected, self.state)
        changed = rc.commit_consequences(context, self.state, result, 'observed')
        self.assertEqual(changed['readerEntityStates'][LU]['承诺'], '在原地等候你')
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(result['authorityReview']['stateChecks'][0]['changeId'], 'C1')

    def test_general_property_survives_five_turns_restart_and_sibling_isolation(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.api_play import PlayService
        from open_story_engine.api_read import ReadService
        from open_story_engine.llm import Completion
        from open_story_engine.api_reader_quality import action_requirements
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
            read = ReadService(fixtures.ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
            play = PlayService(read, Path(tmp))
            self.addCleanup(play.drafts.close)
            start = play.create_session('taixu-relics-part1', '0.1.3', 'entry_gu_trial', GU, identity_opening=True)
            sid, root = start['session']['id'], start['branch']
            responses, texts = [], []
            menus = []
            old_goal = rc.goals_for(self.package, self.context['contract'], root['branchState'])[0]
            def enqueue(action, changed):
                req = action_requirements(action)
                contract = plan(req)
                body = '你把木牌折成两截，两截都留在自己手里。你放下原来的目标，决定先寻找安全的落脚处。' if changed else '你留在原地等候。两截断牌仍握在手中，没有交给别人。'
                if changed:
                    contract['stateChanges'] = [dict(id='C1', entityId='item_open_gu_token', attribute='完整性', before=None, value='断成两截', stepId='S1', reason='玩家折断')]
                    contract['goalUpdates'] = [dict(id=old_goal['id'], title=old_goal['title'], status='transformed',
                                                     dependencies=[], reason='玩家决定改换目标', successor='寻找安全的落脚处')]
                events, checks = observed(body), checked(body)
                events['events'][0]['changes'] = [dict(entityId='item_open_gu_token', attribute='完整性', value='断成两截')]
                if changed: checks[0]['changeIds'] = ['C1']
                review = dict(sceneChecks=scene_checked(body), finalState=rc.final_state_projection(root['branchState']), issues=[],
                              actions=[dict(id=k, status='performed', summary='行动已完成', paragraphId='P1') for k in req],
                              checkedConsequences=True, outcomeEvidence=[], goalEvidence=['P1'] if changed else [],
                              changeEvidence=['P1'] if changed else [], introductionEvidence=[], eventChecks=checks)
                responses.extend(Completion(json.dumps(data, ensure_ascii=False), '{}', []) for data in (contract, authority(contract), events, review))
                texts.append(Completion(body, '{}', []))
            gateway = Mock(model='fixture')
            gateway.complete_text.side_effect = lambda *args: texts.pop(0)
            def complete_json(messages, *args):
                from open_story_engine.prompts import render_prompt
                if messages[0]['content'] == render_prompt('reader.scene_expand'):
                    return Completion('{"insertions":[]}', '{}', [])
                if messages[0]['content'] == render_prompt('reader.scene_grounding'):
                    data = json.loads(messages[1]['content'])
                    return Completion(json.dumps(grounded('\n\n'.join(data['draft'].values()), data['requirements'])), '{}', [])
                if messages[0]['content'] == render_prompt('reader.choices'):
                    menus.append(json.loads(messages[1]['content']))
                    menu = {'choices': [{'title': '留在原地等候', 'action': '我留在原地等候回应，暂时不移动或交出物品。',
                                         'paragraphIds': ['P1'], 'interactWith': []}]}
                    return Completion(json.dumps(menu, ensure_ascii=False), '{}', [])
                return responses.pop(0)
            gateway.complete_json.side_effect = complete_json
            gateway.__deepcopy__ = lambda memo: Mock(model='fixture', complete_text=gateway.complete_text, complete_json=gateway.complete_json)
            play._planner = PlayerNarrativePlanner(gateway)
            action = '折断木牌，放下原来的目标，改为寻找安全的落脚处'
            enqueue(action, True)
            current = play.continue_turn(sid, root['id'], text=action, request_id='break')['branch']
            for i in range(5):
                enqueue('留在原地等候', False)
                current = play.continue_turn(sid, current['id'], text='留在原地等候', request_id='follow-' + str(i))['branch']
                self.assertEqual(current['branchState']['readerEntityStates']['item_open_gu_token']['完整性'], '断成两截')
            play.drafts.close()
            restored = PlayService(read, Path(tmp))
            self.addCleanup(restored.drafts.close)
            with read.store() as store:
                saved = store.branch(sid, current['id'])
                original = store.branch(sid, root['id'])
            self.assertEqual(saved['branchState']['readerEntityStates']['item_open_gu_token']['完整性'], '断成两截')
            self.assertEqual(saved['branchState']['goalLedger'][0]['status'], 'transformed')
            self.assertEqual(saved['branchState']['goalLedger'][-1]['title'], '寻找安全的落脚处')
            self.assertEqual(saved['readerChoices'][0]['summary'], '我留在原地等候回应，暂时不移动或交出物品。')
            self.assertNotIn('readerEntityStates', original['branchState'])
            self.assertEqual(rc.goals_for(self.package, self.context['contract'], original['branchState'])[0]['status'], 'active')
            restored._planner = PlayerNarrativePlanner(gateway)
            enqueue('留在原地等候', False)
            resumed = restored.continue_turn(sid, saved['id'], text='留在原地等候', request_id='resumed')['branch']
            self.assertEqual(resumed['branchState']['goalLedger'], saved['branchState']['goalLedger'])
            self.assertEqual(len(menus), 7)
            for menu in menus:
                token = next(i for i in menu['items'] if i['id'] == 'item_open_gu_token')
                self.assertEqual(token['state'], {'完整性': '断成两截'})
                self.assertEqual(menu['goals'][0]['status'], 'transformed')
                self.assertEqual(menu['goals'][-1]['title'], '寻找安全的落脚处')
                self.assertEqual(menu['goals'][-1]['status'], 'active')
            # A sibling starts from the original state, not the changed route.
            enqueue(action, True)
            sibling = restored.continue_turn(sid, root['id'], text=action, request_id='sibling')['branch']
            self.assertNotEqual(sibling['id'], current['id'])
            self.assertIsNone(sibling['consequenceUpdate']['stateChanges'][0]['before'])

    def test_reviewed_prose_cannot_be_appended_after_review_or_patch_state_directly(self):
        from open_story_engine.cocreation import apply_branch_patch
        body = '你站在原地，问他为何拦路。'
        result = seal(dict(narrativeText=body, consequenceReview=rc.VERSION, consequenceUpdate=self.plan))
        rc.commit_consequences(self.context, self.state, result, 'valid')
        result['narrativeText'] += '\n\n你已经按照他的要求跪下。'
        with self.assertRaisesRegex(ValueError, '全文'):
            rc.commit_consequences(self.context, self.state, result, 'invalid')
        with self.assertRaises(ValueError):
            apply_branch_patch(self.package, self.state, {'readerEntityStates': {GU: {'姿态': '跪下'}}}, 'fixture')


if __name__ == '__main__':
    unittest.main()
