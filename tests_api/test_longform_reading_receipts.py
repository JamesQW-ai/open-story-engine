"""Display receipts bind to saved official-novel turns, without story writes."""
import hashlib
import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from open_story_engine.api import create_app
from open_story_engine.api_read import ReadError
from open_story_engine.api_turn_drafts import TurnDrafts
from open_story_engine.llm import LlmError
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests


class LongformReadingReceiptTests(unittest.TestCase):
    setUp = LongformRepairFallbackTests.setUp

    def client(self):
        return self.enterContext(TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=True)))

    def generate(self, request_id='shown-turn'):
        original = self.play.drafts.generate
        def counted(*args):
            artifact = original(*args)
            artifact['usage'] = dict(calls=1, reported_tokens=17, unreported_calls=0)
            return artifact
        self.play.drafts.generate = counted
        return self.play.continue_turn(self.sid, self.parent, text=self.action, request_id=request_id)['branch']

    def test_saved_turn_is_unconfirmed_until_receipt_and_duplicates_do_not_add_cost(self):
        branch = self.generate()
        self.play.drafts.close()
        client = self.client()
        url = f'/api/v1/sessions/{self.sid}'
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        initial = client.get(url + '/turn-usage').json()
        self.assertEqual(initial['display_unconfirmed']['tokens'], 17)
        self.assertEqual(initial['display_confirmed']['attempts'], 0)
        first = client.post(url + '/reading-receipts', json={'branch_id': branch['id']})
        self.assertEqual(first.json(), {'recorded': True, 'deduplicated': False})
        repeated = client.post(url + '/reading-receipts', json={'branch_id': branch['id']})
        self.assertEqual(repeated.json(), {'recorded': True, 'deduplicated': True})
        summary = client.get(url + '/turn-usage').json()
        self.assertEqual(summary['display_confirmed']['tokens'], 17)
        self.assertEqual(summary['display_confirmed']['attempts'], 1)
        self.assertEqual(summary['display_unconfirmed']['attempts'], 0)
        self.assertEqual(self.client().get(url + '/turn-usage').json(), summary)
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_invalid_cross_session_and_client_supplied_cost_are_rejected(self):
        branch = self.generate()
        self.play.drafts.close()
        client = self.client()
        url = f'/api/v1/sessions/{self.sid}'
        self.assertEqual(client.post('/api/v1/sessions/other/reading-receipts', json={'branch_id': branch['id']}).status_code, 404)
        self.assertEqual(client.post(url + '/reading-receipts', json={'branch_id': 'unknown'}).status_code, 404)
        self.assertEqual(client.post(url + '/reading-receipts', json={'branch_id': branch['id'], 'tokens': 99}).status_code, 422)
        self.assertEqual(client.post(url + '/reading-receipts', json={'branch_id': self.parent}).json()['recorded'], False)
        self.assertEqual(client.get(url + '/turn-usage').json()['display_unconfirmed']['tokens'], 17)

    def test_failed_attempt_cost_stays_unconfirmed_when_retry_is_displayed(self):
        original = self.play.drafts.generate
        def fail(*_args):
            error = LlmError('injected failure')
            error.draft_usage = dict(calls=1, reported_tokens=7, unreported_calls=1)
            raise error
        self.play.drafts.generate = fail
        with self.assertRaises(ReadError):
            self.play.continue_turn(self.sid, self.parent, text=self.action, request_id='retry')
        self.play.drafts.generate = original
        branch = self.generate('retry')
        self.play.drafts.close()
        client = self.client()
        url = f'/api/v1/sessions/{self.sid}'
        self.assertTrue(client.post(url + '/reading-receipts', json={'branch_id': branch['id']}).json()['recorded'])
        summary = client.get(url + '/turn-usage').json()
        self.assertEqual(summary['display_confirmed']['tokens'], 17)
        self.assertEqual(summary['display_unconfirmed']['reported_tokens'], 7)
        self.assertIsNone(summary['display_unconfirmed']['tokens'])
        self.assertEqual(summary['display_unconfirmed']['attempts'], 1)

    def test_statistics_read_never_initializes_or_changes_databases(self):
        client = self.client()
        url = f'/api/v1/sessions/{self.sid}/turn-usage'
        self.assertFalse(self.play.drafts.path.exists())
        with patch.object(TurnDrafts, '_initialize', side_effect=AssertionError('read initialized database')):
            self.assertEqual(client.get(url).json()['display_unconfirmed']['attempts'], 0)
        self.assertFalse(self.play.drafts.path.exists())
        self.generate()
        self.play.drafts.close()
        paths = [self.read.database_path, self.play.drafts.path]
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        with patch.object(TurnDrafts, '_initialize', side_effect=AssertionError('read initialized database')):
            self.assertEqual(client.get(url).json()['display_unconfirmed']['tokens'], 17)
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_eviction_receipt_does_not_recreate_cost_record(self):
        branch = self.generate()
        self.play.drafts.discard_session(self.sid)
        self.play.drafts.close()
        client = self.client()
        url = f'/api/v1/sessions/{self.sid}'
        result = client.post(url + '/reading-receipts', json={'branch_id': branch['id']})
        self.assertEqual(result.json(), {'recorded': False, 'reason': 'measurement_unavailable'})
        self.assertEqual(client.get(url + '/turn-usage').json()['display_confirmed']['attempts'], 0)
        with sqlite3.connect(self.play.drafts.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM turn_drafts').fetchone()[0], 0)

    def test_receipt_cannot_mark_a_replacement_attempt_with_the_same_key(self):
        branch = self.generate()
        job = next(iter(self.play.drafts.jobs.values()))
        with self.play.drafts.condition:
            job['metrics']['committed_branch_id'] = 'different-branch'
            self.play.drafts._save(job)
        self.play.drafts.close()
        client = self.client()
        url = f'/api/v1/sessions/{self.sid}'
        self.assertFalse(client.post(url + '/reading-receipts', json={'branch_id': branch['id']}).json()['recorded'])
        self.assertEqual(client.get(url + '/turn-usage').json()['display_unconfirmed']['tokens'], 17)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformReadingReceipts_' + case['package_id'], (LongformReadingReceiptTests,), {'case': case})))
    return suite
