"""Contact prerequisites, saved menus and real-response replay on official novels."""
import copy
import hashlib
import json
from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import patch

from open_story_engine import reader_consequences as rc
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadError
from open_story_engine.api_turn_drafts import visible_choices
from open_story_engine.reader_choices import choice_context, validate_choices, restored_choices, DEPENDENCY_VERSION
from open_story_engine.storage import SessionStore
from test_support.longform import ROOT, longform_cases
from test_support.live_item_dependencies import build_cases
from tests_api.test_longform_character_status import LongformCharacterStatusTests


class LongformChoiceAvailabilityTests(unittest.TestCase):
    result = LongformCharacterStatusTests.result

    def setUp(self):
        LongformCharacterStatusTests.setUp(self)
        public = self.menu_context(self.context['parent'])['people']
        cid = next(p['id'] for p in public if p['id'] != self.cid)
        self.target = next(c for c in self.context['package']['characters'] if c['id'] == cid)

    def menu_context(self, node, history=None):
        return choice_context(self.context['package'], self.context['contract'],
                              history or self.context['lineage'], node)

    def person(self, node):
        return next(p for p in self.menu_context(node)['people'] if p['id'] == self.target['id'])

    def option(self):
        return dict(title='询问当前情况', action='我向' + self.target['name'] + '询问眼前情况，等他答复后再决定。',
                    paragraphIds=['P1'], interactWith=[self.target['id']], mentionOnly=[], useItems=[])

    def commit(self, bid, status='alive', location=None):
        body = self.target['name'] + ('已确认失踪，去向不明。' if status == 'missing' else '已确认仍然存活。')
        if location:
            place = next(p['name'] for p in self.context['package']['locations'] if p['id'] == location)
            body += self.target['name'] + '现在站在' + place + '。'
        result = self.result(status, body)
        if location:
            change = dict(id='C1', entityId=self.target['id'], attribute='locationId',
                          before=self.context['parent']['branchState']['characterLocationIds'].get(self.target['id']),
                          value=location, stepId='S1', reason='当前公开位置', evidence=body)
            result['consequenceUpdate']['stateChanges'] = [change]
            result['authorityReview']['stateChecks'] = [dict(changeId='C1', authorized=True)]
            result['observedEvents'][0]['changes'].append(dict(entityId=self.target['id'], attribute='locationId', value=location))
            result['eventChecks'][0]['changeIds'] = ['C1']
        state = rc.commit_consequences(self.context, self.context['parent']['branchState'], result, bid)
        return {**self.context['parent'], **result, 'id': bid, 'kind': 'generated', 'branchState': state,
                'sequence': self.context['parent']['sequence'] + 1, 'parentId': self.context['parent']['id'],
                'selectedDirectionId': 'contact-action', 'playerDirection': self.context['playerDirection'],
                'selectedDirection': {'id': 'contact-action', 'isFreeText': True, 'statePatch': {}}}

    def advance(self, node):
        self.context = {**self.context, 'parent': node, 'lineage': [*self.context['lineage'], node]}

    def test_every_official_opening_uses_registered_locations_without_inventing_status(self):
        package = self.context['package']
        for entry in package['story']['entryModel']['entryPoints']:
            cid = entry['sourceCharacterIds'][0]
            start = self.play.create_session(self.case['package_id'], self.case['version'], entry['id'], cid, identity_opening=True)
            _, snap = self.play._turn_snapshot(start['session']['id'], start['branch']['id'])
            node = snap.history[-1]
            before = copy.deepcopy(node)
            context = choice_context(snap.package, snap.story_contract, snap.history, node)
            for person in context['people']:
                expected = entry['openingState']['characterLocationIds'].get(person['id']) == entry['openingState']['playerLocationId']
                self.assertEqual(person['available'], expected)
                self.assertEqual(person['status'], 'unknown')
            self.assertEqual(node, before)

    def test_opening_ground_items_are_public_without_revealing_other_catalog_props(self):
        package = self.context['package']
        for entry in package['story']['entryModel']['entryPoints']:
            cid = entry['sourceCharacterIds'][0]
            start = self.play.create_session(self.case['package_id'], self.case['version'], entry['id'], cid, identity_opening=True)
            _, snap = self.play._turn_snapshot(start['session']['id'], start['branch']['id'])
            node = snap.history[-1]
            state = node['branchState']
            context = choice_context(snap.package, snap.story_contract, snap.history, node)
            expected = {iid for iid, place in entry['openingState']['itemLocationIds'].items()
                        if place == state['playerLocationId']}
            expected.update(iid for iid, owner in state['itemOwnerCharacterIds'].items() if owner == cid)
            self.assertEqual({i['id'] for i in context['items']}, expected)
            hidden = next(i for i in package['items'] if i['id'] not in expected)
            forged = copy.deepcopy(node)
            forged['branchState']['itemLocationIds'][hidden['id']] = state['playerLocationId']
            self.assertNotIn(hidden['id'], {i['id'] for i in choice_context(snap.package, snap.story_contract, [forged], forged)['items']})
            if not entry['openingState']['itemLocationIds']:
                continue
            iid = next(iter(entry['openingState']['itemLocationIds']))
            action = dict(title='查看地面物品', action='我留在原地拾起眼前的物品，查看表面后再决定下一步。',
                          paragraphIds=['P1'], interactWith=[], mentionOnly=[], useItems=[iid])
            saved = validate_choices({'choices': [action]}, context, snap.package)
            changed = copy.deepcopy(node)
            changed['branchState']['itemLocationIds'].pop(iid)
            changed['branchState']['readerEntityStates'] = {iid: {'destroyedPermanently': True}}
            changed_context = choice_context(snap.package, snap.story_contract, snap.history, changed)
            item = next(i for i in changed_context['items'] if i['id'] == iid)
            self.assertEqual(item['locationId'], 'unknown')
            self.assertTrue(item['state']['destroyedPermanently'])
            self.assertEqual(restored_choices(saved, changed_context, snap.package), [])
            with self.assertRaises(ValueError):
                validate_choices({'choices': [action]}, changed_context, snap.package)
            old_context = copy.deepcopy(context)
            old_context['items'] = [i for i in old_context['items'] if i['id'] not in entry['openingState']['itemLocationIds']]
            old_menu = validate_choices({'choices': [dict(action, useItems=[])]}, old_context, snap.package)
            self.assertEqual(restored_choices(old_menu, context, snap.package), [])
            self.assertEqual(choice_context(snap.package, snap.story_contract, snap.history, node), context)

    def test_unknown_or_remote_location_blocks_contact_but_allows_searching(self):
        root = self.context['parent']
        node = copy.deepcopy(root)
        state = node['branchState']
        tid = self.target['id']
        place = state['playerLocationId']
        for target_place, player_place in ((None, place), ('unknown', 'unknown'), (None, None),
                                            ('not-registered', 'not-registered'),
                                            (next(p['id'] for p in self.context['package']['locations'] if p['id'] != place), place)):
            state['characterLocationIds'][tid] = target_place
            state['playerLocationId'] = player_place
            person = self.person(node)
            self.assertFalse(person['available'])
            self.assertEqual(person['status'], 'unknown')
            self.assertNotIn('locationId', person)
            with self.assertRaises(ValueError):
                validate_choices({'choices': [self.option()]}, self.menu_context(node), self.context['package'])
        search = dict(self.option(), title='整理寻找线索',
                      action='我回想' + self.target['name'] + '留下的公开线索，尝试寻找他的去向，尚不假定找到本人。',
                      interactWith=[], mentionOnly=[tid])
        self.assertEqual(len(validate_choices({'choices': [search]}, self.menu_context(node), self.context['package'])), 1)
        state['playerLocationId'] = place
        state['characterLocationIds'][tid] = place
        self.assertTrue(self.person(node)['available'])
        for status in ('dead', 'departed', 'missing'):
            state['characterOutcomeStates'][tid] = dict(status=status, permanence='temporary')
            self.assertFalse(self.person(node)['available'])

    def test_recovery_requires_current_location_and_departure_invalidates_menu(self):
        root = copy.deepcopy(self.context['parent'])
        missing = self.commit('missing', status='missing')
        self.advance(missing)
        alive = self.commit('alive')
        self.assertFalse(self.person(alive)['available'])
        self.advance(alive)
        place = alive['branchState']['playerLocationId']
        nearby = self.commit('nearby', location=place)
        context = self.menu_context(nearby)
        menu = validate_choices({'choices': [self.option()]}, context, self.context['package'])
        self.assertTrue(self.person(nearby)['available'])
        self.advance(nearby)
        remote = self.commit('remote', location=next(p['id'] for p in self.context['package']['locations'] if p['id'] != place))
        self.assertFalse(self.person(remote)['available'])
        self.assertEqual(restored_choices(menu, self.menu_context(remote), self.context['package']), [])
        self.assertEqual(self.context['lineage'][0], root)

    def test_saved_menu_restart_and_legacy_click_checks_without_generation(self):
        nearby = self.commit('nearby-saved', location=self.context['parent']['branchState']['playerLocationId'])
        context = self.menu_context(nearby)
        nearby['readerChoices'] = validate_choices({'choices': [self.option()]}, context, self.context['package'])
        original = copy.deepcopy(nearby)
        with closing(SessionStore(str(self.read.database_path))) as store:
            store.append_branch(self.sid, self.parent, nearby)
        self.play.drafts.close()
        self.play = PlayService(self.read, Path(self.temp))
        self.addCleanup(self.play.drafts.close)
        _, snap = self.play._turn_snapshot(self.sid, nearby['id'])
        self.assertEqual(len(visible_choices(snap.history[-1], snap.package, snap.story_contract, snap.history)), 1)
        self.assertFalse(self.person(self.context['parent'])['available'])
        legacy = copy.deepcopy(nearby)
        legacy['readerChoices'][0]['dependencies']['version'] = 'reader-choice-dependencies/2'
        with closing(SessionStore(str(self.read.database_path))) as store:
            with store.connection:
                store.connection.execute('UPDATE branch_nodes SET node_json=? WHERE id=?',
                                         (json.dumps(legacy, ensure_ascii=False), nearby['id']))
        with patch.object(self.play.drafts, 'ensure', side_effect=AssertionError('stale menu generated')):
            self.assertEqual(self.play.prepare_choices(self.sid, nearby['id'], 'restored')['choices'], [])
            with self.assertRaises(ReadError) as caught:
                self.play.continue_turn(self.sid, nearby['id'], choice_id=legacy['readerChoices'][0]['id'], request_id='old-click')
            self.assertEqual(caught.exception.code, 'choice_unavailable')
        self.assertEqual(nearby, original)
        self.assertEqual(nearby['readerChoices'][0]['dependencies']['version'], DEPENDENCY_VERSION)

    def test_saved_real_response_filters_unconfirmed_interaction_without_changing_evidence(self):
        fixtures = [json.loads(p.read_text()) for p in (ROOT / 'test_support/fixtures').glob('item-dependencies-*.json')]
        fixture = next(f for f in fixtures if f['package_id'] == self.case['package_id'])
        jobs = build_cases(self.case, fixture)
        context = json.loads(next(j['messages'][1]['content'] for j in jobs if j['id'] == 'choices_after_destruction'))
        path = ROOT / 'docs/evidence/live-item-dependencies-2026-09-17' / (self.case['package_id'] + '-choices_after_destruction.json')
        before = path.read_bytes()
        artifact = json.loads(before)
        raw = json.loads(artifact['response'])
        checked = validate_choices(raw, context, self.context['package'])
        blocked = {p['id'] for p in context['people'] if not p['available']}
        rejected = [c for c in raw['choices'] if blocked.intersection(c['interactWith'])]
        self.assertTrue(rejected)
        self.assertEqual(len(checked), len(raw['choices']) - len(rejected))
        self.assertTrue(all(not blocked.intersection(c['dependencies']['interactWith']) for c in checked))
        self.assertEqual(restored_choices(artifact['validated'], context, self.context['package']), [])
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(before).digest())


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformAvailability_' + case['package_id'],
                                                        (LongformChoiceAvailabilityTests,), {'case': case})))
    return suite
