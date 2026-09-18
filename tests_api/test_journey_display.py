"""Journal progress must have a denominator; graph links need public evidence."""
import copy
import unittest
from unittest.mock import Mock, patch

from open_story_engine.api_journey import _mock_fixture_terminal, journey
from open_story_engine.api_journey import route_health
from open_story_engine.api_relationships import known_relationships


class JourneyDisplayTests(unittest.TestCase):
    def test_mock_fixture_terminal_requires_authored_terminal_turn(self):
        package = {'id': 'taixu-relics-part1'}
        terminal_node = {
            'planning': {'narrativeOrigin': 'mock_structural_fixture'},
            'branchState': {'freeTextProgress': 4},
        }
        self.assertTrue(_mock_fixture_terminal(package, [terminal_node]))
        self.assertFalse(_mock_fixture_terminal(package, [{
            **terminal_node, 'branchState': {'freeTextProgress': 3},
        }]))
        self.assertFalse(_mock_fixture_terminal(package, [{
            **terminal_node, 'planning': {},
        }]))

    def test_route_health_exposes_source_progress_stall_streak(self):
        store = Mock()
        store.contract.return_value = {}
        with patch('open_story_engine.route_monitor.monitor', return_value={
                'signals': ['source_progress_stalled'], 'review_recommended': True,
                'window': 3, 'unchanged_state_turns': 0, 'repeated_action_turns': 0,
                'source_progress_streak': 4}), \
                patch('open_story_engine.route_closure.preparation', return_value={
                    'readiness': 'checklist_clear', 'outstanding_count': 0}), \
                patch('open_story_engine.route_lifecycle.view', return_value={'ending_written': False}):
            result = route_health(store, 'session', [{'id': 'root'}], {}, 'active')
        self.assertEqual(result['signals'], ['source_progress_stalled'])
        self.assertEqual(result['source_progress_streak'], 4)
        self.assertTrue(result['review_recommended'])

    def test_branch_location_goals_and_clues_do_not_leak_into_sibling(self):
        package = {'id': 'book', 'characters': [], 'locations': [dict(id='gate', name='山门')],
                   'story': {'entryModel': {'policy': 'official_unknown_reader/1',
                       'entryPoints': [dict(id='entry', openingThreads=['调查旧线索'])]}}}
        root = dict(id='root', sequence=0, narrativeText='你站在山门。', openingClues=['木牌上有金屑。'],
                    branchState={'playerLocationId': 'gate'})
        goals = [dict(id='old', title='调查旧线索', status='abandoned'),
                 dict(id='new', title='寻找落脚处', status='active')]
        quote = '你来到临时石屋，决定放下旧线索，先寻找落脚处。'
        child = dict(id='child', sequence=1, narrativeText=quote,
                     branchState={'playerLocationId': 'shelter', 'derivedLocations': [dict(id='shelter', name='临时石屋')],
                                  'goalLedger': goals},
                     readerOutcome={'clues': [dict(summary='石屋可以避雨。', evidence=quote)]})
        sibling = dict(id='sibling', sequence=2, narrativeText='你仍留在山门。', branchState=root['branchState'])
        before = copy.deepcopy([root, child, sibling])
        store = Mock()
        store.contract.return_value = {'persona': {'name': '旅人'}, 'entryPointId': 'entry'}
        with patch('open_story_engine.api_journey.player_beat', return_value=None), \
                patch('open_story_engine.api_journey.preferences', return_value={'ended': {}}):
            for nodes in ([root, child], [root, sibling], [root, child]):
                store.lineage.return_value = nodes
                result = journey(store, 'session', nodes[-1]['id'], package)
                changed = nodes[-1]['id'] == 'child'
                self.assertEqual(result['location'], '临时石屋' if changed else '山门')
                self.assertEqual(result['goal'], '寻找落脚处' if changed else '调查旧线索')
                self.assertEqual('石屋可以避雨。' in result['clues'], changed)
                self.assertIn('木牌上有金屑。', result['clues'])  # Historical clue survives goal abandonment.
                self.assertEqual(result['lineage'], ['root', nodes[-1]['id']])
                if changed:
                    self.assertEqual(result['feedback'], ['来到临时石屋', '目标已放下：调查旧线索'])
        self.assertEqual([root, child, sibling], before)

    def test_open_route_counts_selected_choices_without_inventing_percentage(self):
        package = {'id': 'long-novel', 'characters': [], 'locations': [],
                   'story': {'narrativeGraph': {'endingBeatIds': {'end': 'last'}}}}
        root = {'id': 'root', 'sequence': 0, 'narrativeText': '风停了。', 'branchState': {}}
        first = {**root, 'id': 'first', 'sequence': 12}
        second = {**root, 'id': 'second', 'sequence': 39}
        selection = {**root, 'id': 'arc', 'kind': 'arc_selection', 'sequence': 15}
        store = Mock()
        store.contract.return_value = {'persona': {'name': '旅人'}}
        with patch('open_story_engine.api_journey.player_beat', return_value=None), \
                patch('open_story_engine.api_journey.preferences', return_value={'ended': {}}):
            for nodes, expected in [([root], 0), ([root, first], 1),
                                    ([root, first, selection, second], 2), ([root, second], 1)]:
                store.lineage.return_value = nodes
                result = journey(store, 'session', nodes[-1]['id'], package)
                self.assertIsNone(result['progress'])
                self.assertEqual(result['choices_made'], expected)
                self.assertEqual(result['status'], 'active')
                self.assertFalse(result['route_health']['ending_written'])
            store.lineage.return_value = [{**root, 'branchState': {'storyScope': 'source'}}]
            with patch('open_story_engine.api_journey.player_beat', return_value={'id': 'last'}):
                result = journey(store, 'session', 'root', package)
                self.assertEqual(result['progress'], 100)
                self.assertEqual(result['status'], 'completed')
                self.assertFalse(result['route_health']['ending_written'])

    def test_source_route_progress_uses_continuous_graph_denominator(self):
        beats = [
            {'id': 'beat_chapter_001', 'branchState': {'sourceProgress': 'chapter_001'}},
            {'id': 'beat_chapter_002', 'branchState': {'sourceProgress': 'chapter_002'}},
            {'id': 'beat_chapter_003', 'branchState': {'sourceProgress': 'chapter_003'}},
        ]
        package = {'id': 'long-novel', 'characters': [], 'locations': [],
                   'story': {'narrativeGraph': {'beats': beats,
                       'endingBeatIds': {'end': 'beat_chapter_003'}}}}
        store = Mock()
        store.contract.return_value = {'persona': {'name': '旅人'}}
        with patch('open_story_engine.api_journey.preferences', return_value={'ended': {}}):
            for index, expected in enumerate((0, 50, 100)):
                node = {'id': 'node-' + str(index), 'sequence': index,
                        'narrativeText': '你继续前行。',
                        'branchState': {'storyScope': 'source',
                                        'sourceProgress': f'chapter_{index + 1:03d}'}}
                store.lineage.return_value = [node]
                with patch('open_story_engine.api_journey.player_beat', return_value=beats[index]):
                    result = journey(store, 'session', node['id'], package)
                self.assertEqual(result['progress'], expected)
                self.assertEqual(result['progress_label'], '原著路线进度')
                self.assertEqual(result['status'], 'completed' if expected == 100 else 'active')
                self.assertIn('不表示自然结局已写成', result['route_health']['note'])
        gap_beats = [
            {'id': 'beat_chapter_001', 'branchState': {'sourceProgress': 'chapter_001'}},
            {'id': 'beat_chapter_003', 'branchState': {'sourceProgress': 'chapter_003'}},
        ]
        gap_package = {'id': 'gapped-novel', 'characters': [], 'locations': [],
                       'story': {'narrativeGraph': {'beats': gap_beats,
                           'endingBeatIds': {'end': 'beat_chapter_003'}}}}
        gap_node = {'id': 'gap', 'sequence': 0, 'narrativeText': '你继续前行。',
                    'branchState': {'storyScope': 'source', 'sourceProgress': 'chapter_001'}}
        store.lineage.return_value = [gap_node]
        with patch('open_story_engine.api_journey.preferences', return_value={'ended': {}}), \
                patch('open_story_engine.api_journey.player_beat', return_value=gap_beats[0]):
            result = journey(store, 'session', gap_node['id'], gap_package)
        self.assertIsNone(result['progress'])
        self.assertEqual(result['progress_label'], '本路线历程')

    def test_opening_links_only_admitted_characters_and_keeps_scope_local(self):
        people = [{'id': 'gu', 'name': '顾长离', 'first_page': 1},
                  {'id': 'lu', 'name': '陆照临', 'first_page': 1},
                  {'id': 'ye', 'name': '叶观澜', 'first_page': 8}]
        root = {'sequence': 0, 'narrativeText': '你报上名字。对方叫陆照临。',
                'openingContext': {'relationships': [
                    {'name': '陆照临', 'relation': '刚认识姓名，没有共同调查。'},
                    {'name': '叶观澜', 'relation': '尚未相遇。'},
                    {'name': '顾长离', 'relation': '本人。'}]}}
        links = known_relationships([root], people, '顾长离')
        self.assertEqual(len(links), 1)
        self.assertEqual((links[0]['source'], links[0]['target'], links[0]['origin']), ('gu', 'lu', 'opening'))
        self.assertEqual(links[0]['label'], '刚认识姓名')
        self.assertEqual(known_relationships([{**root, 'openingContext': {}}], people, '顾长离'), [])
        self.assertEqual(known_relationships([{**root, 'narrativeText': '风停了。'}], people, '顾长离'), [])

    def test_newer_relationship_wins_over_old_reviewed_note(self):
        people = [{'id': 'gu', 'name': '顾长离', 'first_page': 1}, {'id': 'lu', 'name': '陆照临', 'first_page': 1}]
        root = {'sequence': 0, 'narrativeText': '你告诉陆照临此事。', 'readerOutcome': {'relationships': [
            {'source': '顾长离', 'target': '陆照临', 'label': '交谈', 'evidence': '你告诉陆照临此事。'}]}}
        later = {'sequence': 1, 'narrativeText': '你质问陆照临。'}
        links = known_relationships([root, later], people, '顾长离')
        self.assertEqual(links[0]['label'], '交锋')
        self.assertEqual(links[0]['page'], 2)
        self.assertEqual(known_relationships([root], people, '顾长离')[0]['label'], '交谈')

    def test_recap_effects_include_recorded_state_changes(self):
        package = {'id': 'book', 'characters': [
                       {'id': 'player', 'name': '顾长离', 'menuDescription': '试炼者'},
                       {'id': 'lu', 'name': '陆照临', 'menuDescription': '同行者'},
                       {'id': 'hidden', 'name': '沈砚秋', 'menuDescription': '未公开人物'},
                   ], 'items': [{'id': 'token', 'name': '旧令牌'}], 'locations': [],
                   'story': {'entryModel': {'policy': 'official_unknown_reader/1',
                       'entryPoints': [dict(id='entry', openingThreads=['查清旧令牌'])]}}}
        root = {'id': 'root', 'sequence': 0, 'narrativeText': '你与陆照临站在门前。',
                'branchState': {'characterOutcomeStates': {}, 'readerEntityStates': {},
                                'goalLedger': [dict(id='goal', title='查清旧令牌', status='active')],
                                'threadLedger': [dict(id='thread', title='门后是谁', status='open')]}}
        quote = '陆照临受伤，旧令牌已经永久损毁。你解决了门后是谁的问题。'
        child = {'id': 'child', 'sequence': 1, 'narrativeText': quote,
                 'branchState': {'characterOutcomeStates': {'lu': {'status': 'injured', 'permanence': 'temporary'},
                                                            'hidden': {'status': 'dead', 'permanence': 'permanent'}},
                                 'readerEntityStates': {'token': {'destroyedPermanently': True}},
                                 'goalLedger': [dict(id='goal', title='查清旧令牌', status='completed')],
                                 'threadLedger': [dict(id='thread', title='门后是谁', status='resolved')]}}
        store = Mock()
        store.contract.return_value = {'persona': {'name': '顾长离'}}
        with patch('open_story_engine.api_journey.player_beat', return_value=None), \
                patch('open_story_engine.api_journey.preferences', return_value={'ended': {}}):
            store.lineage.return_value = [root, child]
            result = journey(store, 'session', 'child', package)
        self.assertEqual(result['feedback'], ['陆照临已受伤', '旧令牌已永久损毁'])
        self.assertEqual(result['recap'][0]['effects'], [
            '陆照临已受伤', '旧令牌已永久损毁', '目标已完成：查清旧令牌', '问题已解决：门后是谁',
        ])
        self.assertNotIn('沈砚秋', str(result['recap']))

    def test_ledger_coverage_exposes_missing_or_invalid_status_without_inference(self):
        package = {'id': 'book', 'characters': [], 'locations': [],
                   'story': {'entryModel': {'policy': 'official_unknown_reader/1',
                       'entryPoints': [dict(id='entry', openingThreads=['门后是谁'])]}}}
        node = {'id': 'root', 'sequence': 0, 'narrativeText': '你站在门前。',
                'branchState': {'goalLedger': 'invalid', 'threadLedger': 'invalid'}}
        store = Mock()
        store.contract.return_value = {'persona': {'name': '旅人'}, 'entryPointId': 'entry'}
        with patch('open_story_engine.api_journey.player_beat', return_value=None), \
                patch('open_story_engine.api_journey.preferences', return_value={'ended': {}}):
            store.lineage.return_value = [node]
            result = journey(store, 'session', node['id'], package)
        self.assertEqual(result['ledger_coverage'], {'goals': 'unknown', 'threads': 'unknown'})
        self.assertEqual(result['goals'], [])
        self.assertEqual(result['threads'], [])

        legacy = {**node, 'branchState': {}}
        store.lineage.return_value = [legacy]
        with patch('open_story_engine.api_journey.player_beat', return_value=None), \
                patch('open_story_engine.api_journey.preferences', return_value={'ended': {}}):
            result = journey(store, 'session', legacy['id'], package)
        self.assertEqual(result['ledger_coverage'], {'goals': 'unknown', 'threads': 'unknown'})
        self.assertEqual(result['goals'][0]['status'], 'active')
        self.assertEqual(result['threads'][0]['status'], 'unknown')
