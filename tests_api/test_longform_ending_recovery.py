"""Interrupted ending review recovery and transport receipts on every official novel."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadError
from open_story_engine.llm import OpenAICompatibleGateway, LlmError
from open_story_engine.route_endings import CHECKS
from open_story_engine.storage import SessionStore
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_ending_proposals import LongformEndingProposalTests


class LongformEndingRecoveryTests(unittest.TestCase):
    setUp = LongformEndingProposalTests.setUp
    append = LongformEndingProposalTests.append
    lineage = LongformEndingProposalTests.lineage
    ready = LongformEndingProposalTests.ready
    gateway = LongformEndingProposalTests.gateway
    propose = LongformEndingProposalTests.propose

    def interrupted(self, bid, body):
        gateway = self.gateway(error=RuntimeError('simulated process interruption'))
        with self.assertRaises(RuntimeError):
            self.propose(bid, body)
        return gateway, self.read.ending_proposals(self.sid, bid)[0]

    def test_restart_can_cancel_legacy_pending_and_explicitly_resubmit(self):
        bid, body = self.ready()
        gateway, pending = self.interrupted(bid, body)
        restored = PlayService(self.read, self.read.database_path.parent)
        self.addCleanup(restored.drafts.close)
        before = self.read.database_path.read_bytes()
        with patch.object(SessionStore, '__init__', side_effect=AssertionError('read initialized writer')):
            self.assertTrue(self.read.ending_proposal(self.sid, bid, pending['id'])['can_cancel'])
            self.assertIsNone(self.read.ending_proposals(self.sid, bid)[0]['audit']['metrics'])
        self.assertEqual(before, self.read.database_path.read_bytes())
        cancelled = restored.cancel_ending(self.sid, bid, pending['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertFalse(cancelled['can_cancel'])
        self.assertIsNone(cancelled['audit']['metrics'])  # Unknown, never free.
        self.assertFalse(cancelled['audit']['cancellation']['provider_cancelled'])
        self.assertEqual(restored.cancel_ending(self.sid, bid, pending['id']), cancelled)
        self.assertEqual(self.propose(bid, body), cancelled)  # Same id never calls again.
        self.assertEqual(len(gateway.inputs), 1)
        new_gateway = self.gateway()
        newer = self.propose(bid, body, 'explicit-new-review')
        self.assertEqual(newer['status'], 'approved')
        self.assertEqual(len(new_gateway.inputs), 1)
        self.assertNotEqual(newer['id'], cancelled['id'])
        self.assertEqual(self.read.journey(self.sid, bid)['status'], 'active')
        with self.assertRaises(ReadError):
            self.play.commit_ending(self.sid, bid, cancelled['id'])

    def test_late_result_only_updates_audit_and_cannot_overwrite_new_review(self):
        bid, body = self.ready()
        restored = PlayService(self.read, self.read.database_path.parent)
        self.addCleanup(restored.drafts.close)
        newer = []

        def while_running():
            old = self.read.ending_proposals(self.sid, bid)[0]
            restored.cancel_ending(self.sid, bid, old['id'])
            self.gateway()
            newer.append(self.propose(bid, body, 'new-review'))

        self.gateway(on_call=while_running)
        late = self.propose(bid, body)
        self.assertEqual(late['status'], 'cancelled')
        self.assertIsNone(late['review'])
        self.assertEqual(late['audit']['late_result']['status'], 'approved')
        self.assertEqual(late['audit']['metrics']['reported_tokens'], 17)
        self.assertEqual(late['audit']['metrics']['tokens'], 17)
        self.assertEqual(self.read.ending_proposals(self.sid, bid)[0]['id'], newer[0]['id'])
        with self.assertRaises(ReadError):
            restored.commit_ending(self.sid, bid, late['id'])
        self.assertEqual(restored.commit_ending(self.sid, bid, newer[0]['id'])['status'], 'completed')

    def test_late_failure_retains_cancellation_and_unknown_cost(self):
        bid, body = self.ready()
        def cancel():
            pending = self.read.ending_proposals(self.sid, bid)[0]
            self.play.cancel_ending(self.sid, bid, pending['id'])
        self.gateway(on_call=cancel, error=LlmError('timeout', 'transport_error'))
        result = self.propose(bid, body)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['audit']['late_result']['status'], 'failed')
        self.assertEqual(result['audit']['failure']['code'], 'transport_error')
        self.assertIsNone(result['audit']['metrics']['tokens'])
        self.assertEqual(result['audit']['metrics']['unreported_calls'], 1)

    def test_changed_intent_pending_remains_cancellable_and_finished_cannot_cancel(self):
        bid, body = self.ready()
        _, pending = self.interrupted(bid, body)
        self.play.plan_route_closure(self.sid, bid, None)
        viewed = self.read.ending_proposals(self.sid, bid)[0]
        self.assertEqual(viewed['status'], 'stale')
        self.assertTrue(viewed['can_cancel'])
        self.assertEqual(self.play.cancel_ending(self.sid, bid, pending['id'])['status'], 'cancelled')
        self.play.plan_route_closure(self.sid, bid, 'normal')
        self.gateway()
        approved = self.propose(bid, body, 'new')
        with self.assertRaises(ReadError) as error:
            self.play.cancel_ending(self.sid, bid, approved['id'])
        self.assertEqual(error.exception.code, 'ending_review_finished')
        self.play.commit_ending(self.sid, bid, approved['id'])
        with self.assertRaises(ReadError):
            self.play.cancel_ending(self.sid, bid, approved['id'])

    def test_http_cancel_is_scoped_idempotent_and_not_available_read_only(self):
        bid, body = self.ready()
        _, pending = self.interrupted(bid, body)
        url = f'/api/v1/sessions/{self.sid}/ending-proposals/{pending["id"]}/cancel'
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=True)) as client:
            self.assertEqual(client.post(url, json=dict(branch_id=self.parent)).status_code, 404)
            self.assertEqual(client.post(url, json=dict(branch_id=bid, approved=True)).status_code, 422)
            result = client.post(url, json=dict(branch_id=bid))
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['status'], 'cancelled')
            self.assertEqual(client.post(url, json=dict(branch_id=bid)).json(), result.json())
        before = self.read.database_path.read_bytes()
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client:
            self.assertEqual(client.post(url, json=dict(branch_id=bid)).status_code, 404)
        self.assertEqual(before, self.read.database_path.read_bytes())

    def test_real_gateway_fallback_counts_each_transport_and_missing_receipts(self):
        bid, body = self.ready()
        value = dict(decision='allow', checks={key: dict(passed=True, evidence=body, reason='契约夹具') for key in CHECKS})
        gateway = OpenAICompatibleGateway('http://unused.invalid', '', 'fixture', stream=False)
        self.play._planner = SimpleNamespace(gateway=gateway)
        for receipt in (True, False):
            raw = json.dumps({'usage': {'total_tokens': 23}} if receipt else {})
            with patch.object(OpenAICompatibleGateway, '_json', side_effect=TimeoutError('first transport')), \
                 patch.object(OpenAICompatibleGateway, '_stream', return_value=(json.dumps(value), raw)) as stream:
                result = self.propose(bid, body, f'fallback-{receipt}')
            stream.assert_called_once()
            metrics = result['audit']['metrics']
            self.assertEqual(metrics['calls'], 2)
            self.assertEqual(metrics['reported_tokens'], 23 if receipt else 0)
            self.assertEqual(metrics['unreported_calls'], 1 if receipt else 2)
            self.assertIsNone(metrics['tokens'])
            self.assertEqual(len(result['audit']['observations']), 2)
        with patch.object(OpenAICompatibleGateway, '_json', side_effect=TimeoutError('first')), \
             patch.object(OpenAICompatibleGateway, '_stream', side_effect=TimeoutError('second')):
            failed = self.propose(bid, body, 'both-failed')
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['audit']['metrics']['calls'], 2)
        self.assertEqual(failed['audit']['metrics']['unreported_calls'], 2)

    def test_budget_rejection_has_zero_transport_calls(self):
        bid, body = self.ready()
        gateway = OpenAICompatibleGateway('http://unused.invalid', '', 'fixture', stream=False)
        gateway.remaining_calls = 0
        self.play._planner = SimpleNamespace(gateway=gateway)
        with patch.object(OpenAICompatibleGateway, '_json', side_effect=AssertionError('must not call provider')):
            result = self.propose(bid, body)
        self.assertEqual(result['audit']['metrics']['calls'], 0)
        self.assertEqual(result['audit']['metrics']['tokens'], 0)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformEndingRecovery_' + case['package_id'],
                                                        (LongformEndingRecoveryTests,), {'case': case})))
    return suite
