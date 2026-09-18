"""Image cancellation races using official long-novel branch identities."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import Mock, patch

from open_story_engine.api_illustrations import IllustrationService
from open_story_engine.api_read import ReadError
from open_story_engine.illustration_metrics import report
from open_story_engine.scene_library import SceneLibrary
from test_support.longform import longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests
from tests_api.test_story_media import PNG


class LongformImageLifecycleTests(unittest.TestCase):
    setUp = LongformRepairFallbackTests.setUp

    def service(self):
        gateway = Mock(available=True, model='fixture')
        library = Mock()
        library.resolve.return_value = None
        library.live_policy.return_value = {'prompt': '仅验证排队与取消，不调用图片服务'}
        service = IllustrationService(self.read, Path(self.temp) / 'images', gateway, library)
        self.addCleanup(service.close)
        return service, gateway

    def test_release_before_draw_prevents_late_provider_call(self):
        service, gateway = self.service()
        service.release(self.sid, self.parent, 'leaving')
        with self.assertRaises(ReadError) as caught:
            service.ensure(self.sid, self.parent, subscriber='leaving', draw=True)
        self.assertEqual(caught.exception.code, 'subscription_released')
        service.ensure(self.sid, self.parent, subscriber='restored')
        gateway.generate.assert_not_called()

    def test_release_during_context_lookup_stops_draw_before_enqueuing(self):
        service, gateway = self.service()
        entered, resume = Event(), Event()
        original = service._context
        def delayed(*args):
            result = original(*args)
            entered.set()
            if not resume.wait(5):
                raise AssertionError('context lookup did not resume')
            return result
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(service, '_context', side_effect=delayed):
            request = pool.submit(service.ensure, self.sid, self.parent, subscriber='leaving', draw=True)
            try:
                self.assertTrue(entered.wait(5))
                service.release(self.sid, self.parent, 'leaving')
            finally:
                resume.set()
            with self.assertRaises(ReadError):
                request.result(timeout=5)
        gateway.generate.assert_not_called()
        self.assertFalse(service._jobs)

    def test_closed_service_rejects_draw_without_spend(self):
        service, gateway = self.service()
        service.close()
        with self.assertRaises(ReadError) as caught:
            service.ensure(self.sid, self.parent, subscriber='reader', draw=True)
        self.assertEqual(caught.exception.code, 'illustration_unavailable')
        gateway.generate.assert_not_called()

    def test_compatible_art_accepts_provenance_fields_but_rejects_changed_status(self):
        node = self.read.branch_view(self.sid, self.parent)
        state = node['branchState']
        cid, place = self.cid, state['playerLocationId']
        state['characterOutcomeStates'][cid] = dict(status='alive', permanence='temporary', evidence=node['narrativeText'], causeBranchId=self.parent)
        state['characterAppearanceVersions'] = {cid: 'current'}
        card = dict(review_status='approved', source_evidence=[node['narrativeText']], location_id=place,
                    characters={cid: {'outcome': {'status': 'alive', 'permanence': 'temporary'}, 'appearance_version': 'current'}})
        self.assertTrue(SceneLibrary.compatible(card, node))
        for status in ('dead', 'departed', 'missing', 'injured'):
            changed = copy.deepcopy(node)
            changed['branchState']['characterOutcomeStates'][cid]['status'] = status
            self.assertFalse(SceneLibrary.compatible(card, changed))

    def test_unconfirmed_image_usage_keeps_unknown_cost_separate(self):
        from open_story_engine.illustration_metrics import report
        directory = Path(self.temp) / 'metrics'
        directory.mkdir()
        for i, data in enumerate([
            dict(provider_calls=1, usage={'total_tokens': 12}, displayed=True),
            dict(provider_calls=1, usage={'total_tokens': 7}, displayed=False, completed=True),
            dict(provider_calls=1, usage=None, displayed=False),
            dict(provider_calls=0, usage=None, cancel_stage='queued'),
        ]):
            (directory / f'{i}.json').write_text(json.dumps(dict(data, sid=self.sid, bid=self.parent)))
        result = report(directory)
        self.assertEqual(result['display_confirmed']['total_tokens'], 12)
        self.assertEqual(result['display_unconfirmed']['reported_tokens'], 7)
        self.assertEqual(result['display_unconfirmed']['unmeasured_calls'], 1)
        self.assertIsNone(result['display_unconfirmed']['total_tokens'])
        self.assertIsNone(result['display_unconfirmed']['billed_amount'])
        self.assertEqual(result['completed_not_displayed'], 1)

    def test_private_visit_records_cache_miss_then_hit(self):
        service, gateway = self.service()
        gateway.generate.return_value = (PNG, 'image/png')
        service.ensure(self.sid, self.parent, subscriber='first', draw=True)
        job = next(iter(service._jobs.values()))
        job['future'].result(timeout=5)
        service.ensure(self.sid, self.parent, subscriber='second', draw=True)
        visits = sorted(service.directory.glob('visit-*.json'))
        self.assertEqual(len(visits), 2)
        records = [json.loads(path.read_text()) for path in visits]
        self.assertEqual(sorted(r['private_cache_hit'] for r in records), [False, True])
        result = report(service.directory)
        self.assertEqual(result['private_visits'], 2)
        self.assertEqual(result['private_cache_hits'], 1)
        self.assertEqual(result['private_cache_misses'], 1)
        self.assertEqual(result['private_cache_hit_rate'], 0.5)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformImages_' + case['package_id'], (LongformImageLifecycleTests,), {'case': case})))
    return suite
