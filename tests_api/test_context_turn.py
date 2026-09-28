"""Prose delivery is independent of desired outcomes and observation success."""
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from open_story_engine import reader_consequences as rc, narrative_delivery as delivery
from open_story_engine.api_narrative import ContextNarrativePlanner
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.api_reader_quality import action_requirements
from open_story_engine.context_turn import bind_plan_base
from open_story_engine.llm import Completion, LlmError
from tests_api import test_reader_consequences as fixtures
from tests_api.test_reader_consequences import GU, LU, ROOT, plan


def scene(name, people):
    return dict(name=name, basis='observed', paragraphIds=['P1'],
                presentEntities=[dict(entityId=p, entityName='你' if p == GU else '陆照临',
                                      basis='observed', paragraphIds=['P1']) for p in people])


class ContextTurnTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ConsequenceTests()
        fixture.setUp()
        self.fixture = fixture
        self.action = '留在原地等候'
        self.body = '风像一只看不见的手拨动衣袖，你留在原地等候。\n\n你把手收进袖中，听着身旁渐渐平静的呼吸。'
        self.context = {**fixture.context, 'playerDirection': self.action, 'characterDetails': []}
        self.plan = plan(action_requirements(self.action))
        self.data = dict(summary='你在原地等候。', actionStatus='performed', updates={}, followups=[], resolvedFollowups=[])

    def gateway(self, data=None):
        gateway = Mock(model='fixture')
        gateway.complete_text.return_value = Completion(self.body, '{}', [])
        values = [self.plan, self.data if data is None else data]
        gateway.complete_json.side_effect = [v if isinstance(v, Exception) else Completion(json.dumps(v, ensure_ascii=False), '{}', [])
                                             for v in values]
        gateway.__deepcopy__ = lambda memo: gateway
        return gateway

    def generate(self, gateway):
        selected = dict(id='custom', title=self.action, summary=self.action,
                        isFreeText=True, statePatch={'freeTextProgress': 1})
        return ContextNarrativePlanner(gateway).plan(self.context, selected, self.fixture.root['branchState'])

    def commit(self, result):
        return rc.commit_consequences(self.context, self.fixture.root['branchState'], result, 'committed')

    def test_minor_prose_saved_once_and_request_binding_still_protected(self):
        gateway = self.gateway()
        result, audit = self.generate(gateway)
        self.assertEqual(result['narrativeText'], self.body)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 2)
        for key in ('authorityReview', 'consequenceReview', 'sceneChecks', 'eventChecks', 'stateReceipt'):
            self.assertNotIn(key, result)
        self.assertFalse(audit['promptContext']['proseReview']['automaticGate'])
        self.commit(result)
        for changes in ({'narrativeText': self.body + '你离开了。'},
                        {'deliveryReceipt': {**result['deliveryReceipt'], 'actionSha256': 'wrong'}},
                        {'continuityFollowups': [{'id': 'forged'}]}):
            with self.assertRaises(ValueError):
                self.commit({**result, **changes})

    def test_identity_cards_reach_writer_and_extractor_without_extra_calls(self):
        self.action = '确认顾长离的身份'
        self.context['playerDirection'] = self.action
        gateway = self.gateway()
        from open_story_engine.module_context import ModuleContextResolver
        resolver = ModuleContextResolver.for_package(
            ROOT / 'content/packages/taixu-relics-part1/0.1.3/package.json', self.fixture.package)
        selected = dict(id='custom', title=self.action, summary=self.action, isFreeText=True, statePatch={})
        self.context.update(validatedStatePatch={}, validatedStatePatchSource='apply_branch_patch')
        result, audit = ContextNarrativePlanner(gateway, context_resolver=resolver,
            require_context_bundle=True).plan(self.context, selected, self.fixture.root['branchState'])
        plan_input = json.loads(gateway.complete_json.call_args_list[0].args[0][-1]['content'])
        cards = plan_input['contextProjection']['hardConstraints']['entityFacts']
        self.assertTrue(any(e['name'] == '顾长离' for e in cards['entities']), cards)
        writer_input = gateway.complete_text.call_args.args[0][-1]['content']
        self.assertIn('unconfirmedMentions', writer_input)
        extraction = json.loads(gateway.complete_json.call_args_list[-1].args[0][-1]['content'])
        self.assertTrue(any(e['name'] == '顾长离' for e in extraction['entityFacts']['entities']))
        self.assertNotIn('branchLedger', extraction['authoritativeState'])
        self.assertLess(len(extraction['registry']), len(self.fixture.package['characters']))
        self.assertEqual(result['narrativeText'], self.body)
        self.assertEqual(gateway.complete_json.call_count, 2)

    def test_near_name_reference_does_not_change_registered_person(self):
        self.body = '册页上写着“陆照”二字，身份还不确定。'
        self.data['updates'] = dict(outcomes=[dict(characterId=LU, entityName='陆照',
            status='missing', permanence='temporary', cause='册上记载', paragraphIds=['P1'])])
        result, _ = self.generate(self.gateway())
        self.commit(result)
        self.assertEqual(result['narrativeText'], self.body)
        self.assertEqual(result['consequenceUpdate']['outcomes'], [])
        self.assertTrue(result['observationDiagnostics'])

    def test_copied_location_label_cannot_override_actual_place_reference(self):
        location = self.fixture.root['branchState']['playerLocationId']
        from open_story_engine.reader_actions import registry
        name = registry(self.fixture.package, self.fixture.root['branchState'])[location]['name']
        self.body = '你走进器物房，站在案前。'
        self.data['confirmedStates'] = [dict(entityId=GU, entityName='你', basis='observed', attribute='locationId', value=location,
            locationName=name, observedLocationName='器物房', reason='已经进屋', paragraphIds=['P1'])]
        result, _ = self.generate(self.gateway())
        self.commit(result)
        self.assertTrue(result['observationDiagnostics'])
        self.assertEqual(result['consequenceUpdate']['stateChanges'], [])
        self.assertEqual(result['narrativeText'], self.body)

    def test_current_scene_registers_literal_new_place_without_guessing_old_id(self):
        old = copy.deepcopy(self.fixture.root['branchState'])
        self.body = '你和陆照临一起走进器物房，停在案前。'
        self.data['currentScene'] = scene('器物房', [GU, LU])
        self.data['confirmedStates'] = [dict(entityId=GU, attribute='当前位置',
            value=old['playerLocationId'], reason='借用旧地点', paragraphIds=['P1'])]
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        place = result['consequenceUpdate']['introductions']['locations'][0]
        self.assertEqual(place['name'], '器物房')
        self.assertNotEqual(place['id'], old['playerLocationId'])
        self.assertEqual(state['playerLocationId'], place['id'])
        self.assertEqual(state['characterLocationIds'][LU], place['id'])
        self.assertEqual(self.fixture.root['branchState'], old)
        self.assertNotIn('当前位置', state.get('readerEntityStates', {}).get(GU, {}))
        self.assertEqual(result['narrativeText'], self.body)

    def test_current_scene_reuses_exact_place_and_defers_unquoted_or_ambiguous_name(self):
        state = self.context['parent']['branchState']
        state['derivedLocations'] = [dict(id='location_qiwu', name='器物房', summary='此前登记')]
        self.data['currentScene'] = scene('器物房', [GU])
        record = delivery.observe(self.context, '你在器物房里翻册。', self.data)
        self.assertEqual(record['update']['introductions']['locations'], [])
        self.assertEqual(record['update']['stateChanges'][0]['value'], 'location_qiwu')
        bad = delivery.observe(self.context, '你在廊下。', self.data)
        self.assertTrue(bad['diagnostics'])
        self.assertEqual(bad['update']['stateChanges'], [])
        state['derivedLocations'].append(dict(id='location_qiwu2', name='器物房', summary='另一处同名房间'))
        ambiguous = delivery.observe(self.context, '你在器物房里。', self.data)
        self.assertTrue(ambiguous['diagnostics'])
        self.assertEqual(ambiguous['update']['stateChanges'], [])

    def test_new_place_display_name_can_bind_its_literal_mention_without_aliasing_old_place(self):
        self.body = '你和陆照临站在库房西架这间屋子里。'
        name = '库房西架的器物房'
        self.data['updates'] = dict(introductions=dict(locations=[dict(
            id='location_qiwu', name=name, summary='本回合进入的新房间', paragraphIds=['P1'])]))
        self.data['currentScene'] = scene(name, [GU, LU])
        self.data['confirmedStates'] = [dict(entityId=GU, entityName='你', basis='observed', attribute='locationId', value='location_qiwu',
            locationName=name, observedLocationName='库房西架这间屋子', reason='在场', paragraphIds=['P1'])]
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        self.assertEqual(state['playerLocationId'], 'location_qiwu')
        self.assertEqual(state['characterLocationIds'][LU], 'location_qiwu')
        self.assertEqual(result['observationDiagnostics'], [])
        self.assertEqual(result['narrativeText'], self.body)

    def test_scene_with_undefined_model_id_registers_literal_place_for_present_people(self):
        self.body = '你和陆照临站在库房西架这间屋子里。'
        self.data['currentScene'] = scene('库房西架的器物房', [GU, LU])
        self.data['confirmedStates'] = [dict(entityId=GU, entityName='你', basis='observed', attribute='locationId', value='location_undefined',
            locationName='库房西架的器物房', observedLocationName='库房西架这间屋子', reason='在场', paragraphIds=['P1'])]
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        place = result['consequenceUpdate']['introductions']['locations'][0]
        self.assertEqual(place['name'], '库房西架这间屋子')
        self.assertNotEqual(place['id'], 'location_undefined')
        self.assertEqual(state['playerLocationId'], place['id'])
        self.assertEqual(state['characterLocationIds'][LU], place['id'])
        self.assertEqual(result['narrativeText'], self.body)

    def test_unachieved_death_does_not_veto_prose_or_invent_death(self):
        self.action = '本回合杀死陆照临'
        self.context['playerDirection'] = self.action
        self.plan = self.fixture.plan
        self.body = '你出手后陆照临倒在地上，但他仍有呼吸。'
        self.data.update(summary='陆照临倒地，仍有呼吸。', actionStatus='partial',
            updates=dict(outcomes=[dict(characterId=LU, entityName='陆照临', basis='observed', status='injured', permanence='temporary',
                                       cause='本回合受伤', paragraphIds=['P1'])]))
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        self.assertEqual(result['narrativeText'], self.body)
        self.assertEqual(state['characterOutcomeStates'][LU]['status'], 'injured')
        self.assertTrue(any(f['kind'] == 'action_pending' for f in result['continuityFollowups']))

    def test_extraction_failure_keeps_body_once_and_marks_unconfirmed(self):
        gateway = self.gateway(LlmError('提取超时', 'timeout'))
        result, _ = self.generate(gateway)
        self.commit(result)
        self.assertEqual(result['narrativeText'], self.body)
        self.assertEqual(result['consequenceUpdate']['outcomes'], [])
        self.assertTrue(result['observationDiagnostics'])
        self.assertTrue(delivery.pending(result))
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 2)

    def test_malformed_observations_do_not_lose_prose_or_invent_location(self):
        self.data['updates'] = dict(stateChanges=[dict(entityId=GU, entityName='你', basis='observed', attribute='locationId', value='missing',
                                                     reason='进入房间', paragraphIds=['P2'])])
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        self.assertEqual(state['playerLocationId'], self.fixture.root['branchState']['playerLocationId'])
        self.assertTrue(result['observationDiagnostics'])
        self.assertEqual(result['narrativeText'], self.body)

    def test_invalid_plan_does_not_stop_body_generation(self):
        self.plan = {'invalid': True}
        result, audit = self.generate(self.gateway())
        self.commit(result)
        self.assertEqual(result['narrativeText'], self.body)
        self.assertTrue(audit['nonBlockingNotes'])

    def test_unchanged_thread_is_not_a_new_uncertainty(self):
        from open_story_engine import reader_threads
        current = reader_threads.threads_for(self.fixture.package, self.context['contract'],
                                             self.context['parent']['branchState'])
        current[0].update(status='open', priority='high', recoveryWindow='near')
        self.context['parent']['branchState'][reader_threads.STATE_KEY] = current
        self.data['updates'] = dict(threadUpdates=[dict(id=current[0]['id'], title=current[0]['title'],
            status='open', reason='仍未回答', paragraphIds=['P1'])])
        record = delivery.observe(self.context, self.body, self.data)
        self.assertEqual(record['update']['threadUpdates'], [])
        self.assertEqual(record['diagnostics'], [])
        self.assertEqual(record['followups'], [])

    def test_empty_body_is_real_failure(self):
        self.body = ' \n '
        with self.assertRaisesRegex(LlmError, '未返回实际正文'):
            self.generate(self.gateway())

    def test_unusable_observation_fields_remain_nonblocking(self):
        for raw in (None, [], {'updates': []}, {'updates': {'stateChanges': [None, [], {}]}},
                    {'updates': {'outcomes': [{'characterId': []}]}},
                    {'updates': {'goalUpdates': [{'id': []}]}},
                    {'updates': {'threadUpdates': [None]}},
                    {'resolvedFollowups': [{'id': []}]}, {'followups': [None]},
                    {'actionStatus': []}):
            with self.subTest(raw=raw):
                record = delivery.observe(self.context, self.body, raw)
                self.assertIsInstance(record['update'], dict)

    def test_followup_selection_is_bounded_without_losing_saved_issues(self):
        issues = [dict(id=str(i), status='open', summary='其他待办' + str(i), evidence='原文') for i in range(10)]
        issues[-1]['summary'] = '确认侧门能否通行'
        node = {'continuityFollowups': issues}
        selected = delivery.pending(node, '确认侧门', limit=4)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['id'], '9')
        self.assertEqual(len(node['continuityFollowups']), 10)

    def test_injected_followup_evidence_is_short_but_saved_evidence_stays_intact(self):
        from test_support.longform import longform_cases
        prose = longform_cases()[0]['source'].read_text()[:4000]
        issue = dict(id='old-question', status='open', summary='去向待查', evidence=prose)
        node = dict(continuityFollowups=[issue])
        selected = delivery.pending(node)[0]
        self.assertEqual(selected['evidence'], prose[:240])
        self.assertTrue(selected['evidenceTruncated'])
        self.assertEqual(node['continuityFollowups'][0]['evidence'], prose)

    def test_actual_death_is_durable_and_wrong_revival_is_deferred(self):
        self.action = '本回合杀死陆照临'
        self.context['playerDirection'] = self.action
        self.plan = self.fixture.plan
        self.body = '你扼住陆照临的喉咙，直到他彻底停止呼吸，确认已经死亡。'
        self.data['updates'] = dict(outcomes=[dict(characterId=LU, entityName='陆照临', basis='observed', status='dead', cause='窒息死亡', paragraphIds=['P1'])])
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        self.assertEqual(state['characterOutcomeStates'][LU]['status'], 'dead')
        from open_story_engine.dynamic_memory import receipt_for
        node = {**result, 'id': 'committed', 'parentId': self.fixture.root['id'],
                'sourceNodeRef': self.fixture.root['sourceNodeRef'], 'branchState': state}
        self.assertIsNotNone(receipt_for(self.fixture.package, node))
        context = {**self.context, 'parent': node}
        bad = dict(actionStatus='performed', updates=dict(outcomes=[dict(characterId=LU, status='alive',
                   permanence='temporary', cause='他又站起来了', paragraphIds=['P1'])]))
        record = delivery.observe(context, '陆照临又站了起来。', bad)
        self.assertEqual(record['update']['outcomes'], [])
        self.assertTrue(record['diagnostics'])
        self.assertTrue(record['followups'])
        self.assertEqual(state['characterOutcomeStates'][LU]['status'], 'dead')

    def test_unnamed_person_gets_independent_durable_outcome_without_guessing_identity(self):
        self.action = '杀死眼前的伤者'
        self.context['playerDirection'] = self.action
        self.body = '你杀死了伤者。伤者已经死亡，没有呼吸。'
        cid = 'character_wounded_stranger'
        self.data['updates'] = dict(introductions=dict(characters=[dict(id=cid, name='伤者',
            summary='当前场景中身份未明的伤者', paragraphIds=['P1'])], items=[], locations=[]),
            outcomes=[dict(characterId=cid, entityName='伤者', basis='observed', status='dead',
                permanence='permanent', cause='玩家杀死眼前伤者', paragraphIds=['P1'], evidenceQuote=self.body)])
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        self.assertEqual(state['characterOutcomeStates'][cid]['status'], 'dead')
        self.assertEqual(state['characterOutcomeStates'][cid]['permanence'], 'permanent')
        self.assertNotIn(LU, state['characterOutcomeStates'])
        self.assertEqual(result['narrativeText'], self.body)

    def test_new_person_pronoun_outcome_uses_same_turn_introduction_anchor(self):
        self.action = '杀死眼前的伤者'
        self.context['playerDirection'] = self.action
        self.body = '伤者倒在石阶下。\n\n你对他动了手。\n\n他的呼吸停了，身体渐渐冷下去。'
        cid = 'character_wounded_stranger'
        self.data['updates'] = dict(introductions=dict(characters=[dict(id=cid, name='伤者',
            summary='身份未明的伤者', paragraphIds=['P1', 'P3'])], items=[], locations=[]),
            outcomes=[dict(characterId=cid, entityName='伤者', basis='observed', status='dead',
                permanence='permanent', cause='玩家杀死眼前伤者', paragraphIds=['P2', 'P3'])])
        original = copy.deepcopy(self.data)
        gateway = self.gateway()
        result, _ = self.generate(gateway)
        state = self.commit(result)
        self.assertEqual(state['characterOutcomeStates'][cid]['status'], 'dead')
        self.assertEqual(state['characterOutcomeStates'][cid]['evidence'], self.body)
        self.assertEqual(self.data, original)
        self.assertEqual(gateway.complete_json.call_count, 2)
        self.assertEqual(result['narrativeText'], self.body)

        # A later pronoun in a reported account is still not observed death.
        self.body = '伤者倒在石阶下。\n\n守门人说：“他已死了。”'
        self.data['updates']['introductions']['characters'][0]['paragraphIds'] = ['P1']
        self.data['updates']['outcomes'][0]['paragraphIds'] = ['P2']
        result, _ = self.generate(self.gateway())
        self.assertEqual(result['consequenceUpdate']['outcomes'], [])
        self.assertTrue(result['observationDiagnostics'])

    def test_selected_result_paragraph_excludes_unrelated_dialogue_without_copying_prose(self):
        self.body = '守门人喊：“你做什么！”\n\n你杀死了伤者，伤者的呼吸停止。\n\n守门人说：“去请执事。”'
        cid = 'character_wounded_stranger'
        self.data['updates'] = dict(introductions=dict(characters=[dict(id=cid, name='伤者',
            summary='身份未明的伤者', paragraphIds=['P2'])], items=[], locations=[]),
            outcomes=[dict(characterId=cid, entityName='伤者', basis='observed', status='dead',
                permanence='permanent', cause='玩家杀死伤者', paragraphIds=['P1', 'P2', 'P3'], evidenceParagraphId='P2')])
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['characterOutcomeStates'][cid]['evidence'], self.body.split('\n\n')[1])
        self.assertEqual(result['narrativeText'], self.body)
        for changes in ({'evidenceParagraphId': 'P9'}, {'evidenceParagraphId': 'P1'},
                        {'paragraphIds': ['P1', 'P3']}, {'evidenceQuote': '伤者死了'}):
            data = copy.deepcopy(self.data)
            data['updates']['outcomes'][0].update(changes)
            result, _ = self.generate(self.gateway(data))
            self.assertEqual(result['consequenceUpdate']['outcomes'], [])
            self.assertTrue(result['observationDiagnostics'])

    def test_separate_result_paragraph_and_new_identity_preserve_legacy_receipts(self):
        self.body = '伤者躺在石阶旁。\n\n守门人喊：“停手！”\n\n你杀死了他，他的呼吸停止。'
        cid = 'character_wounded_stranger'
        self.data['updates'] = dict(introductions=dict(characters=[dict(id=cid, name='伤者',
            summary='身份未明的伤者', paragraphIds=['P1'])], items=[], locations=[]),
            outcomes=[dict(characterId=cid, entityName='伤者', basis='observed', status='dead',
                permanence='permanent', cause='玩家杀死伤者', paragraphIds=['P1', 'P3'], evidenceParagraphId='P3')])
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['characterOutcomeStates'][cid]['evidence'], self.body.split('\n\n')[2])
        data = result['deliveryReceipt']['data']
        old = delivery.observe(self.context, self.body, data, separate_identity=False)
        self.assertEqual(old['update']['outcomes'], [])
        saved = {**result, 'consequenceUpdate': old['update'], 'readerOutcome': old['outcome'],
                 'continuityFollowups': old['followups'],
                 'deliveryReceipt': {**result['deliveryReceipt'], 'version': 'narrative-delivery/4'}}
        delivery.validate_commit(self.context, saved)
        for change in ({'basis': 'reported'}, {'entityName': '陌生伤者'}, {'evidenceParagraphId': 'P2'}):
            invalid = copy.deepcopy(self.data)
            invalid['updates']['outcomes'][0].update(change)
            rejected, _ = self.generate(self.gateway(invalid))
            self.assertEqual(rejected['consequenceUpdate']['outcomes'], [])
        invalid = copy.deepcopy(data)
        invalid['updates']['outcomes'][0]['evidenceParagraphId'] = 'P99'
        self.assertEqual(delivery.observe(self.context, self.body, invalid)['update']['outcomes'], [])

    def test_followup_survives_unselected_turn_and_needs_actual_resolution_reference(self):
        prior = delivery.observe(self.context, self.body, {**self.data, 'actionStatus': 'partial'})
        self.assertTrue(prior['followups'])
        node = {**self.context['parent'], 'continuityFollowups': prior['followups']}
        context = {**self.context, 'parent': node}
        unchanged = delivery.observe(context, '你转头查看雨势，并没有继续之前的行动。', self.data)
        self.assertEqual(unchanged['followups'], prior['followups'])
        identifier = prior['followups'][0]['id']
        invalid = {**self.data, 'resolvedFollowups': [dict(id=identifier, summary='已解决', paragraphIds=['P99'])]}
        self.assertEqual(delivery.observe(context, self.body, invalid)['followups'][0]['status'], 'open')
        resolved = {**self.data, 'resolvedFollowups': [dict(id=identifier, summary='执事终于到场答复', paragraphIds=['P1'])]}
        record = delivery.observe(context, '执事赶到了，向你交代了救治的安排。', resolved)
        self.assertEqual(record['followups'][0]['status'], 'resolved')
        self.assertEqual(delivery.pending({'continuityFollowups': record['followups']}), [])

    def test_reported_or_inferred_custody_stays_open_without_rejecting_prose(self):
        item = self.fixture.package['items'][0]['id']
        parent = self.context['parent']['branchState']
        parent.setdefault('itemOwnerCharacterIds', {})[item] = GU
        self.body = '书办说：“也许东西早已交给陆照临了，我没亲眼看见，你可以找他核实。”'
        for field in ('stateChanges', 'confirmedStates'):
            for basis in ('reported', 'inferred'):
                with self.subTest(field=field, basis=basis):
                    data = copy.deepcopy(self.data)
                    entry = dict(entityId=item, attribute='ownerCharacterId', value=LU,
                                 reason='书办猜测东西已交给陆照临，未亲眼看见',
                                 basis=basis, paragraphIds=['P1'])
                    (data if field == 'confirmedStates' else data['updates'])[field] = [entry]
                    gateway = self.gateway(data)
                    result, audit = self.generate(gateway)
                    self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], GU)
                    self.assertEqual(result['narrativeText'], self.body)
                    self.assertEqual(result['observationDiagnostics'], [])
                    issue = result['continuityFollowups'][0]
                    self.assertEqual(issue['kind'], 'reported_state')
                    self.assertIn('书办', issue['summary'])
                    self.assertEqual(issue['evidence'], self.body)
                    self.assertEqual(issue['status'], 'open')
                    self.assertEqual(gateway.complete_text.call_count, 1)
                    self.assertFalse(audit['promptContext']['proseReview']['automaticGate'])
        self.assertEqual(parent['itemOwnerCharacterIds'][item], GU)

    def test_rumored_death_does_not_become_an_irreversible_outcome(self):
        self.body = '书办说：“听说陆照临死了，我没有见到尸体。”'
        self.data['updates'] = dict(outcomes=[dict(characterId=LU, status='dead', permanence='permanent',
            basis='reported', cause='书办转述陆照临死亡的传闻', paragraphIds=['P1'])])
        result, _ = self.generate(self.gateway())
        self.assertEqual(result['consequenceUpdate']['outcomes'], [])
        self.assertNotEqual(self.commit(result).get('characterOutcomeStates', {}).get(LU, {}).get('status'), 'dead')
        self.assertTrue(result['continuityFollowups'])
        self.assertEqual(result['narrativeText'], self.body)

    def test_later_observation_can_confirm_state_after_an_earlier_guess(self):
        item = self.fixture.package['items'][0]['id']
        self.context['parent']['branchState'].setdefault('itemOwnerCharacterIds', {})[item] = GU
        self.body = '书办猜东西已交给陆照临。\n\n你随后当面将药瓶交给陆照临，他接过收好。'
        guessed = dict(entityId=item, attribute='ownerCharacterId', value=LU,
                       reason='书办猜测', basis='inferred', paragraphIds=['P1'])
        observed = dict(entityId=item, entityName='药瓶', attribute='ownerCharacterId', value=LU,
                        reason='实际交接', basis='observed', paragraphIds=['P2'])
        self.data['updates'] = dict(stateChanges=[guessed, observed])
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], LU)
        self.assertEqual(result['consequenceUpdate']['stateChanges'][0]['evidence'], self.body.split('\n\n')[1])

    def test_actual_item_transfer_need_not_equal_planned_holder(self):
        item = self.fixture.package['items'][0]['id']
        self.context['parent']['branchState'].setdefault('itemOwnerCharacterIds', {})[item] = GU
        self.body = '你把药瓶交给陆照临，他接过并收好。'
        self.data['updates'] = dict(stateChanges=[dict(entityId=item, entityName='药瓶', basis='observed', attribute='ownerCharacterId', value=LU,
                                                     reason='实际交接', paragraphIds=['P1'])])
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.plan['stateChanges'], [])
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], LU)

    def test_confirmed_current_custody_repairs_new_branch_without_editing_parent(self):
        item = self.fixture.package['items'][0]['id']
        parent = self.context['parent']['branchState']
        parent.setdefault('itemOwnerCharacterIds', {})[item] = GU
        self.body = '陆照临打开匣子，你当面核对过，药瓶确实仍由他保管。'
        self.data['confirmedStates'] = [dict(entityId=item, entityName='药瓶', basis='observed', attribute='ownerCharacterId', value=LU,
                                            reason='当面核对现状，补齐此前漏记的保管人', paragraphIds=['P1'])]
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], LU)
        self.assertEqual(parent['itemOwnerCharacterIds'][item], GU)
        self.assertEqual(result['narrativeText'], self.body)
        self.data['confirmedStates'][0]['paragraphIds'] = ['P99']
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], GU)
        self.assertTrue(result['observationDiagnostics'])

    def test_followup_progress_reuses_id_and_cannot_also_close_it(self):
        old = dict(id='followup-lamp', kind='continuity', status='open', summary='灯的来源待查',
                   evidence='铜灯出现在舍内。', entityIds=[], originParentId='earlier')
        self.context['parent']['continuityFollowups'] = [old]
        self.body = '书办接收铜灯，说明来历仍待查。'
        self.data['followups'] = [dict(id=old['id'], summary='灯已接收，来历仍待查', paragraphIds=['P1'])]
        self.data['resolvedFollowups'] = [dict(id=old['id'], summary='已接收', paragraphIds=['P1'])]
        result, _ = self.generate(self.gateway())
        self.commit(result)
        self.assertEqual(len(result['continuityFollowups']), 1)
        updated = result['continuityFollowups'][0]
        self.assertEqual(updated['id'], old['id'])
        self.assertEqual(updated['status'], 'open')
        self.assertEqual(updated['originParentId'], 'earlier')
        self.assertEqual(old['summary'], '灯的来源待查')
        self.assertTrue(result['observationDiagnostics'])

    def test_top_level_state_changes_are_validated_and_committed(self):
        item = self.fixture.package['items'][0]['id']
        self.context['parent']['branchState'].setdefault('itemOwnerCharacterIds', {})[item] = GU
        self.body = '你把药瓶交给陆照临，他接过并收好。'
        self.data['stateChanges'] = [dict(entityId=item, entityName='药瓶', basis='observed', attribute='ownerCharacterId', value=LU,
                                        reason='实际交接', paragraphIds=['P1'])]
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], LU)
        self.data['updates']['stateChanges'] = []
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], GU)
        del self.data['updates']['stateChanges']
        self.data['stateChanges'][0]['value'] = 'character_unregistered'
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], GU)
        self.assertTrue(result['observationDiagnostics'])

    def test_custody_alias_cannot_override_actual_handover_and_bad_change_can_be_recovered(self):
        item = self.fixture.package['items'][0]['id']
        self.context['parent']['branchState'].setdefault('itemOwnerCharacterIds', {})[item] = GU
        self.body = '你把药瓶交给陆照临，他接过并收好。'
        change = dict(entityId=item, entityName='药瓶', basis='observed', attribute='holderCharacterId', value=LU,
                      reason='实际交接', paragraphIds=['P1'])
        self.data['updates'] = dict(stateChanges=[change])
        self.data['confirmedStates'] = [dict(change, attribute='ownerCharacterId', value=GU)]
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], LU)
        self.assertTrue(result['observationDiagnostics'])
        change['paragraphIds'] = ['P99']
        self.data['confirmedStates'] = [dict(change, paragraphIds=['P1'])]
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], LU)

    def test_unrelated_open_questions_do_not_fill_a_specific_action_context(self):
        node = dict(continuityFollowups=[
            dict(id='lamp', status='open', summary='铜灯去向仍待核实', evidence='旧事'),
            dict(id='ledger', status='open', summary='何进山是否领队仍待核实', evidence='新疑问')])
        before = copy.deepcopy(node)
        selected = delivery.continuity_context(node, '核对何进山是否领队')
        self.assertEqual([i['id'] for i in selected['pendingFollowups']], ['ledger'])
        self.assertEqual(delivery.continuity_context(node, '观看日出')['pendingFollowups'], [])
        self.assertEqual(len(delivery.pending(node, '继续')), 2)
        self.assertEqual(node, before)

    def test_resolved_answer_is_found_by_new_name_without_injecting_old_unknown(self):
        issue = dict(id='name', status='resolved', summary='押送人姓名未核实', evidence='旧纸条：姓名未核实',
                     resolution='当面对照总册，押送人全名为何进山。', resolutionEvidence='“何进山。”你逐字读出。')
        node = dict(continuityFollowups=[issue])
        before = copy.deepcopy(node)
        for query in ('何进山', '继续查何进山的线索', '押送人姓名'):
            selected = delivery.continuity_context(node, query)
            answer = selected['resolvedFollowups'][0]
            self.assertEqual(answer['id'], 'name')
            self.assertEqual(answer['resolution'], issue['resolution'])
            self.assertNotIn('姓名未核实', json.dumps(selected, ensure_ascii=False))
        self.assertEqual(node, before)
        self.assertEqual(delivery.continuity_context(node, '天气')['resolvedFollowups'], [])
        self.assertEqual(delivery.continuity_context(node, '何进山', limit=0),
                         dict(pendingFollowups=[], resolvedFollowups=[]))

    def test_location_name_id_mismatch_is_not_committed_or_a_prose_veto(self):
        old = self.context['parent']['branchState']['playerLocationId']
        self.body = '你抵达器物房，在门边停下。'
        self.data['confirmedStates'] = [dict(entityId=GU, entityName='你', basis='observed', attribute='locationId', value=old,
            locationName='器物房', observedLocationName='器物房', reason='抵达器物房', paragraphIds=['P1'])]
        gateway = self.gateway()
        result, _ = self.generate(gateway)
        self.assertEqual(self.commit(result)['playerLocationId'], old)
        self.assertTrue(any('地点' in d['reason'] for d in result['observationDiagnostics']))
        self.assertEqual(result['narrativeText'], self.body)
        self.assertEqual(gateway.complete_text.call_count, 1)

    def test_new_observed_location_moves_present_people_without_renaming_old_place(self):
        parent = copy.deepcopy(self.context['parent'])
        self.body = '你与陆照临进入器物房，在长案旁停下。'
        self.data['updates'] = dict(introductions=dict(locations=[dict(id='location_new_storage',
            name='器物房', summary='库房西架的器物房，供当面调阅总册。', paragraphIds=['P1'])]))
        self.data['confirmedStates'] = [dict(entityId=cid, entityName='你' if cid == GU else '陆照临', attribute='locationId',
            value='location_new_storage', locationName='器物房', observedLocationName='器物房', basis='observed',
            reason='实际抵达器物房', paragraphIds=['P1']) for cid in (GU, LU)]
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        self.assertEqual(state['playerLocationId'], 'location_new_storage')
        self.assertTrue(all(state['characterLocationIds'][cid] == 'location_new_storage' for cid in (GU, LU)))
        self.assertEqual(self.context['parent'], parent)
        self.assertEqual(result['observationDiagnostics'], [])

    def test_resolved_answers_survive_and_share_context_budget_with_open_work(self):
        issues = [dict(id='answer', status='resolved', summary='铜灯由谁保管', resolution='书办保管',
                       resolutionEvidence='书办接过铜灯。')]
        issues += [dict(id=str(i), status='open', summary='待查事项' + str(i)) for i in range(8)]
        self.context['parent']['continuityFollowups'] = issues
        record = delivery.observe(self.context, self.body, self.data)
        self.assertIn(issues[0], record['followups'])
        node = {'continuityFollowups': record['followups']}
        selected = delivery.continuity_context(node, '铜灯保管')
        self.assertEqual(selected['resolvedFollowups'][0]['id'], 'answer')
        self.assertEqual(sum(map(len, selected.values())), 1)
        self.assertEqual(len(delivery.continuity_context(node, '问天气')['resolvedFollowups']), 0)
        unseen = next(i for i in issues[1:] if i['id'] not in
                      {f['id'] for f in delivery.continuity_context(node, self.action)['pendingFollowups']})
        data = {**self.data, 'resolvedFollowups': [dict(id=unseen['id'], summary='已解决', paragraphIds=['P1'])]}
        record = delivery.observe(self.context, self.body, data)
        self.assertEqual(next(i for i in record['followups'] if i['id'] == unseen['id'])['status'], 'open')
        self.assertTrue(record['diagnostics'])

    def test_real_api_store_commits_observation_failure_and_idempotent_replay(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'STORY_PLANNER': 'mock', 'JEV_RUNTIME_REVIEW_MODE': 'off'}):
            read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
            play = PlayService(read, Path(tmp))
            self.addCleanup(play.drafts.close)
            start = play.create_session('taixu-relics-part1', '0.1.3', 'entry_gu_trial', GU, identity_opening=True)
            gateway = self.gateway(LlmError('记录不可用', 'timeout'))
            text_call = gateway.complete_text
            menu = {'choices': [dict(title='观察眼前动静', action='我留在原地观察周围的动静。', paragraphIds=['P1'], interactWith=[])]}
            gateway.complete_json.side_effect = [Completion(json.dumps(self.plan), '{}', []), LlmError('记录不可用', 'timeout'),
                                                Completion(json.dumps(menu), '{}', [])]
            play._planner = ContextNarrativePlanner(gateway)
            with patch('open_story_engine.cocreation.guard_narrative', side_effect=AssertionError('old prose gate')):
                first = play.continue_turn(start['session']['id'], start['branch']['id'], text=self.action, request_id='same')
                second = play.continue_turn(start['session']['id'], start['branch']['id'], text=self.action, request_id='same')
            self.assertEqual(first['branch']['narrativeText'], self.body)
            self.assertEqual(first['branch']['id'], second['branch']['id'])
            self.assertTrue(second['deduplicated'])
            self.assertTrue(first['branch']['continuityFollowups'])
            self.assertEqual(text_call.call_count, 1)

    def test_parent_owns_before_and_identity_cannot_be_reassigned(self):
        candidate = copy.deepcopy(self.plan)
        old = self.context['parent']['branchState']['playerLocationId']
        candidate['stateChanges'] = [dict(id='C1', entityId=GU, attribute='locationId', before='guessed',
                                        value='proposed-location', stepId='S1', reason='行动')]
        self.assertEqual(bind_plan_base(candidate, self.context)['stateChanges'][0]['before'], old)
        self.assertEqual(candidate['stateChanges'][0]['before'], 'guessed')
        candidate['introductions']['characters'] = [dict(id=GU, name='另一个执事', summary='新人物', sourceStepId='S1')]
        with self.assertRaisesRegex(ValueError, '不同人物须使用新的独立ID'):
            bind_plan_base(candidate, self.context)

    def test_movement_intent_cannot_commit_unobserved_arrival(self):
        from open_story_engine.cocreation import DirectionEvaluator
        parent = self.context['parent']
        location = next(p for p in self.fixture.package['locations']
                        if p['id'] != parent['branchState']['playerLocationId'])
        action = '我带着陆照临前往' + location['name']
        selected = DirectionEvaluator(self.fixture.package)._custom_direction(parent, action)
        self.assertIn('characterLocationIds', selected['statePatch'])
        prepared = ContextNarrativePlanner.prepare_direction(self.fixture.package, parent, selected, action)
        self.assertEqual(prepared['statePatch'], {'freeTextProgress': parent['branchState']['freeTextProgress'] + 1})
        self.assertIn('characterLocationIds', selected['statePatch'])

    def test_valid_long_evidence_and_empty_introductions_are_not_uncertain(self):
        paragraphs = ['执事走近。', '他取出药箱。', '他救治伤者。', '伤者醒来。', '你留在旁边。', '他把药箱收好。']
        body = '\n\n'.join(paragraphs)
        data = {**self.data, 'updates': {'introductions': []},
                'followups': [dict(summary='还需确认后续照料安排', paragraphIds=['P1','P2','P3','P4','P5','P6'])]}
        record = delivery.observe(self.context, body, data)
        self.assertEqual(record['diagnostics'], [])
        self.assertEqual(len(record['followups']), 1)
        self.assertEqual(record['followups'][0]['evidence'], body)

    def test_flat_typed_introductions_support_actual_item_transfer(self):
        body = '书办将外门木牌交给你，你接过收好。'
        data = {**self.data, 'updates': {
            'introductions': [dict(id='character_new_clerk', name='书办', summary='办理登记的书办', paragraphIds=['P1']),
                              dict(id='item_new_token', name='外门木牌', summary='书办发给你的木牌', paragraphIds=['P1'])],
            'stateChanges': [dict(entityId='item_new_token', entityName='外门木牌', basis='observed', attribute='ownerCharacterId', value=GU,
                                  reason='接过木牌', paragraphIds=['P1'])]}}
        record = delivery.observe(self.context, body, data)
        self.assertEqual(record['diagnostics'], [])
        self.assertEqual(len(record['update']['introductions']['characters']), 1)
        self.assertEqual(record['update']['stateChanges'][0]['value'], GU)

    def test_reported_scene_cannot_move_people_even_with_valid_ids(self):
        self.body = '顾长离听门外的人说：“陆照临已经去了器物房。”顾长离仍留在原处，并未亲眼看见陆照临。'
        before = copy.deepcopy(self.context['parent']['branchState'])
        for basis in ('reported', 'observed', None):
            with self.subTest(basis=basis):
                self.data['currentScene'] = {**scene('器物房', [GU, LU]), 'basis': basis}
                gateway = self.gateway()
                result, _ = self.generate(gateway)
                state = self.commit(result)
                self.assertEqual(state['characterLocationIds'], before['characterLocationIds'])
                self.assertEqual(result['consequenceUpdate']['introductions']['locations'], [])
                self.assertTrue(result['observationDiagnostics'])
                self.assertEqual(result['narrativeText'], self.body)
                self.assertEqual(gateway.complete_text.call_count, 1)

    def test_each_person_needs_own_present_evidence_and_valid_person_still_moves(self):
        self.body = '你走进器物房，在案边停下。\n\n陆照临并不在屋里。'
        self.data['currentScene'] = scene('器物房', [GU, LU])
        for refs in (['P1'], ['P2']):
            self.data['currentScene']['presentEntities'][1]['paragraphIds'] = refs
            result, _ = self.generate(self.gateway())
            state = self.commit(result)
            self.assertNotEqual(state['playerLocationId'], self.context['parent']['branchState']['playerLocationId'])
            self.assertEqual(state['characterLocationIds'][LU], self.context['parent']['branchState']['characterLocationIds'][LU])
            self.assertTrue(result['observationDiagnostics'])

    def test_omitted_or_copied_identity_cannot_turn_near_name_rumor_into_death(self):
        father = next(c for c in self.fixture.package['characters'] if c['name'] == '陆沉舟')
        self.body = '传话人说：“一个自称陆沉的人已经死了。”这只是传闻，身份尚未核实。'
        for extra in ({}, {'entityName': '陆沉舟', 'basis': 'observed'},
                      {'entityName': '陆沉', 'basis': 'observed'}, {'basis': 'reported'}):
            self.data['updates'] = dict(outcomes=[dict(characterId=father['id'], status='dead',
                cause='转述陆沉死亡', paragraphIds=['P1'], **extra)])
            gateway = self.gateway()
            result, _ = self.generate(gateway)
            state = self.commit(result)
            self.assertNotIn(father['id'], state['characterOutcomeStates'])
            self.assertTrue(result['continuityFollowups'])
            self.assertEqual(result['narrativeText'], self.body)
            self.assertEqual(gateway.complete_text.call_count, 1)

    def test_copied_full_name_without_literal_identity_is_deferred(self):
        self.body = '陆照倒在地上，已经死亡。'
        self.data['updates'] = dict(outcomes=[dict(characterId=LU, entityName='陆照临', basis='observed',
            status='dead', cause='已死亡', paragraphIds=['P1'])])
        result, _ = self.generate(self.gateway())
        self.assertNotIn(LU, self.commit(result)['characterOutcomeStates'])
        self.assertTrue(result['observationDiagnostics'])

    def test_missing_basis_does_not_implicitly_mean_observed(self):
        self.body = '陆照临倒在地上，已经死亡。'
        self.data['updates'] = dict(outcomes=[dict(characterId=LU, entityName='陆照临',
            status='dead', cause='已死亡', paragraphIds=['P1'])])
        result, _ = self.generate(self.gateway())
        self.assertNotIn(LU, self.commit(result)['characterOutcomeStates'])

    def test_literal_quote_can_isolate_actual_action_from_unrelated_hearsay(self):
        self.body = '有人说：“听说雨要停了。”你把药瓶交给陆照临，他接过收好。'
        item = self.fixture.package['items'][0]['id']
        change = dict(entityId=item, entityName='药瓶', basis='observed', attribute='ownerCharacterId',
            value=LU, reason='当面交接', paragraphIds=['P1'], evidenceQuote='你把药瓶交给陆照临，他接过收好。')
        self.data['updates'] = dict(stateChanges=[change])
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['itemOwnerCharacterIds'][item], LU)
        change['evidenceQuote'] = '你将药瓶递给陆照临。'
        result, _ = self.generate(self.gateway())
        self.assertEqual(result['consequenceUpdate']['stateChanges'], [])
        self.assertEqual(result['narrativeText'], self.body)

    def test_legacy_receipt_replays_old_projection_without_changing_saved_history(self):
        self.body = '你和陆照临走进器物房。'
        self.data['currentScene'] = dict(name='器物房', paragraphIds=['P1'], presentEntityIds=[GU, LU])
        old = delivery.observe(self.context, self.body, self.data, legacy=True)
        result = dict(narrativeText=self.body, actionIntent={'input': self.action},
            consequenceUpdate=old['update'], readerOutcome=old['outcome'], continuityFollowups=old['followups'],
            deliveryReceipt={**delivery.seal(self.context, self.body, self.data), 'version': delivery.LEGACY_VERSION})
        before = copy.deepcopy(result)
        delivery.validate_commit(self.context, result)
        self.assertEqual(result, before)
        result['deliveryReceipt']['version'] = delivery.VERSION
        with self.assertRaises(ValueError):
            delivery.validate_commit(self.context, result)

    def test_short_quote_cannot_strip_reporter_or_quotation_marks(self):
        for body in ('书办说：“陆照临已经死亡。”', '据说陆照临已经死亡。'):
            self.body = body
            self.data['updates'] = dict(outcomes=[dict(characterId=LU, entityName='陆照临', basis='observed',
                status='dead', cause='死亡', paragraphIds=['P1'], evidenceQuote='陆照临已经死亡。')])
            result, _ = self.generate(self.gateway())
            self.assertNotIn(LU, self.commit(result)['characterOutcomeStates'])
            self.assertTrue(result['observationDiagnostics'])
            self.assertEqual(result['narrativeText'], body)

    def test_actual_arrival_is_not_lost_to_unrelated_dialogue_in_same_paragraph(self):
        self.body = '门房的门被推开，一股暖气扑出来。你把伤者放到门内长凳上，中年人回头说：“去叫医修。”'
        self.data['currentScene'] = scene('门房', [GU])
        self.data['currentScene']['presentEntities'][0]['evidenceQuote'] = '你把伤者放到门内长凳上'
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        self.assertNotEqual(state['playerLocationId'], self.context['parent']['branchState']['playerLocationId'])
        self.assertEqual(result['observationDiagnostics'], [])
        self.assertEqual(result['narrativeText'], self.body)
        # Preserve the intermediate v2 receipt's original projection as well.
        old = delivery.observe(self.context, self.body, self.data, literal_scope=False)
        self.assertEqual(old['update']['stateChanges'], [])
        saved = {**result, 'consequenceUpdate': old['update'], 'readerOutcome': old['outcome'],
                 'continuityFollowups': old['followups'],
                 'deliveryReceipt': {**result['deliveryReceipt'], 'version': 'narrative-delivery/2'}}
        delivery.validate_commit(self.context, saved)

    def test_destination_in_dialogue_does_not_mean_arrival(self):
        place = next(p for p in self.fixture.package['locations']
                     if p['id'] != self.context['parent']['branchState']['playerLocationId'])
        self.body = '“你今晚只能歇在' + place['name'] + '。”执事把文书还给你。\n\n你仍站在线外。'
        self.data['confirmedStates'] = [dict(entityId=GU, entityName='你', basis='observed',
            attribute='locationId', value=place['id'], observedLocationName=place['name'],
            reason='执事指定今晚住处', paragraphIds=['P1'])]
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['playerLocationId'],
                         self.context['parent']['branchState']['playerLocationId'])
        self.assertTrue(result['observationDiagnostics'])
        self.assertEqual(result['narrativeText'], self.body)
        old = delivery.observe(self.context, self.body, self.data, narrated_location=False)
        self.assertEqual(old['update']['stateChanges'][0]['value'], place['id'])
        saved = {**result, 'consequenceUpdate': old['update'], 'readerOutcome': old['outcome'],
                 'continuityFollowups': old['followups'],
                 'deliveryReceipt': {**result['deliveryReceipt'], 'version': 'narrative-delivery/3'}}
        delivery.validate_commit(self.context, saved)
        self.data['currentScene'] = scene(place['name'], [GU])
        result, _ = self.generate(self.gateway())
        self.assertEqual(result['consequenceUpdate']['stateChanges'], [])
        # A subsequent actual arrival is recorded without rejecting the dialogue.
        self.body += '\n\n你随后走进' + place['name'] + '，在桌边坐下。'
        self.data.pop('currentScene')
        self.data['confirmedStates'][0]['paragraphIds'] = ['P3']
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['playerLocationId'], place['id'])

    def test_contract_binds_player_you_without_promoting_other_near_names(self):
        self.body = '你进入门洞，在石凳上坐下。'
        self.data['currentScene'] = scene('门洞', [GU])
        self.data['currentScene']['presentEntities'][0].update(
            entityName='顾长离', evidenceQuote='你进入门洞，在石凳上坐下。')
        before = copy.deepcopy(self.data)
        result, _ = self.generate(self.gateway())
        state = self.commit(result)
        self.assertNotEqual(state['playerLocationId'], self.context['parent']['branchState']['playerLocationId'])
        self.assertEqual(result['observationDiagnostics'], [])
        self.assertEqual(self.data, before)
        self.assertEqual(result['deliveryReceipt']['data']['currentScene']['presentEntities'][0]['entityName'], '你')
        self.data['currentScene']['presentEntities'][0].update(entityId=LU, entityName='陆照临')
        result, _ = self.generate(self.gateway())
        self.assertEqual(self.commit(result)['characterLocationIds'][LU],
                         self.context['parent']['branchState']['characterLocationIds'][LU])


if __name__ == '__main__':
    unittest.main()
