"""Saved-choice reuse and per-attempt display attribution on official novels."""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from open_story_engine.api import create_app
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadError
from open_story_engine.api_turn_drafts import visible_choices
from open_story_engine.cocreation import MockPlanner
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests


class LongformSavedChoiceReuseTests(unittest.TestCase):
    setUp = LongformRepairFallbackTests.setUp

    def choices(self):
        _, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        return visible_choices(snapshot.history[-1], snapshot.package, snapshot.story_contract, snapshot.history)

    def turn(self, choice, request):
        return self.play.continue_turn(self.sid, self.parent, choice_id=choice['id'], request_id=request)

    def restart(self):
        self.play.drafts.close()
        self.play = PlayService(self.read, self.read.database_path.parent)
        self.addCleanup(self.play.drafts.close)

    def test_reselect_after_cache_removal_and_restart_makes_no_generation_call(self):
        choice = self.choices()[0]
        first = self.turn(choice, 'first')
        self.play.drafts.discard_session(self.sid)
        self.restart()
        with patch.object(self.play.drafts, 'generate', side_effect=AssertionError('saved choice regenerated')):
            result = self.turn(choice, 'second')
            replay = self.turn(choice, 'second')
        self.assertTrue(result['deduplicated'])
        self.assertEqual(result['branch']['id'], first['branch']['id'])
        self.assertEqual(replay['branch']['id'], first['branch']['id'])
        self.assertEqual(self.play.drafts.jobs, {})
        self.assertFalse(self.play.record_display(self.sid, first['branch']['id'])['recorded'])
        with self.read.store() as store:
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM branch_nodes WHERE session_id=?', (self.sid,)).fetchone()[0], 2)
            self.assertIsNotNone(store.connection.execute('SELECT 1 FROM turn_requests WHERE request_id=?', ('second',)).fetchone())

    def test_prepare_saved_choices_does_not_recreate_disposable_drafts(self):
        choices = self.choices()
        for i, choice in enumerate(choices):
            self.turn(choice, f'saved-{i}')
        self.play.drafts.discard_session(self.sid)
        with patch.object(self.play.drafts, 'ensure', side_effect=AssertionError('saved menu scheduled generation')):
            prepared = self.play.prepare_choices(self.sid, self.parent, 'returning-reader')['choices']
        self.assertEqual([c['id'] for c in prepared], [c['id'] for c in choices])
        self.assertTrue(all(c['status'] == 'ready' and c['metrics']['source'] == 'saved_branch' for c in prepared))
        self.assertEqual(self.play.drafts.jobs, {})

    def test_saved_choice_stream_replays_cached_text_as_deltas_after_cache_removal(self):
        choice = self.choices()[0]
        saved = self.turn(choice, 'first-stream-choice')
        self.play.drafts.discard_session(self.sid)
        self.play.drafts.close()
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=True)) as client:
            with patch.object(MockPlanner, 'plan', side_effect=AssertionError('saved stream regenerated')):
                response = client.post(f'/api/v1/sessions/{self.sid}/branches/stream', json={
                    'parent_branch_id': self.parent, 'choice_id': choice['id'], 'request_id': 'restored-stream-choice'})
            self.assertEqual(response.status_code, 200)
            events = [frame for frame in response.text.split('\n\n') if frame.startswith('event:')]
            delta_events = [frame for frame in events if frame.startswith('event: delta\n')]
            self.assertGreater(len(delta_events), 1)
            self.assertTrue(events[-1].startswith('event: done\n'))
            result = json.loads(events[-1].split('\ndata: ', 1)[1])
            self.assertTrue(result['deduplicated'])
            self.assertEqual(result['branch']['narrativeText'], saved['branch']['narrativeText'])
            self.assertEqual(result['branch']['id'], saved['branch']['id'])

    def test_saved_reuse_still_checks_request_binding_draft_key_and_ended_route(self):
        choice = self.choices()[0]
        saved = self.turn(choice, 'bound-request')
        self.play.drafts.discard_session(self.sid)
        with patch.object(self.play.drafts, 'generate', side_effect=AssertionError('invalid reuse generated')):
            with self.assertRaises(ReadError) as caught:
                self.play.continue_turn(self.sid, self.parent, text=self.action, request_id='bound-request')
            self.assertEqual(caught.exception.code, 'request_conflict')
            with self.assertRaises(ReadError) as caught:
                self.play.continue_turn(self.sid, self.parent, choice_id=choice['id'], draft_id='0' * 64, request_id='stale-key')
            self.assertEqual(caught.exception.code, 'draft_expired')
            self.play.end_route(self.sid, saved['branch']['id'])
            with self.assertRaises(ReadError) as caught:
                self.play.continue_turn(self.sid, saved['branch']['id'], choice_id=choice['id'], request_id='after-end')
            self.assertEqual(caught.exception.code, 'route_ended')

    def test_receipt_recovers_commit_metric_gap_from_saved_attempt_identity(self):
        result = self.turn(self.choices()[0], 'gap')
        job = next(iter(self.play.drafts.jobs.values()))
        self.assertEqual(result['branch']['preparedAttemptId'], job['attempt_id'])
        with self.play.drafts.condition:
            job['metrics'].pop('committed_branch_id')
            job['metrics'].pop('committed')
            self.play.drafts._save(job)
        self.restart()
        self.assertTrue(self.play.record_display(self.sid, result['branch']['id'])['recorded'])
        summary = self.play.turn_usage(self.sid)
        self.assertEqual(summary['display_confirmed']['attempts'], 1)
        self.assertEqual(summary['display_unconfirmed']['attempts'], 0)

    def test_discarded_generated_attempt_cannot_claim_a_previously_saved_result(self):
        choice = self.choices()[0]
        result = self.turn(choice, 'original')
        old = next(iter(self.play.drafts.jobs.values()))
        # Reproduce a caller already past its saved-result check when another
        # request commits. Its generated artifact is unused by the final commit.
        self.play.drafts.discard_parent(self.sid, self.parent)
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        binding['choice_id'] = choice['id']
        new = self.play.drafts.ensure(binding, choice['payload'], snapshot, foreground=True, retry=True)
        self.play.drafts.wait(new)
        self.play.drafts.release_selection(new)
        self.assertNotEqual(old['attempt_id'], new['attempt_id'])
        # Hide the preflight lookup once, as if the other commit occurred just after it.
        lookup = self.play._prepared_branch
        calls = []
        def delayed_lookup(store, sid, key):
            calls.append(1)
            return None if len(calls) == 1 else lookup(store, sid, key)
        with patch.object(self.play, '_prepared_branch', side_effect=delayed_lookup):
            reused = self.turn(choice, 'racing-selection')
        self.assertEqual(reused['branch']['id'], result['branch']['id'])
        self.assertFalse(new['metrics']['committed'])
        self.assertTrue(new['metrics']['result_reused'])
        self.assertNotIn('committed_branch_id', new['metrics'])
        self.assertTrue(self.play.record_display(self.sid, result['branch']['id'])['recorded'])
        self.assertNotIn('first_display_receipt_at', new['metrics'])
        self.assertIn('first_display_receipt_at', new['previous_attempts'][0]['metrics'])
        summary = self.play.turn_usage(self.sid)
        self.assertEqual(summary['display_confirmed']['attempts'], 1)
        self.assertEqual(summary['display_unconfirmed']['attempts'], 1)

    def test_legacy_ready_draft_receives_identity_before_first_commit(self):
        choice = self.choices()[0]
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        binding['choice_id'] = choice['id']
        job = self.play.drafts.ensure(binding, choice['payload'], snapshot, foreground=True)
        self.play.drafts.wait(job)
        self.play.drafts.release_selection(job)
        with self.play.drafts.condition:
            job.pop('attempt_id')
            self.play.drafts._save(job)
        self.restart()
        with patch.object(self.play.drafts, 'generate', side_effect=AssertionError('legacy ready result regenerated')):
            result = self.turn(choice, 'legacy')
        self.assertTrue(result['branch']['preparedAttemptId'])
        self.assertTrue(self.play.record_display(self.sid, result['branch']['id'])['recorded'])


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformSavedChoice_' + case['package_id'], (LongformSavedChoiceReuseTests,), {'case': case})))
    return suite
