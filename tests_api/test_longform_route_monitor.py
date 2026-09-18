"""Route diagnostics use only committed ancestors of every official long novel."""
import copy
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.api_read import ReadService
from open_story_engine.branch_ledger import append_branch_ledger
from open_story_engine.storage import SessionStore
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests


class LongformRouteMonitorTests(unittest.TestCase):
    setUp = LongformRepairFallbackTests.setUp

    def append(self, parent=None, action='留在原地等待', change=None, body=None):
        parent = parent or self.parent
        with closing(SessionStore(str(self.read.database_path))) as store:
            old = store.branch(self.sid, parent)
            state = copy.deepcopy(old['branchState'])
            if change:
                change(state)
            node = dict(branchState=state, narrativeText=body or '\n\n'.join(self.paragraphs),
                        summary=action, nextDirections=[], sourceNodeRef=old['sourceNodeRef'],
                        selectedDirectionId='monitor-action', playerDirection=action,
                        selectedDirection=dict(id='monitor-action', isFreeText=True, statePatch={}))
            return store.append_branch(self.sid, parent, node)['id']

    def view(self, bid=None):
        return self.read.route_monitor(self.sid, bid or self.parent)

    def test_counters_and_events_do_not_hide_unchanged_state_or_repetition(self):
        bid = self.parent
        for i in range(3):
            def change(s, i=i):
                s.update(derivedTurn=i+1, freeTextProgress='turn-' + str(i))
                s['derivedEvents'].append(dict(id='event-' + str(i), summary='等待的记录'))
                append_branch_ledger(s, [dict(kind='event', operation='added', entityId='event-' + str(i),
                                             summary='等待的记录', before=None, after=i)],
                                     {'kind': 'player_direction', 'ref': 'wait-' + str(i)})
            bid = self.append(bid, change=change)
        result = self.view(bid)
        self.assertEqual(result['turns'], 3)
        self.assertEqual(result['unchanged_state_turns'], 3)
        self.assertEqual(result['repeated_action_turns'], 3)
        self.assertEqual(set(result['signals']), {'unchanged_tracked_state', 'repeated_action', 'repeated_body', 'source_progress_stalled'})
        self.assertTrue(result['review_recommended'])
        self.assertEqual(result['source_progress_streak'], 3)
        self.assertEqual(result['recent_turns'][-1]['repeated_body_from'], result['recent_turns'][0]['branch_id'])
        # A signal does not mutate or stop a deliberate waiting route.
        self.assertEqual(self.read.journey(self.sid, bid)['status'], 'active')

    def test_material_state_changes_reset_only_state_signal(self):
        bid = self.append(self.append(self.append()))
        base = self.view(bid)
        self.assertEqual(base['unchanged_state_turns'], 3)
        changes = {
            'entities': lambda s: s.setdefault('readerEntityStates', {}).update({self.cid: {'injury': '手背擦伤'}}),
            'outcomes': lambda s: s['characterOutcomeStates'].update({self.cid: dict(status='injured', permanence='temporary')}),
            'knowledge': lambda s: s.setdefault('knownFacts', []).append('confirmed-fixture-fact'),
            'position': lambda s: s.update(playerLocationId='explicit-fixture-place'),
            'threads': lambda s: s['threadLedger'][0].update(status='resolved'),
        }
        for group, change in changes.items():
            with self.subTest(group=group):
                child = self.append(bid, change=change)
                result = self.view(child)
                self.assertEqual(result['unchanged_state_turns'], 0)
                self.assertIn(group, result['recent_turns'][-1]['changed'])
                self.assertIn('repeated_action', result['signals'])

    def test_source_progress_stall_is_detected_even_when_metadata_changes(self):
        bid = self.parent
        for index in range(3):
            bid = self.append(bid, action='换一个说法', change=lambda state, index=index: state.update(
                derivedTurn=index + 1, freeTextProgress='metadata-' + str(index)))
        result = self.view(bid)
        self.assertIn('source_progress_stalled', result['signals'])
        self.assertEqual(result['source_progress_streak'], 3)

    def test_source_progress_regression_is_reported_from_package_beat_order(self):
        first = self.append(change=lambda state: state.update(sourceProgress='chapter_002'))
        regressed = self.append(first, action='回看上一处', change=lambda state: state.update(sourceProgress='chapter_001'))
        result = self.view(regressed)
        self.assertIn('source_progress_regressed', result['signals'])

    def test_unknown_source_progress_does_not_invent_regression(self):
        first = self.append(change=lambda state: state.update(sourceProgress='unlisted-progress'))
        regressed = self.append(first, action='继续确认', change=lambda state: state.update(sourceProgress='chapter_001'))
        self.assertNotIn('source_progress_regressed', self.view(regressed)['signals'])

    def test_metadata_and_list_reordering_do_not_count_as_progress(self):
        def first(s):
            s['characterOutcomeStates'][self.cid] = dict(status='injured', permanence='temporary', causeBranchId='old', evidence='旧证据')
            s['knownFacts'] = ['a', 'b']
        old = self.append(change=first)
        def metadata(s):
            s['characterOutcomeStates'][self.cid].update(causeBranchId='new', evidence='新证据')
            s['knownFacts'].reverse()
            s['threadLedger'].reverse()
        result = self.view(self.append(old, action='仔细查看四周', change=metadata))
        self.assertEqual(result['recent_turns'][-1]['changed'], [])
        self.assertEqual(result['repeated_action_turns'], 1)

    def test_siblings_restoration_and_end_do_not_close_open_questions(self):
        bid = self.append(self.append(self.append()))
        sibling = self.append(self.parent, action='观察眼前场景')
        before = self.view(bid)
        self.assertEqual(self.view(sibling)['turns'], 1)
        self.assertEqual(self.view()['turns'], 0)
        restored = ReadService(ROOT / 'content/packages', self.read.database_path)
        self.assertEqual(restored.route_monitor(self.sid, bid), before)
        self.play.end_route(self.sid, bid)
        ended = self.view(bid)
        self.assertEqual(ended['status'], 'abandoned')
        self.assertFalse(ended['review_recommended'])
        self.assertEqual(ended['closure'], before['closure'])
        self.assertTrue(ended['closure']['open_threads'])
        self.assertEqual(self.view(sibling)['status'], 'active')

    def test_missing_legacy_ledgers_and_history_are_unknown_not_resolved(self):
        bid = self.append(self.append())
        with sqlite3.connect(self.read.database_path) as db:
            row = db.execute('SELECT node_json FROM branch_nodes WHERE id=?', (self.parent,)).fetchone()
            node = json.loads(row[0])
            node.pop('branchState')
            db.execute('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(node), self.parent))
        def legacy(s):
            s.pop('threadLedger', None)
            s.pop('goalLedger', None)
        result = self.view(self.append(bid, change=legacy))
        self.assertEqual(result['coverage'], 'unknown')
        self.assertEqual(result['unknown_state_branches'], [self.parent])
        self.assertIsNone(result['recent_turns'][0]['changed'])
        self.assertEqual(result['closure']['goal_coverage'], 'unknown')
        self.assertTrue(result['closure']['unknown_goals'])
        self.assertTrue(result['closure']['unknown_threads'])
        self.assertFalse(result['closure']['open_threads'])
        self.assertEqual(result['closure']['ending_eligibility'], 'unknown')

    def test_recent_window_is_bounded_and_does_not_reset_cumulative_streak(self):
        bid = self.parent
        for _ in range(15):
            bid = self.append(bid)
        result = self.view(bid)
        self.assertEqual(result['turns'], 15)
        self.assertEqual(len(result['recent_turns']), 12)
        self.assertEqual(result['recent_turns'][0]['turn'], 4)
        self.assertEqual(result['unchanged_state_turns'], 15)

    def test_ordered_entity_attribute_change_is_not_hidden_by_set_normalization(self):
        def initialize(s):
            s['readerEntityStates'] = {self.cid: {'confirmed_order': ['first', 'second']}}
        old = self.append(change=initialize)
        new = self.append(old, change=lambda s: s['readerEntityStates'][self.cid]['confirmed_order'].reverse())
        self.assertIn('entities', self.view(new)['recent_turns'][-1]['changed'])

    def test_closed_goals_and_threads_do_not_imply_an_ending(self):
        from open_story_engine.reader_consequences import goals_for
        with self.read.store() as store:
            contract = store.contract(self.sid)
        _, package = self.read.load_package(self.case['package_id'], self.case['version'])
        def finish(s):
            s['goalLedger'] = goals_for(package, contract, s)
            for goal in s['goalLedger']:
                goal['status'] = 'completed'
            for thread in s['threadLedger']:
                thread['status'] = 'resolved'
        bid = self.append(change=finish)
        result = self.view(bid)
        self.assertFalse(result['closure']['active_goals'])
        self.assertFalse(result['closure']['open_threads'])
        self.assertEqual(result['closure']['ending_eligibility'], 'unknown')
        self.assertEqual(result['status'], 'active')

    def test_invalid_history_returns_controlled_error_without_migration(self):
        bid = self.append()
        with sqlite3.connect(self.read.database_path) as db:
            row = db.execute('SELECT node_json FROM branch_nodes WHERE id=?', (bid,)).fetchone()
            node = json.loads(row[0])
            node['branchState']['goalLedger'] = 'invalid-ledger'
            db.execute('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(node), bid))
        before = self.read.database_path.read_bytes()
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client:
            response = client.get(f'/api/v1/sessions/{self.sid}/route-monitor', params={'branch_id': bid})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['error']['code'], 'route_history_unavailable')
        self.assertEqual(before, self.read.database_path.read_bytes())

    def test_http_is_read_only_and_rejects_cross_session_branch(self):
        bid = self.append()
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client, \
                patch.object(SessionStore, '__init__', side_effect=AssertionError('read must not initialize a writer')):
            response = client.get(f'/api/v1/sessions/{self.sid}/route-monitor', params={'branch_id': bid})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['branch_id'], bid)
            self.assertEqual(client.get(f'/api/v1/sessions/{self.sid}/route-monitor').status_code, 422)
            self.assertEqual(client.get(f'/api/v1/sessions/{self.sid}/route-monitor', params={'branch_id': 'unrelated'}).status_code, 404)
            self.assertEqual(client.get('/api/v1/sessions/unrelated/route-monitor', params={'branch_id': bid}).status_code, 404)
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_absent_database_is_not_created(self):
        missing = Path(self.temp) / 'missing.sqlite'
        with TestClient(create_app(ROOT / 'content/packages', missing, play=False)) as client:
            response = client.get('/api/v1/sessions/missing/route-monitor', params={'branch_id': self.parent})
            self.assertEqual(response.status_code, 404)
        self.assertFalse(missing.exists())


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformRouteMonitor_' + case['package_id'], (LongformRouteMonitorTests,), {'case': case})))
    return suite
