"""Image receipt integrity on every eligible official long novel."""
import io
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
import unittest
from unittest.mock import patch

from open_story_engine.api_illustrations import ImageGateway
from open_story_engine.illustration_metrics import report
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_image_lifecycle import LongformImageLifecycleTests


class LongformImageUsageTests(unittest.TestCase):
    setUp = LongformImageLifecycleTests.setUp
    service = LongformImageLifecycleTests.service

    def records(self, items):
        directory = Path(self.temp) / 'metrics'
        directory.mkdir(exist_ok=True)
        for i, item in enumerate(items):
            (directory / f'{i}.json').write_text(json.dumps(dict(sid=self.sid, bid=self.parent, **item)))
        return directory

    def reconciles(self, result):
        for field in ('attempts', 'provider_calls', 'usage_reported_calls', 'reported_tokens',
                      'unmeasured_calls', 'unmeasured_attempts'):
            self.assertEqual(result[field], sum(result[g][field] for g in ('display_confirmed', 'display_unconfirmed')), field)
        json.dumps(result, allow_nan=False)

    def test_invalid_tokens_never_pollute_totals_or_become_complete_receipts(self):
        invalid = [-1, float('nan'), float('inf'), -float('inf'), True, 1.5, 1.0, '12', None, {}, []]
        directory = self.records([
            dict(provider_calls=1, usage={'total_tokens': value}, displayed=i % 2 == 0)
            for i, value in enumerate(invalid)
        ] + [dict(provider_calls=1, usage={'total_tokens': 17}, displayed=True),
             dict(provider_calls=1, usage={'total_tokens': 0}, displayed=False)])
        result = report(directory)
        self.reconciles(result)
        self.assertEqual(result['provider_calls'], len(invalid) + 2)
        self.assertEqual(result['reported_tokens'], 17)
        self.assertEqual(result['usage_reported_calls'], 2)
        self.assertEqual(result['unmeasured_calls'], len(invalid))
        self.assertIsNone(result['total_tokens'])
        self.assertIsNone(result['display_confirmed']['total_tokens'])
        self.assertIsNone(result['display_unconfirmed']['total_tokens'])

    def test_missing_invalid_or_contradictory_call_counts_keep_attempts_unknown(self):
        invalid = [-1, 1.0, True, '1', None, float('nan')]
        items = [dict(provider_calls=value, usage={'total_tokens': 19}, displayed=False) for value in invalid]
        items += [dict(status='failed', usage=None), dict(provider_calls=0, usage={'total_tokens': 9}),
                  dict(provider_calls=0, usage=None, cancel_stage='queued'),
                  dict(provider_calls=1, usage={'total_tokens': 3}, displayed='false', completed='false')]
        result = report(self.records(items))
        self.reconciles(result)
        self.assertEqual(result['unmeasured_attempts'], len(invalid) + 2)
        self.assertEqual(result['provider_calls'], 1)
        self.assertEqual(result['reported_tokens'], 3)
        self.assertIsNone(result['total_tokens'])
        self.assertEqual(result['display_confirmed']['attempts'], 0)
        self.assertEqual(result['completed_not_displayed'], 0)
        self.assertEqual(result['queued_cancelled'], 1)

    def test_one_receipt_cannot_account_for_multiple_calls(self):
        result = report(self.records([dict(provider_calls=3, usage={'total_tokens': 11}, displayed=True),
                                      dict(provider_calls=0, usage=None, cancel_stage='queued')]))
        self.reconciles(result)
        self.assertEqual(result['usage_reported_calls'], 1)
        self.assertEqual(result['unmeasured_calls'], 2)
        self.assertEqual(result['reported_tokens'], 11)
        self.assertIsNone(result['display_confirmed']['total_tokens'])
        self.assertIsNone(result['total_tokens'])
        self.assertEqual(result['display_unconfirmed']['total_tokens'], 0)

    def test_broken_files_are_visible_unknowns_and_report_is_read_only(self):
        directory = self.records([dict(provider_calls=1, usage={'total_tokens': 5})])
        (directory / 'broken.json').write_text('{broken')
        (directory / 'array.json').write_text('[]')
        (directory / 'null.json').write_text('null')
        (directory / 'unicode.json').write_bytes(b'\xff')
        (directory / 'visit-valid.json').write_text(json.dumps({'published_hit': True}))
        (directory / 'visit-invalid.json').write_text(json.dumps({'published_hit': 'false'}))
        (directory / 'views-valid.json').write_text(json.dumps({'display_ms': 12.5}))
        for i, value in enumerate([float('nan'), float('inf'), True, -1, None, '30']):
            (directory / f'views-invalid-{i}.json').write_text(json.dumps({'display_ms': value}))
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        result = report(directory)
        self.reconciles(result)
        self.assertEqual(result['unreadable_records'], 4)
        self.assertEqual(result['unmeasured_attempts'], 4)
        self.assertEqual(result['reported_tokens'], 5)
        self.assertIsNone(result['total_tokens'])
        self.assertEqual(result['visits'], 1)
        self.assertEqual(result['published_hit_rate'], 1)
        self.assertEqual(result['invalid_visit_records'], 1)
        self.assertEqual(result['invalid_display_records'], 6)
        self.assertEqual(result['private_visits'], 0)
        self.assertIsNone(result['private_cache_hit_rate'])
        self.assertEqual(result['display_ms_samples'], [12.5])
        output = subprocess.run([sys.executable, '-B', '-m', 'open_story_engine.illustration_metrics', str(directory)],
                                cwd=ROOT, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(output.stdout), result)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_display_latency_summary_uses_only_valid_receipts(self):
        directory = self.records([dict(provider_calls=0, usage=None)])
        (directory / 'views-1.json').write_text(json.dumps({'display_ms': 10}))
        (directory / 'views-2.json').write_text(json.dumps({'display_ms': 20}))
        (directory / 'views-3.json').write_text(json.dumps({'display_ms': 100}))
        (directory / 'views-invalid.json').write_text(json.dumps({'display_ms': '20'}))
        result = report(directory)
        self.assertEqual(result['display_ms_samples'], [10, 20, 100])
        self.assertEqual(result['display_ms_summary'], {
            'count': 3,
            'average_ms': 130 / 3,
            'p50_ms': 20,
            'p95_ms': 100,
        })

    def test_private_cache_metrics_keep_legacy_visits_unknown(self):
        directory = self.records([dict(provider_calls=0, usage=None)])
        (directory / 'visit-published.json').write_text(json.dumps({'published_hit': True}))
        (directory / 'visit-hit.json').write_text(json.dumps({'published_hit': False, 'private_cache_hit': True}))
        (directory / 'visit-miss.json').write_text(json.dumps({'published_hit': False, 'private_cache_hit': False}))
        (directory / 'visit-legacy.json').write_text(json.dumps({'published_hit': False}))
        result = report(directory)
        self.assertEqual(result['private_visits'], 3)
        self.assertEqual(result['private_cache_hits'], 1)
        self.assertEqual(result['private_cache_misses'], 1)
        self.assertEqual(result['private_cache_unknown'], 1)
        self.assertEqual(result['private_cache_hit_rate'], 0.5)

    def test_zero_calls_and_zero_receipt_are_known_zero_not_missing(self):
        directory = self.records([dict(provider_calls=0, usage=None, cancel_stage='queued'),
                                  dict(provider_calls=1, usage={'total_tokens': 0}, displayed=True)])
        result = report(directory)
        self.reconciles(result)
        self.assertEqual(result['total_tokens'], 0)
        self.assertEqual(result['usage_reported_calls'], 1)
        self.assertEqual(result['unmeasured_attempts'], 0)
        missing = Path(self.temp) / 'does-not-exist'
        self.assertEqual(report(missing)['attempts'], 0)
        self.assertFalse(missing.exists())

    def test_real_gateway_decode_and_download_failures_retain_provider_receipt(self):
        for stage in ('decode', 'download'):
            with self.subTest(stage=stage):
                service, _ = self.service()
                service.directory = Path(self.temp) / stage
                gateway = ImageGateway()
                gateway.base_url, gateway.api_key, gateway.model = 'https://unused.invalid', 'fixture', 'fixture'
                service.gateway = gateway
                item = {'b64_json': 'invalid!'} if stage == 'decode' else {'url': 'https://unused.invalid/image'}
                reply = io.BytesIO(json.dumps({'usage': {'total_tokens': 21}, 'data': [item]}).encode())
                with patch('open_story_engine.api_illustrations.urlopen', side_effect=[reply, TimeoutError('download')]):
                    service.ensure(self.sid, self.parent, subscriber='reader', draw=True)
                    job = next(iter(service._jobs.values()))
                    job['future'].result(timeout=5)
                self.assertEqual(job['status'], 'failed')
                self.assertEqual(json.loads((service.directory / (job['key'] + '.json')).read_text())['usage'], {'total_tokens': 21})
                result = report(service.directory)
                self.assertEqual(result['display_unconfirmed']['total_tokens'], 21)
                self.assertIsNone(result['billed_amount'])
                self.assertFalse(list(service.directory.glob('*.image')))

    def test_failure_before_receipt_does_not_reuse_previous_job_usage(self):
        service, gateway = self.service()
        gateway.usage = {'total_tokens': 999}
        gateway.generate.side_effect = TimeoutError('no current receipt')
        service.ensure(self.sid, self.parent, subscriber='reader', draw=True)
        job = next(iter(service._jobs.values()))
        job['future'].result(timeout=5)
        self.assertIsNone(job['usage'])
        result = report(service.directory)
        self.assertEqual(result['provider_calls'], 1)
        self.assertEqual(result['reported_tokens'], 0)
        self.assertEqual(result['unmeasured_calls'], 1)
        self.assertIsNone(result['total_tokens'])

    def test_cancelled_late_failure_keeps_cost_without_reviving_image(self):
        service, gateway = self.service()
        entered, resume = Event(), Event()
        def fail_late(_prompt):
            entered.set()
            if not resume.wait(5):
                raise AssertionError('test did not resume image worker')
            gateway.usage = {'total_tokens': 13}
            raise ValueError('invalid generated image')
        gateway.generate.side_effect = fail_late
        try:
            service.ensure(self.sid, self.parent, subscriber='leaving', draw=True)
            self.assertTrue(entered.wait(5))
            service.release(self.sid, self.parent, 'leaving')
            self.assertIsNone(report(service.directory)['total_tokens'])
        finally:
            resume.set()
        job = next(iter(service._jobs.values()))
        job['future'].result(timeout=5)
        self.assertEqual(job['status'], 'cancelled')
        result = report(service.directory)
        self.reconciles(result)
        self.assertEqual(result['display_unconfirmed']['total_tokens'], 13)
        self.assertEqual(result['inflight_cancelled'], 1)
        self.assertEqual(result['display_confirmed']['attempts'], 0)
        self.assertFalse(list(service.directory.glob('*.image')))


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformImageUsage_' + case['package_id'],
                                                        (LongformImageUsageTests,), {'case': case})))
    return suite
