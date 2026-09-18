"""Cancellation and cache boundaries on every supported official long novel."""
import threading
import unittest
from unittest.mock import patch

from open_story_engine.api_read import ReadError
from open_story_engine.api_turn_drafts import LEASE_SECONDS, TurnDrafts
from test_support.longform import longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests


class LongformDraftLifecycleTests(unittest.TestCase):
    setUp = LongformRepairFallbackTests.setUp

    def manager(self, generate):
        manager = TurnDrafts(self.play.drafts.path, generate)
        self.addCleanup(manager.close)
        return manager

    def wait_finished(self, manager, job):
        with manager.condition:
            self.assertTrue(manager.condition.wait_for(lambda: 'complete_ms' in job['metrics'], timeout=5))

    def test_full_cache_reuses_existing_task_without_eviction_or_regeneration(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        calls = []
        def generate(*_args):
            calls.append(1)
            return {'usage': dict(calls=1, reported_tokens=17, unreported_calls=0)}
        manager = self.manager(generate)
        with patch('open_story_engine.api_turn_drafts.MAX_JOBS', 1):
            first = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
            manager.wait(first)
            manager.release_selection(first)
            again = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
            manager.wait(again)
            self.assertIs(again, first)
            self.assertEqual(len(calls), 1)
            self.assertEqual(manager.view(again)['metrics']['cumulative']['tokens'], 17)
            manager.release_selection(again)

    def test_full_active_cache_rejects_new_key_but_keeps_shared_task(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(*_args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('blocked provider did not resume')
            return {'usage': dict(calls=1, reported_tokens=9, unreported_calls=0)}
        manager = self.manager(generate)
        with patch('open_story_engine.api_turn_drafts.MAX_JOBS', 1):
            first = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
            self.assertTrue(entered.wait(5))
            with self.assertRaises(ReadError) as caught:
                manager.ensure(dict(binding, request_id='extra'), {'text': self.action}, snapshot, foreground=True)
            self.assertEqual(caught.exception.code, 'draft_capacity')
            shared = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
            self.assertIs(shared, first)
            manager.release_selection(first)
            release.set()
            manager.wait(shared)
            self.assertEqual(shared['status'], 'ready')
            manager.release_selection(shared)

    def test_evicted_task_cannot_return_through_late_selection_release(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        manager = self.manager(lambda *_args: {'usage': dict(calls=1, reported_tokens=7, unreported_calls=0)})
        with patch('open_story_engine.api_turn_drafts.MAX_JOBS', 1):
            old = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
            manager.wait(old)
            manager.release_selection(old)
            new = manager.ensure(dict(binding, request_id='replacement'), {'text': self.action}, snapshot, foreground=True)
            manager.wait(new)
            manager.release_selection(old)
            with manager._db() as db:
                self.assertEqual([row[0] for row in db.execute('SELECT key FROM turn_drafts')], [new['key']])
            self.assertEqual(list(manager.jobs), [new['key']])
            manager.release_selection(new)

    def test_deleted_session_stays_deleted_after_replaced_worker_returns(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(*_args):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError('old provider did not resume')
            return {'usage': dict(calls=1, reported_tokens=13, unreported_calls=0)}
        manager = self.manager(generate)
        old = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
        self.assertTrue(entered.wait(5))
        manager.discard_parent(self.sid, self.parent)
        current = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True, retry=True)
        manager.wait(current)
        manager.discard_session(self.sid)
        release.set()
        self.wait_finished(manager, old)
        manager.release_selection(old)
        manager.release_selection(current)
        self.assertEqual(manager.jobs, {})
        with manager._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM turn_drafts').fetchone()[0], 0)
        manager.close()
        restored = self.manager(generate)
        with restored.condition:
            restored._initialize()
        self.assertEqual(restored.jobs, {})

    def test_queue_cancel_reports_zero_calls_and_never_runs(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        manager = self.manager(lambda *_args: self.fail('cancelled queued work ran'))
        # Hold the scheduler lock so releasing the subscription wins before a worker starts.
        with manager.condition:
            job = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='leaving-tab')
            manager.release(self.sid, self.parent, 'leaving-tab')
        self.assertEqual(job['status'], 'expired')
        metrics = manager.view(job)['metrics']
        self.assertEqual(metrics['cumulative']['tokens'], 0)
        self.assertEqual(metrics['cumulative']['calls'], 0)
        self.assertEqual(metrics['complete_ms'], 0)
        self.assertNotIn('snapshot', job)
        manager.close()
        restored = self.manager(manager.generate)
        with restored.condition:
            restored._initialize()
        self.assertEqual(restored.view(restored.jobs[job['key']])['metrics'], metrics)

    def test_shared_subscriber_survives_one_tab_leaving_and_preserves_unused_cost(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(*_args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('shared provider did not resume')
            return {'usage': dict(calls=1, reported_tokens=21, unreported_calls=0)}
        manager = self.manager(generate)
        job = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='tab-one')
        self.assertTrue(entered.wait(5))
        self.assertIs(manager.ensure(binding, {'text': self.action}, snapshot, subscriber='tab-two'), job)
        manager.release(self.sid, self.parent, 'tab-one')
        release.set()
        manager.wait(job)
        manager.release(self.sid, self.parent, 'tab-two')
        self.assertEqual(job['status'], 'ready')
        self.assertEqual(job['metrics'].get('selection_count', 0), 0)
        self.assertEqual(manager.view(job)['metrics']['cumulative']['tokens'], 21)
        self.assertNotIn('committed', job['metrics'])
        manager.close()
        restored = self.manager(generate)
        with restored.condition:
            restored._initialize()
        self.assertEqual(restored.view(restored.jobs[job['key']])['metrics']['cumulative']['tokens'], 21)

    def test_close_cancels_queued_work_and_rejects_new_requests(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        manager = self.manager(lambda *_args: self.fail('closed service ran generation'))
        with manager.condition:
            job = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='tab')
            manager.close()
        self.assertEqual(job['status'], 'expired')
        self.assertEqual(manager.view(job)['metrics']['cumulative']['tokens'], 0)
        with self.assertRaises(ReadError) as caught:
            manager.ensure(dict(binding, request_id='after-close'), {'text': self.action}, snapshot, foreground=True)
        self.assertEqual(caught.exception.code, 'draft_unavailable')
        self.assertEqual(list(manager.jobs), [job['key']])

    def test_expired_subscription_stops_next_callback_without_inventing_usage(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(_snapshot, _payload, delta, _reset, _validating, _check):
            entered.set()
            if not release.wait(5):
                raise AssertionError('provider did not resume')
            delta(self.body)
            self.fail('expired subscription allowed further generation')
        manager = self.manager(generate)
        job = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='stale-tab')
        self.assertTrue(entered.wait(5))
        with manager.condition:
            job['subscribers']['stale-tab'] = 0
        release.set()
        with self.assertRaises(ReadError):
            manager.wait(job)
        self.assertEqual(job['status'], 'expired')
        self.assertEqual(job['events'], [])
        self.assertIsNone(manager.view(job)['metrics']['cumulative']['tokens'])
        self.assertNotIn('artifact', job)

    def test_failed_retry_keeps_other_tabs_live_lease_after_foreground_disconnect(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        calls = []
        def generate(_snapshot, _payload, delta, _reset, _validating, _check):
            calls.append(1)
            if len(calls) == 1:
                raise ReadError(503, 'generation_failed', 'injected first attempt failure')
            entered.set()
            if not resume.wait(5):
                raise AssertionError('retry provider did not resume')
            delta(self.paragraphs[0])
            return {'usage': dict(calls=1, reported_tokens=12, unreported_calls=0)}
        manager = self.manager(generate)
        with manager.condition:
            first = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='selecting-tab')
            manager.ensure(binding, {'text': self.action}, snapshot, subscriber='reading-tab')
            manager.ensure(binding, {'text': self.action}, snapshot, subscriber='offline-tab')
        with self.assertRaises(ReadError):
            manager.wait(first)
        with manager.condition:
            first['subscribers']['offline-tab'] = 0
            deadline = first['subscribers']['reading-tab']
            retry = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True, retry=True)
            manager.release(self.sid, self.parent, 'selecting-tab')
        self.assertTrue(entered.wait(5))
        # The foreground request disconnects before the other tab's next poll.
        manager.release_selection(retry)
        resume.set()
        self.wait_finished(manager, retry)
        self.assertEqual(retry['status'], 'ready')
        self.assertEqual(retry['subscribers'], {'reading-tab': deadline})
        self.assertEqual(first['subscribers']['reading-tab'], deadline)
        self.assertEqual(len(calls), 2)
        self.assertEqual(retry['previous_attempts'][0]['status'], 'failed')

    def test_expired_tab_does_not_cancel_another_tabs_renewed_inflight_task(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        calls = []
        def generate(_snapshot, _payload, delta, _reset, _validating, _check):
            calls.append(1)
            entered.set()
            if not resume.wait(5):
                raise AssertionError('shared provider did not resume')
            delta(self.paragraphs[0])
            return {'usage': dict(calls=1, reported_tokens=10, unreported_calls=0)}
        manager = self.manager(generate)
        job = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='offline-tab')
        self.assertTrue(entered.wait(5))
        with manager.condition:
            deadline = job['subscribers']['offline-tab']
            with patch('open_story_engine.api_turn_drafts.time.time', return_value=deadline):
                current = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='live-tab')
                manager.release(self.sid, self.parent, 'offline-tab')
                self.assertIs(current, job)
                self.assertEqual(job['subscribers'], {'live-tab': deadline + LEASE_SECONDS})
        resume.set()
        manager.wait(job)
        self.assertEqual(len(calls), 1)
        self.assertEqual(job['status'], 'ready')

    def test_foreground_takeover_survives_all_page_leases_expiring(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def generate(_snapshot, _payload, delta, _reset, _validating, _check):
            entered.set()
            if not resume.wait(5):
                raise AssertionError('selected provider did not resume')
            delta(self.paragraphs[0])
            return {'usage': dict(calls=1, reported_tokens=8, unreported_calls=0)}
        manager = self.manager(generate)
        job = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='offline-tab')
        self.assertTrue(entered.wait(5))
        selected = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
        self.assertIs(selected, job)
        with manager.condition:
            job['subscribers']['offline-tab'] = 0
            manager._sweep()
        self.assertEqual(job['subscribers'], {})
        resume.set()
        manager.wait(selected)
        self.assertEqual(selected['status'], 'ready')
        manager.release_selection(selected)

    def test_queued_lease_expiry_reports_zero_calls_and_fresh_lease_restarts_once(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        calls = []
        def generate(*_args):
            calls.append(1)
            return {'usage': dict(calls=1, reported_tokens=6, unreported_calls=0)}
        manager = self.manager(generate)
        with manager.condition:
            old = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='offline-tab')
            deadline = old['subscribers']['offline-tab']
            with patch('open_story_engine.api_turn_drafts.time.time', return_value=deadline):
                replacement = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='restored-tab')
            self.assertEqual(old['status'], 'expired')
            self.assertEqual(old['metrics']['calls'], 0)
            self.assertEqual(old['metrics']['tokens'], 0)
            self.assertEqual(set(replacement['subscribers']), {'restored-tab'})
            self.assertNotEqual(old['attempt_id'], replacement['attempt_id'])
        manager.wait(replacement)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manager.view(replacement)['metrics']['cumulative']['tokens'], 6)

    def test_releasing_same_subscriber_is_scoped_to_session_and_parent(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        manager = self.manager(lambda *_args: self.fail('queued cancellation unexpectedly ran'))
        with manager.condition:
            target = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='tab')
            other_parent = manager.ensure(dict(binding, parent_branch_id='other-parent'), {'text': self.action}, snapshot, subscriber='tab')
            other_session = manager.ensure(dict(binding, session_id='other-session'), {'text': self.action}, snapshot, subscriber='tab')
            manager.release(self.sid, self.parent, 'tab')
            manager.release(self.sid, self.parent, 'tab')
            self.assertEqual(target['status'], 'expired')
            for other in (other_parent, other_session):
                self.assertEqual(other['status'], 'queued')
                self.assertEqual(set(other['subscribers']), {'tab'})
            manager.close()

    def test_release_before_prepare_blocks_late_request_but_not_restored_page(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        manager = self.manager(lambda *_args: self.fail('retired subscription generated'))
        with manager.condition:
            manager.release(self.sid, self.parent, 'old-page', retire=True)
            with self.assertRaises(ReadError) as caught:
                manager.ensure(binding, {'text': self.action}, snapshot, subscriber='old-page')
            self.assertEqual(caught.exception.code, 'subscription_released')
            self.assertFalse(manager.jobs)
            current = manager.ensure(binding, {'text': self.action}, snapshot, subscriber='restored-page')
            manager.release(self.sid, self.parent, 'old-page', retire=True)
            self.assertEqual(set(current['subscribers']), {'restored-page'})
            self.assertEqual(current['status'], 'queued')
            manager.close()

    def test_release_history_is_bounded_without_forgetting_unexpired_retirements(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        manager = self.manager(lambda *_args: self.fail('retired subscription generated'))
        with patch('open_story_engine.api_turn_drafts.MAX_RELEASED_SUBSCRIPTIONS', 1):
            manager.release(self.sid, self.parent, 'old-page', retire=True)
            manager.release(self.sid, self.parent, 'old-page', retire=True)
            with self.assertRaises(ReadError) as caught:
                manager.release(self.sid, self.parent, 'second-page', retire=True)
            self.assertEqual(caught.exception.code, 'draft_capacity')
            with self.assertRaises(ReadError):
                manager.ensure(binding, {'text': self.action}, snapshot, subscriber='old-page')
            expiry = next(iter(manager.released_subscriptions.values()))
            with patch('open_story_engine.api_turn_drafts.time.monotonic', return_value=expiry):
                manager.release(self.sid, self.parent, 'second-page', retire=True)
            self.assertEqual(list(manager.released_subscriptions), [(self.sid, self.parent, 'second-page')])


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformLifecycle_' + case['package_id'], (LongformDraftLifecycleTests,), {'case': case})))
    return suite
