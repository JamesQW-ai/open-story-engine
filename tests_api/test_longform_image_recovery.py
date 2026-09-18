"""Recover optional illustrations without inferring permission to spend."""
import json
from pathlib import Path
import unittest

from open_story_engine.api_read import ReadError
from open_story_engine.illustration_metrics import report
from test_support.longform import longform_cases
from tests_api.test_longform_image_lifecycle import LongformImageLifecycleTests
from tests_api.test_story_media import PNG


class LongformImageRecoveryTests(unittest.TestCase):
    setUp = LongformImageLifecycleTests.setUp
    service = LongformImageLifecycleTests.service

    def seed(self, name, **changes):
        service, gateway = self.service()
        service.directory = Path(self.temp) / name
        service.directory.mkdir()
        node = self.read.branch_view(self.sid, self.parent)
        key = service._key(self.sid, self.parent, node)
        record = dict(key=key, sid=self.sid, bid=self.parent, status='ready', provider_calls=1,
                      usage={'total_tokens': 23}, completed=True, displayed=False, mime='image/png')
        record.update(changes)
        path = service.directory / (key + '.json')
        path.write_text(json.dumps(record))
        return service, gateway, key, path

    def blocked(self, service, gateway):
        for result in (service.view(self.sid, self.parent),
                       service.ensure(self.sid, self.parent, subscriber='reader', draw=True, retry=True)):
            self.assertFalse(result['can_generate'])
            self.assertNotEqual(result['items'][0]['status'], 'ready')
            self.assertNotIn('url', result['items'][0])
        gateway.generate.assert_not_called()
        with self.assertRaises(ReadError) as caught:
            service.asset(self.sid, self.parent, 0)
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(ReadError) as caught:
            service.shown(self.sid, self.parent, 'reader', 20)
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(list(service.directory.glob('views-*.json')))

    def test_corrupt_records_block_redraw_without_rewriting_evidence(self):
        for i, data in enumerate([b'{broken', b'[]', b'null', b'{}', b'\xff']):
            with self.subTest(data=data):
                service, gateway, _, path = self.seed(f'broken-{i}')
                path.write_bytes(data)
                self.blocked(service, gateway)
                self.assertEqual(path.read_bytes(), data)
                self.assertIsNone(report(service.directory)['total_tokens'])

    def test_invalid_call_counts_and_conflicting_zero_never_enable_spend(self):
        changes = [dict(provider_calls=v) for v in (None, -1, True, '0', 0.0, float('nan'))]
        changes += [dict(status='cancelled', provider_calls=0), dict(status='cancelled', provider_calls=0, usage={}),
                    dict(status='ready', provider_calls=0, usage=None), dict(status='unrecognized'), dict(key='other')]
        for i, change in enumerate(changes):
            with self.subTest(change=change):
                service, gateway, _, path = self.seed(f'invalid-{i}', **change)
                before = path.read_bytes()
                self.blocked(service, gateway)
                self.assertEqual(path.read_bytes(), before)

    def test_missing_ready_asset_keeps_receipt_and_refuses_display(self):
        service, gateway, _, path = self.seed('missing')
        before = path.read_bytes()
        self.blocked(service, gateway)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(report(service.directory)['display_unconfirmed']['total_tokens'], 23)

    def test_live_asset_loss_uses_same_recovery_rules_as_restart(self):
        service, gateway = self.service()
        gateway.generate.return_value = (PNG, 'image/png')
        service.ensure(self.sid, self.parent, subscriber='original', draw=True)
        job = next(iter(service._jobs.values()))
        job['future'].result(timeout=5)
        self.assertEqual(service.view(self.sid, self.parent)['items'][0]['status'], 'ready')
        (service.directory / (job['key'] + '.image')).unlink()
        gateway.generate.reset_mock()
        self.blocked(service, gateway)

    def test_orphan_asset_and_invalid_mime_do_not_count_as_ready(self):
        for name in ('orphan', 'mime'):
            service, gateway, key, path = self.seed(name, mime=None)
            asset = service.directory / (key + '.image')
            asset.write_bytes(PNG)
            if name == 'orphan':
                path.unlink()
            self.blocked(service, gateway)
            self.assertEqual(asset.read_bytes(), PNG)
            if name == 'orphan':
                self.assertFalse(path.exists())

    def test_interrupted_spent_task_stays_cancelled_without_new_call(self):
        for status in ('queued', 'generating'):
            service, gateway, _, path = self.seed(status, status=status, usage=None, completed=False)
            before = path.read_bytes()
            self.blocked(service, gateway)
            self.assertEqual(service.view(self.sid, self.parent)['items'][0]['status'], 'cancelled')
            self.assertEqual(path.read_bytes(), before)
            self.assertIsNone(report(service.directory)['total_tokens'])

    def test_interrupted_zero_call_queue_can_generate_once(self):
        service, gateway, key, _ = self.seed('unspent', status='queued', provider_calls=0, usage=None, completed=False)
        self.assertTrue(service.view(self.sid, self.parent)['can_generate'])
        def generate(_prompt):
            gateway.usage = {'total_tokens': 11}
            return PNG, 'image/png'
        gateway.generate.side_effect = generate
        service.ensure(self.sid, self.parent, subscriber='reader', draw=True)
        service._jobs[key]['future'].result(timeout=5)
        service.ensure(self.sid, self.parent, subscriber='second', draw=True, retry=True)
        gateway.generate.assert_called_once()
        self.assertEqual(report(service.directory)['total_tokens'], 11)

    def test_healthy_ready_recovery_and_public_priority_keep_usage_separate(self):
        service, gateway, key, path = self.seed('ready')
        asset = service.directory / (key + '.image')
        asset.write_bytes(PNG)
        self.assertEqual(service.asset(self.sid, self.parent, 0), (asset, 'image/png'))
        self.assertEqual(service.ensure(self.sid, self.parent, subscriber='reader', draw=True)['items'][0]['status'], 'ready')
        service.library.resolve.return_value = {'id': 'reviewed', 'url': '/public.png', 'source': 'published'}
        self.assertEqual(service.view(self.sid, self.parent)['items'][0]['source'], 'published')
        service.shown(self.sid, self.parent, 'public-reader', 30)
        self.assertFalse(json.loads(path.read_text())['displayed'])
        receipt = next(service.directory.glob('views-*.json'))
        self.assertEqual(json.loads(receipt.read_text())['source'], 'published')
        self.assertEqual(report(service.directory)['display_unconfirmed']['total_tokens'], 23)
        service.library.resolve.return_value = None
        service.shown(self.sid, self.parent, 'private-reader', 40)
        self.assertEqual(report(service.directory)['display_confirmed']['total_tokens'], 23)
        gateway.generate.assert_not_called()


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformImageRecovery_' + case['package_id'],
                                                        (LongformImageRecoveryTests,), {'case': case})))
    return suite
