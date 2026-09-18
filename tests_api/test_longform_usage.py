"""Retry cost accounting against every supported official long novel."""
import copy
import json
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.api_read import ReadError
from open_story_engine.api_turn_drafts import TurnDrafts, cumulative_metrics, reported_usage
from open_story_engine.llm import LlmError
from test_support.longform import longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests, RepairFailureGateway


class ReceiptGateway(RepairFailureGateway):
    def complete_json(self, *args, **kwargs):
        try:
            result = super().complete_json(*args, **kwargs)
        except LlmError as error:
            error.raw_response = json.dumps({'usage': {'total_tokens': 7}})
            raise
        result.raw_response = json.dumps({'usage': {'total_tokens': 11}})
        return result

    def complete_text(self, *args, **kwargs):
        result = super().complete_text(*args, **kwargs)
        result.raw_response = json.dumps({'usage': {'total_tokens': 13}})
        return result


class LongformUsageTests(unittest.TestCase):
    setUp = LongformRepairFallbackTests.setUp

    def manager(self, generate):
        manager = TurnDrafts(self.play.drafts.path, generate)
        self.addCleanup(manager.close)
        return manager

    def test_failed_receipts_are_counted_through_actual_player_repairs(self):
        gateway = ReceiptGateway(self.cid, self.action, self.body)
        self.play._planner = PlayerNarrativePlanner(gateway)
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        with self.assertRaises(ReadError):
            self.play.continue_turn(self.sid, self.parent, text=self.action, request_id='receipts')
        self.assertEqual(gateway.calls, ['plan', 'authority', 'prose', 'repair', 'repair'])
        job = next(iter(self.play.drafts.jobs.values()))
        metrics = self.play.drafts.view(job)['metrics']
        self.assertEqual(metrics['calls'], 5)
        self.assertEqual(metrics['reported_tokens'], 49)
        self.assertEqual(metrics['unreported_calls'], 2)
        self.assertIsNone(metrics['tokens'])
        self.assertEqual(metrics['cumulative']['reported_tokens'], 49)
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_retry_restart_and_ready_hits_preserve_cost_and_duration(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        binding['request_id'] = 'retry-cost'
        calls = []
        def generate(*_args):
            calls.append(1)
            if len(calls) == 1:
                error = LlmError('injected failure')
                error.draft_usage = dict(calls=2, reported_tokens=17, unreported_calls=1)
                raise error
            return {'usage': dict(calls=1, reported_tokens=11, unreported_calls=0)}
        manager = self.manager(generate)
        first = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
        with self.assertRaises(ReadError):
            manager.wait(first)
        first_metrics = copy.deepcopy(first['metrics'])
        self.assertIn('complete_ms', first_metrics)
        manager.release_selection(first)
        manager.close()
        manager = self.manager(generate)
        second = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True, retry=True)
        manager.wait(second)
        total = manager.view(second)['metrics']['cumulative']
        self.assertEqual(total['attempts'], 2)
        self.assertEqual(total['calls'], 3)
        self.assertEqual(total['reported_tokens'], 28)
        self.assertEqual(total['unreported_calls'], 1)
        self.assertEqual(total['unmeasured_attempts'], 0)
        self.assertIsNone(total['tokens'])
        for key in ('queue_ms', 'complete_ms'):
            self.assertEqual(total[key], first_metrics[key] + second['metrics'][key])
        self.assertEqual(second['previous_attempts'][0]['metrics'], first_metrics)
        manager.release_selection(second)
        manager.close()
        manager = self.manager(generate)
        ready = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
        manager.wait(ready)
        self.assertEqual(manager.view(ready)['metrics']['cumulative'], total)
        self.assertEqual(ready['metrics']['ready_hits'], 1)
        self.assertEqual(len(calls), 2)
        manager.release_selection(ready)
        separate = manager.ensure(dict(binding, request_id='other-cost'), {'text': self.action}, snapshot, foreground=True)
        manager.wait(separate)
        self.assertEqual(manager.view(separate)['metrics']['cumulative']['tokens'], 11)
        self.assertEqual(separate['previous_attempts'], [])
        manager.release_selection(separate)

    def test_cancel_after_generation_keeps_cost_without_ready_artifact(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        def generate(*_args):
            manager.discard_parent(self.sid, self.parent)
            return {'usage': dict(calls=1, reported_tokens=19, unreported_calls=0)}
        manager = self.manager(generate)
        job = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
        with self.assertRaises(ReadError):
            manager.wait(job)
        # Cancellation is visible before an in-flight provider has returned.
        with manager.condition:
            self.assertTrue(manager.condition.wait_for(lambda: 'complete_ms' in job['metrics'], timeout=5))
        self.assertEqual(job['status'], 'expired')
        self.assertNotIn('artifact', job)
        self.assertEqual(manager.view(job)['metrics']['cumulative']['tokens'], 19)
        manager.release_selection(job)

    def test_post_body_failure_retains_usage_and_audits(self):
        _, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        def generated(service, *_args, **_kwargs):
            service.store.usage.update(calls=1, reported_tokens=23, unreported_calls=0)
            service.store.audits.append([{'kind': 'usage-fixture'}, self.parent])
            return {'kind': 'accepted'}
        def validating():
            raise ReadError(409, 'draft_cancelled', 'injected post-body cancellation')
        with patch('open_story_engine.api_play.CoCreationService.continue_free_text', generated):
            with self.assertRaises(ReadError) as caught:
                self.play._generate_turn(snapshot, {'text': self.action}, None, None, validating, lambda: None)
        self.assertEqual(caught.exception.draft_usage['reported_tokens'], 23)
        self.assertEqual(caught.exception.draft_audits[0][0]['kind'], 'usage-fixture')

    def test_cancelled_attempt_late_receipt_updates_history_not_replacement(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(*_args):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError('cancelled provider did not resume')
                tokens = 19
            else:
                tokens = 11
            return {'usage': dict(calls=1, reported_tokens=tokens, unreported_calls=0)}
        manager = self.manager(generate)
        old = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True)
        self.assertTrue(entered.wait(5))
        manager.discard_parent(self.sid, self.parent)
        new = manager.ensure(binding, {'text': self.action}, snapshot, foreground=True, retry=True)
        manager.wait(new)
        pending = manager.view(new)['metrics']['cumulative']
        self.assertIsNone(pending['tokens'])
        self.assertEqual(pending['unmeasured_attempts'], 1)
        release.set()
        with manager.condition:
            self.assertTrue(manager.condition.wait_for(lambda: 'complete_ms' in old['metrics'], timeout=5))
        self.assertEqual(new['status'], 'ready')
        total = manager.view(new)['metrics']['cumulative']
        self.assertEqual(total['calls'], 2)
        self.assertEqual(total['tokens'], 30)
        self.assertEqual(total['attempts'], 2)
        manager.release_selection(old)
        manager.release_selection(new)
        manager.close()
        restarted = self.manager(generate)
        with restarted.condition:
            restarted._initialize()
        restored = restarted.jobs[new['key']]
        self.assertEqual(restored['status'], 'ready')
        self.assertEqual(restarted.view(restored)['metrics']['cumulative'], total)

    def test_receipt_validation_and_cumulative_streaming(self):
        for value in (True, -1, '12', None):
            with self.subTest(value=value):
                receipt = SimpleNamespace(raw_response=json.dumps({'usage': {'total_tokens': value}}))
                self.assertEqual(reported_usage(receipt), (0, True))
        receipt = SimpleNamespace(raw_response='\n'.join(json.dumps({'usage': {'total_tokens': v}}) for v in (3, 8, 8)))
        self.assertEqual(reported_usage(receipt), (8, False))
        receipt.observations = [{'outcome': 'failed'}, {'outcome': 'completed'}]
        self.assertEqual(reported_usage(receipt), (8, True))
        self.assertEqual(reported_usage(LlmError('no receipt')), (0, True))
        self.assertEqual(reported_usage(SimpleNamespace(raw_response='{"usage":{"total_tokens":0}}')), (0, False))

    def test_legacy_and_pending_usage_remain_unknown(self):
        known = dict(calls=1, reported_tokens=12, unreported_calls=0, complete_ms=20, queue_ms=2)
        total = cumulative_metrics(dict(metrics=known, previous_attempts=[{'status': 'failed'}]))
        self.assertEqual(total['reported_tokens'], 12)
        self.assertEqual(total['unmeasured_attempts'], 1)
        self.assertIsNone(total['tokens'])
        self.assertIsNone(total['complete_ms'])
        pending = cumulative_metrics(dict(metrics={'first_text_ms': None}))
        self.assertIsNone(pending['tokens'])
        self.assertEqual(pending['unmeasured_attempts'], 1)
        empty = cumulative_metrics(dict(metrics=dict(calls=0, reported_tokens=0, unreported_calls=0)))
        self.assertEqual(empty['tokens'], 0)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformUsage_' + case['package_id'], (LongformUsageTests,), {'case': case})))
    return suite
