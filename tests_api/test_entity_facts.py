import copy
import unittest

from open_story_engine.entity_facts import turn_entity_facts, check_observed_reference, observation_state, bind_new_entity_references
from tests_api import test_reader_consequences as fixtures


class EntityFactsTests(unittest.TestCase):
    def test_new_reference_binding_does_not_guess_aliases_or_widen_short_quotes(self):
        body = '伤者躺在石阶下。\n\n他的呼吸已经停止。'
        base = dict(updates=dict(introductions=dict(characters=[dict(id='character_new', name='伤者', paragraphIds=['P1'])]),
                                 outcomes=[dict(characterId='character_new', entityName='伤者', paragraphIds=['P2'])]))
        cases = []
        quoted = copy.deepcopy(base)
        quoted['updates']['outcomes'][0]['evidenceQuote'] = '他的呼吸已经停止。'
        cases.append((quoted, set()))
        alias = copy.deepcopy(base)
        alias['updates']['introductions']['characters'][0]['name'] = '陌生伤者'
        cases.append((alias, set()))
        cases.append((base, {'character_new'}))
        duplicate = copy.deepcopy(base)
        duplicate['updates']['introductions']['characters'].append(dict(id='character_other', name='伤者', paragraphIds=['P1']))
        cases.append((duplicate, set()))
        fabricated = copy.deepcopy(base)
        fabricated['updates']['introductions']['characters'][0]['paragraphIds'] = ['P99']
        cases.append((fabricated, set()))
        for data, existing in cases:
            with self.subTest(data=data, existing=existing):
                original = copy.deepcopy(data)
                self.assertEqual(bind_new_entity_references(data, body, existing), original)
                self.assertEqual(data, original)

    def setUp(self):
        fixture = fixtures.ConsequenceTests()
        fixture.setUp()
        self.package = fixture.package
        self.state = copy.deepcopy(fixture.root['branchState'])
        self.father = next(c for c in self.package['characters'] if c['name'] == '陆沉舟')
        self.visible = dict(characters=[self.father], locations=[], items=[])

    def test_partial_name_is_candidate_only_and_other_source_entities_stay_hidden(self):
        original = copy.deepcopy(self.state)
        result = turn_entity_facts(self.visible, self.state, '核对“陆沉”是否是父亲', '')
        card = result['entities'][0]
        self.assertEqual(card['id'], self.father['id'])
        self.assertEqual(card['unconfirmedMentions'], ['陆沉'])
        self.assertNotIn('aliases', card)
        self.assertEqual({e['id'] for e in result['entities']}, {self.father['id']})
        self.assertEqual(self.state, original)
        unquoted = turn_entity_facts(self.visible, self.state, '随行为陆沉，是否就是父亲？')
        self.assertEqual(unquoted['entities'][0]['unconfirmedMentions'], ['陆沉'])
        full = turn_entity_facts(self.visible, self.state, '查陆沉舟的事')
        self.assertNotIn('unconfirmedMentions', full['entities'][0])

    def test_unrelated_entities_are_not_padding_and_branch_places_remain_distinct(self):
        self.state['derivedLocations'] = [dict(id='location_qiwu', name='器物房', summary='已到达的房间'),
                                          dict(id='location_old', name='前院偏厅', summary='先前地点')]
        result = turn_entity_facts(self.visible, self.state, '回器物房', '')
        self.assertEqual([e['id'] for e in result['entities']], ['location_qiwu'])
        self.assertEqual(turn_entity_facts(self.visible, self.state, '看天气')['entities'], [])
        self.assertEqual(turn_entity_facts(self.visible, self.state, '器物房前院偏厅', limit=1)['entities'][0]['id'], 'location_qiwu')

    def test_observed_place_must_be_quoted_and_match_its_own_registry_entry(self):
        known = {'location_qiwu': dict(name='器物房', kind='location')}
        item = dict(attribute='locationId', value='location_qiwu', observedLocationName='器物房')
        check_observed_reference(item, known, '你走进器物房。')
        with self.assertRaises(ValueError):
            check_observed_reference(item, known, '你仍在前院偏厅。')

    def test_observation_state_does_not_expand_to_unrelated_history_or_entities(self):
        state = dict(playerLocationId='place', readerEntityStates={'a': {'clue': 'known'}, 'b': {'secret': 'hidden'}},
                     branchLedger={'old': 'large history'}, goalLedger=[{'old': 'irrelevant'}])
        before = copy.deepcopy(state)
        scoped = observation_state(state, {'a'})
        self.assertEqual(scoped['readerEntityStates'], {'a': {'clue': 'known'}})
        self.assertNotIn('branchLedger', scoped)
        self.assertNotIn('goalLedger', scoped)
        self.assertEqual(state, before)
