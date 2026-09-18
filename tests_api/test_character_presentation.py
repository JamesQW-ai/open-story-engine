"""Known identity, public evidence, and outcome projection on one lineage."""
import copy
import unittest

from open_story_engine.api_journey import character_card, character_appears
from open_story_engine.character_presentation import character_presentation, known_status, portrait


class CharacterPresentationTests(unittest.TestCase):
    def setUp(self):
        self.character = {'id': 'character_lu', 'name': '陆照临',
                          'menuDescription': '追查父亲失踪与秘密印记的年轻人。',
                          'portraitAsset': '/images/taixu-lu-zhaolin-v1.png'}
        self.root = {'id': 'root', 'sequence': 0, 'narrativeText': '你站在试炼场。陆照临看向你。',
                     'branchState': {'characterOutcomeStates': {}}}

    def outcome(self, code='dead'):
        quote = '陆照临已经死亡。' if code == 'dead' else '陆照临已离队，此后不再同行。'
        update = {'characterId': self.character['id'], 'status': code,
                  'permanence': 'permanent', 'evidence': quote}
        return {'id': 'result', 'sequence': 1, 'narrativeText': quote,
                'consequenceUpdate': {'outcomes': [update]},
                'branchState': {'characterOutcomeStates': {self.character['id']: {**update, 'causeBranchId': 'result'}}}}

    def test_known_name_has_local_evidence_but_other_role_biography_is_hidden(self):
        card = character_card([self.root], self.character, '顾长离')
        result = character_presentation([self.root], self.character, card, 'book', character_appears)
        self.assertEqual(result['name'], '陆照临')
        self.assertNotIn('父亲', str(result))
        self.assertNotIn('秘密', str(result))
        self.assertEqual(result['name_evidence']['branch_id'], 'root')
        self.assertIn(result['name_evidence']['quote'], self.root['narrativeText'])
        self.assertEqual(result['portrait']['url'], self.character['portraitAsset'])
        self.assertEqual(result['status']['code'], 'unknown')

    def test_unseen_real_name_is_not_disclosed_by_alias_or_private_catalog(self):
        for prose in ('你看向蒙面人。', '你想起陆照临。', '如果陆照临来了，你会问他。'):
            node = {**self.root, 'narrativeText': prose}
            self.assertIsNone(character_card([node], self.character, '顾长离'))

    def test_reviewed_opening_name_and_public_outcome_admit_known_people(self):
        opening = {**self.root, 'narrativeText': '他叫陆照临。你们刚互通姓名。',
                   'openingContext': {'relationships': [{'name': '陆照临', 'relation': '刚认识'}]}}
        card = character_card([opening], self.character, '顾长离')
        shown = character_presentation([opening], self.character, card, 'book', character_appears)
        self.assertEqual(shown['first_page'], 1)
        self.assertEqual(shown['name_evidence']['quote'], opening['narrativeText'])
        hidden = {**opening, 'narrativeText': '你看着门外的雨。'}
        self.assertIsNone(character_card([hidden], self.character, '顾长离'))
        child = self.outcome()
        card = character_card([hidden, child], self.character, '顾长离')
        self.assertEqual(card['first_page'], 2)
        self.assertEqual(character_presentation([hidden, child], self.character, card, 'book', character_appears)['status']['code'], 'dead')

    def test_death_and_departure_persist_with_original_evidence(self):
        for code in ('dead', 'departed'):
            with self.subTest(code=code):
                child = self.outcome(code)
                later = {**copy.deepcopy(child), 'id': 'later', 'sequence': 2,
                         'narrativeText': '你留在原地。', 'consequenceUpdate': {}}
                result = known_status([self.root, child, later], self.character['id'])
                self.assertEqual(result['code'], code)
                self.assertEqual(result['permanence'], 'permanent')
                self.assertEqual(result['evidence']['branch_id'], 'result')
                self.assertEqual(result['evidence']['page'], 2)
                self.assertEqual(known_status([self.root], self.character['id'])['code'], 'unknown')

    def test_internal_state_future_branch_and_invalid_evidence_do_not_leak(self):
        child = self.outcome()
        for change in ('foreign_origin', 'unread_quote', 'unrecorded_consequence'):
            candidate = copy.deepcopy(child)
            record = candidate['branchState']['characterOutcomeStates'][self.character['id']]
            if change == 'foreign_origin':
                record['causeBranchId'] = 'sibling-or-future'
            elif change == 'unread_quote':
                record['evidence'] = '未向玩家展示的秘密死亡。'
            else:
                candidate['consequenceUpdate'] = {}
            with self.subTest(change=change):
                result = known_status([self.root, candidate], self.character['id'])
                self.assertEqual(result['code'], 'unknown')
                self.assertIsNone(result['evidence'])

    def test_absence_rumor_and_appearance_never_infer_outcome(self):
        for text in ('陆照临没有来。', '有人说：“陆照临已经死了。”', '陆照临仍站在门边。'):
            node = {**self.root, 'narrativeText': text,
                    'branchState': {'characterLocationIds': {}, 'readerEntityStates': {'character_lu': {'情况': '失踪'}}}}
            self.assertEqual(known_status([node], self.character['id'])['code'], 'unknown')

    def test_portrait_fallback_is_stable_and_does_not_accept_external_or_unsafe_paths(self):
        for asset in (None, 'https://example.com/secret.png', '/images/../private.png', '/images/a.png?secret=x'):
            with self.subTest(asset=asset):
                card = {**self.character, 'portraitAsset': asset}
                result = portrait(card, 'book')
                self.assertIsNone(result['url'])
                self.assertEqual(result['source'], 'preset')
                self.assertEqual(result['fallback_key'], portrait({**card, 'name': '蒙面人'}, 'book')['fallback_key'])
                self.assertRegex(result['fallback_key'], r'^person-[1-8]$')


if __name__ == '__main__':
    unittest.main()
