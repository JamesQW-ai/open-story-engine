"""Explicit closing stages on all eligible novels; no narrative acceptance."""
import copy
import os
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.api_read import ReadError, ReadService
from open_story_engine.api_play import PlayService
from open_story_engine.reader_consequences import initial_goals
from open_story_engine.route_lifecycle import records
from open_story_engine.storage import SessionStore
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_route_closure import LongformRouteClosureTests


class LongformRouteLifecycleTests(unittest.TestCase):
    setUp = LongformRouteClosureTests.setUp
    append = LongformRouteClosureTests.append
    view = LongformRouteClosureTests.view
    lineage = LongformRouteClosureTests.lineage

    def plan(self, mode, bid=None):
        return self.play.plan_route_closure(self.sid, bid or self.parent, mode)

    def test_intents_are_reversible_and_do_not_modify_story_facts(self):
        before = copy.deepcopy(self.lineage())
        self.assertEqual(self.view()['lifecycle']['phase'], 'active')
        for mode in ('normal', 'deviation', 'failure'):
            result = self.plan(mode)
            self.assertEqual(result['lifecycle']['phase'], 'preparing')
            self.assertEqual(result['lifecycle']['intended_type'], mode)
            self.assertIsNone(result['lifecycle']['ending_type'])
            self.assertFalse(result['ending_written'])
            self.assertEqual(result['status'], 'active')
        self.assertEqual(self.plan(None)['lifecycle']['phase'], 'active')
        self.assertEqual(before, self.lineage())

    def test_checklist_clear_enters_closing_without_ending_or_blocking_play(self):
        threads = self.lineage()[-1]['branchState']['threadLedger']
        bid = self.append(goals=[dict(id=g['id'], title=g['title'], status='completed')
                                 for g in initial_goals(self.package, self.contract)],
                          threads=[dict(id=t['id'], title=t['title'], status='resolved') for t in threads])
        self.assertEqual(self.view(bid)['lifecycle']['phase'], 'active')
        result = self.plan('normal', bid)
        self.assertEqual(result['lifecycle']['phase'], 'closing')
        self.assertTrue(result['lifecycle']['requires_ending_evidence'])
        self.assertFalse(result['lifecycle']['ending_written'])
        self.assertEqual(self.read.journey(self.sid, bid)['status'], 'active')
        _, snapshot = self.play._turn_snapshot(self.sid, bid)
        self.assertEqual(snapshot.history[-1]['id'], bid)
        reopened = self.append(parent=bid, threads=[dict(id='new-question', title='新的待查问题', status='open')])
        self.assertEqual(self.view(reopened)['lifecycle']['phase'], 'preparing')

    def test_intent_inheritance_cancellation_and_sibling_isolation(self):
        self.plan('deviation')
        left, right = self.append(), self.append()
        self.assertEqual(self.view(left)['lifecycle']['intent_branch_id'], self.parent)
        self.plan(None, left)
        later = self.append(parent=left)
        self.assertIsNone(self.view(later)['lifecycle']['intended_type'])
        self.assertEqual(self.view(right)['lifecycle']['intended_type'], 'deviation')
        self.assertEqual(self.plan('failure', right)['lifecycle']['intended_type'], 'failure')
        with self.assertRaises(ReadError) as error:
            self.plan('normal')
        self.assertEqual(error.exception.code, 'route_has_continuation')

    def test_early_end_snapshots_unresolved_items_and_is_idempotent_after_restart(self):
        self.plan('failure')
        before = self.view()
        self.play.end_route(self.sid, self.parent)
        ended = self.view()
        lifecycle = ended['lifecycle']
        self.assertEqual(lifecycle['phase'], 'ended')
        self.assertEqual(lifecycle['intended_type'], 'failure')
        self.assertEqual(lifecycle['ending_type'], 'early')
        self.assertEqual(lifecycle['receipt']['outstanding'], before['outstanding'])
        self.assertEqual(lifecycle['receipt']['coverage'], before['coverage'])
        self.assertFalse(lifecycle['ending_written'])
        restored = PlayService(ReadService(ROOT / 'content/packages', self.read.database_path), ROOT)
        self.addCleanup(restored.drafts.close)
        restored.end_route(self.sid, self.parent)
        self.assertEqual(ended, self.view())
        with self.assertRaises(ReadError) as error:
            self.plan('normal')
        self.assertEqual(error.exception.code, 'route_ended')

    def test_end_receipt_and_status_rollback_together(self):
        self.plan('deviation')
        before = self.view()
        with patch('open_story_engine.api_play.save_preferences', side_effect=RuntimeError('injected write failure')):
            with self.assertRaises(RuntimeError):
                self.play.end_route(self.sid, self.parent)
        self.assertEqual(before, self.view())
        self.assertIsNone(self.view()['lifecycle']['receipt'])

    def test_incomplete_history_cannot_enter_closing(self):
        bid = self.append(change=lambda s: s.update(goalLedger=[], threadLedger=[]))
        result = self.plan('normal', bid)
        self.assertEqual(result['readiness'], 'blocked_unknown')
        self.assertEqual(result['lifecycle']['phase'], 'preparing')

    def test_invalid_ledger_rejects_intent_but_early_exit_records_unknown(self):
        bid = self.append(change=lambda s: s.update(goalLedger='broken'))
        with self.assertRaises(ReadError) as error:
            self.plan('failure', bid)
        self.assertEqual(error.exception.code, 'route_history_unavailable')
        self.play.end_route(self.sid, bid)
        with self.read.store() as store:
            receipt = records(store, self.sid, store.lineage(self.sid, bid))[bid]['receipt']
        self.assertEqual(receipt['readiness'], 'unknown')
        self.assertTrue(all(v == 'unknown' for v in receipt['coverage'].values()))

    def test_legacy_database_read_does_not_create_lifecycle_table(self):
        with sqlite3.connect(self.read.database_path) as db:
            db.execute('DROP TABLE route_lifecycle')
        before = self.read.database_path.read_bytes()
        with patch.object(SessionStore, '__init__', side_effect=AssertionError('read must not initialize writer')):
            self.assertEqual(self.view()['lifecycle']['phase'], 'active')
        self.assertEqual(before, self.read.database_path.read_bytes())

    def test_duplicate_plan_does_not_change_the_saved_record(self):
        first = self.plan('normal')
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        self.assertEqual(first, self.plan('normal'))
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_lifecycle_records_are_removed_with_session(self):
        self.plan('normal')
        self.play.end_route(self.sid, self.parent)
        self.play.delete_session(self.sid)
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM route_lifecycle').fetchone()[0], 0)

    def test_http_contract_and_read_only_boundary(self):
        url = f'/api/v1/sessions/{self.sid}/route-closure'
        with patch.dict(os.environ, {'STORY_PLANNER': 'mock'}), \
                TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=True)) as client:
            response = client.post(url, json=dict(branch_id=self.parent, intended_type='deviation'))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['lifecycle']['phase'], 'preparing')
            for mode in ('early', 'completed', 'other'):
                self.assertEqual(client.post(url, json=dict(branch_id=self.parent, intended_type=mode)).status_code, 422)
            self.assertEqual(client.post(url, json=dict(branch_id=self.parent)).status_code, 422)
            self.assertEqual(client.post(url, json=dict(branch_id='foreign', intended_type='normal')).status_code, 404)
            self.assertEqual(client.post('/api/v1/sessions/foreign/route-closure',
                                         json=dict(branch_id=self.parent, intended_type='normal')).status_code, 404)
            response = client.post(url, json=dict(branch_id=self.parent, intended_type=None))
            self.assertEqual(response.json()['lifecycle']['phase'], 'active')
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client:
            self.assertEqual(client.post(url, json=dict(branch_id=self.parent, intended_type='normal')).status_code, 404)
            self.assertEqual(client.get(url, params=dict(branch_id=self.parent)).status_code, 200)

    def test_every_official_role_has_an_independent_closing_intent(self):
        for cid in self.package['story']['entryModel']['sourceCharacterIds']:
            character = next(c for c in self.package['characters'] if c['id'] == cid)
            start = self.play.create_session(self.case['package_id'], self.case['version'],
                                             character['defaultEntryPointId'], cid, identity_opening=True)
            sid, bid = start['session']['id'], start['branch']['id']
            self.play.plan_route_closure(sid, bid, 'normal')
            restored = ReadService(ROOT / 'content/packages', self.read.database_path)
            self.assertEqual(restored.route_closure(sid, bid)['lifecycle']['intended_type'], 'normal')
        self.assertIsNone(self.view()['lifecycle']['intended_type'])

    def test_legacy_ending_without_receipt_stays_unclassified(self):
        from open_story_engine.api_journey import save_preferences
        from contextlib import closing
        with closing(SessionStore(str(self.read.database_path))) as store:
            save_preferences(store, self.sid, ended={self.parent: 'abandoned'})
        result = self.view()['lifecycle']
        self.assertEqual(result['phase'], 'ended')
        self.assertIsNone(result['ending_type'])
        self.assertIsNone(result['receipt'])
        self.play.end_route(self.sid, self.parent)
        self.assertEqual(result, self.view()['lifecycle'])

    def test_end_transaction_blocks_late_commit_from_another_service(self):
        from open_story_engine.route_lifecycle import save_record
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        payload = {'text': '留在原地观察周围'}
        job = self.play._ensure_turn(binding, payload, snapshot, foreground=True)
        self.play.drafts.wait(job)
        artifact = copy.deepcopy(job['artifact'])
        other = PlayService(self.read, ROOT)
        self.addCleanup(other.drafts.close)
        receipt_saved, release, commit_started = threading.Event(), threading.Event(), threading.Event()

        def hold_receipt(*args):
            save_record(*args)
            receipt_saved.set()
            self.assertTrue(release.wait(5))

        def commit():
            commit_started.set()
            return other._commit_turn(job, artifact, payload, 'closing-race')

        with ThreadPoolExecutor(max_workers=2) as pool, \
                patch('open_story_engine.route_lifecycle.save_record', side_effect=hold_receipt):
            ended = pool.submit(self.play.end_route, self.sid, self.parent)
            try:
                self.assertTrue(receipt_saved.wait(5))
                committed = pool.submit(commit)
                self.assertTrue(commit_started.wait(5))
            finally:
                release.set()
            self.assertEqual(ended.result(timeout=5)['status'], 'abandoned')
            with self.assertRaises(ReadError) as error:
                committed.result(timeout=5)
            self.assertEqual(error.exception.code, 'route_ended')
        with self.read.store() as store:
            self.assertEqual(len(store.branches(self.sid)), 1)
        self.assertEqual(self.view()['lifecycle']['ending_type'], 'early')


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformRouteLifecycle_' + case['package_id'],
                                                        (LongformRouteLifecycleTests,), {'case': case})))
    return suite
