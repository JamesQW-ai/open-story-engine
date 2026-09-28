"""Required bundle gate across actual turn jobs, SSE and temporary storage."""
import copy
import os
import sqlite3
import unittest
from unittest.mock import patch

from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadError
from open_story_engine.context_bundle import ContextBundleBuilder, ContextBundleError
from open_story_engine.module_context import ModuleContextResolver
from test_support.longform import longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests, RepairFailureGateway


class RequiredContextApiTests(unittest.TestCase):
    setUp = LongformRepairFallbackTests.setUp

    def test_runtime_factory_requires_bundle_independently_of_visibility_mode(self):
        for mode in ('audit_fallback', 'formal_required'):
            runtime = PlayService.__new__(PlayService)
            runtime.mode = 'openai'
            with patch.dict(os.environ, {'STORY_CONTEXT_PROJECTION_STATE_VISIBILITY_MODE': mode}, clear=True), \
                    patch('open_story_engine.api_play.writer_config_from_env', return_value={
                        'base_url': 'http://127.0.0.1:1', 'api_key': 'offline-fixture', 'model': 'fixture', 'route': 'relay'}):
                runtime._build_runtime()
            self.assertTrue(runtime._planner.require_context_bundle)
            self.assertEqual(runtime._planner.context_projection['stateVisibilityMode'], mode)

    def assert_failed_turn(self, gateway, request_id):
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        events = []
        with self.assertRaises(ReadError):
            self.play.continue_turn(self.sid, self.parent, text=self.action, request_id=request_id,
                stream=lambda text: events.append(('delta', text)),
                stream_reset=lambda reason: events.append(('reset', reason)))
        self.assertEqual(events, [])
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))
        with self.play.drafts.condition:
            self.assertTrue(self.play.drafts.condition.wait_for(lambda: sum(self.play.drafts.workers) == 0, timeout=5))
            jobs = list(self.play.drafts.jobs.values())
            self.assertTrue(jobs)
            job = jobs[-1]
            self.assertEqual(job['status'], 'failed')
            self.assertNotIn('artifact', job)
            audits = copy.deepcopy(job['failure_audits'])
        self.assertTrue(audits)
        audit = audits[-1][0]['promptContext']['contextBundle']
        self.assertEqual(audit['status'], 'rejected')
        self.assertEqual(audit['bundleRejectionAudit']['auditType'], 'context_bundle_rejected')
        self.assertEqual(audit['bundleRejectionAudit']['parentBranchId'], self.parent)

    def test_missing_module_stops_before_provider_and_does_not_write_or_stream(self):
        gateway = RepairFailureGateway(self.cid, self.action, self.body)
        self.play._planner = PlayerNarrativePlanner(gateway, require_context_bundle=True)
        with patch.object(ModuleContextResolver, 'for_package', return_value=None):
            self.assert_failed_turn(gateway, 'missing-context')
        self.assertEqual(gateway.calls, [])

    def test_bundle_validation_failure_stops_before_provider(self):
        gateway = RepairFailureGateway(self.cid, self.action, self.body)
        self.play._planner = PlayerNarrativePlanner(gateway, require_context_bundle=True)
        with patch.object(ContextBundleBuilder, 'build', side_effect=ContextBundleError('injected source mismatch')):
            self.assert_failed_turn(gateway, 'invalid-context')
        self.assertEqual(gateway.calls, [])

    def test_protected_overflow_does_not_retry_stream_or_commit(self):
        gateway = RepairFailureGateway(self.cid, self.action, self.body)
        self.play._planner = PlayerNarrativePlanner(gateway, require_context_bundle=True,
                                                   context_window_tokens=1)
        self.assert_failed_turn(gateway, 'context-overflow')
        self.assertEqual(gateway.calls, [])

    def test_late_module_failure_blocks_prose_after_valid_planning(self):
        gateway = RepairFailureGateway(self.cid, self.action, self.body)
        self.play._planner = PlayerNarrativePlanner(gateway, require_context_bundle=True)
        original = ModuleContextResolver.resolve
        calls = []

        def fail_second(resolver, *args, **kwargs):
            calls.append(True)
            if len(calls) > 1:
                raise ValueError('injected chapter module failure')
            return original(resolver, *args, **kwargs)

        with patch.object(ModuleContextResolver, 'resolve', fail_second):
            self.assert_failed_turn(gateway, 'late-context-failure')
        self.assertEqual(gateway.calls, ['plan', 'authority'])
        self.assertEqual(len(calls), 2)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('RequiredContext_' + case['package_id'],
                                                       (RequiredContextApiTests,), {'case': case})))
    return suite
