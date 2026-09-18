"""Explicit rechoosing creates new history while retries retain their identity."""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.api_read import ReadError
from open_story_engine.api_turn_drafts import digest
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_saved_choice_reuse import LongformSavedChoiceReuseTests


class LongformRechoiceHistoryTests(unittest.TestCase):
    setUp = LongformSavedChoiceReuseTests.setUp
    choices = LongformSavedChoiceReuseTests.choices
    turn = LongformSavedChoiceReuseTests.turn
    restart = LongformSavedChoiceReuseTests.restart

    def rechoose(self, choice, history='new-history', request='rechoice', **kwargs):
        return self.play.continue_turn(self.sid, self.parent, choice_id=choice['id'],
                                       history_id=history, request_id=request, **kwargs)

    def test_same_choice_after_ending_creates_active_sibling_and_preserves_old_route(self):
        choice = self.choices()[0]
        old = self.turn(choice, 'original')['branch']
        self.play.end_route(self.sid, old['id'])
        closure = self.read.route_closure(self.sid, old['id'])
        self.restart()
        new = self.rechoose(choice)['branch']
        self.assertNotEqual(new['id'], old['id'])
        self.assertEqual(new['parentId'], old['parentId'])
        self.assertEqual(new['turnInput']['history_id'], 'new-history')
        self.assertEqual(self.read.journey(self.sid, new['id'])['status'], 'active')
        self.assertEqual(self.read.route_closure(self.sid, old['id']), closure)
        self.assertEqual(self.read.branch_view(self.sid, old['id']), old)
        with self.assertRaises(ReadError) as error:
            self.play.prepare_choices(self.sid, old['id'], 'reader', 'cannot-revive-ended')
        self.assertEqual(error.exception.code, 'route_ended')
        with self.assertRaises(ReadError) as error:
            self.play.continue_turn(self.sid, old['id'], text=self.action, history_id='cannot-revive-ended')
        self.assertEqual(error.exception.code, 'route_ended')

    def test_prepare_poll_selection_and_restart_share_only_the_new_history(self):
        for i, choice in enumerate(self.choices()):
            self.turn(choice, f'old-{i}')
        first = self.play.prepare_choices(self.sid, self.parent, 'reader', 'replay-a')['choices']
        jobs = [j for j in self.play.drafts.jobs.values() if j['binding'].get('history_id') == 'replay-a']
        self.assertEqual(len(jobs), len(first))
        for job in jobs:
            self.play.drafts.wait(job)
            self.assertEqual(job['binding']['parent_branch_id'], self.parent)
        repeated = self.play.prepare_choices(self.sid, self.parent, 'reader', 'replay-a')['choices']
        self.assertEqual([c['draft_id'] for c in first], [c['draft_id'] for c in repeated])
        self.assertTrue(all(c['status'] == 'ready' for c in repeated))
        self.restart()
        choice = first[0]
        with patch.object(self.play.drafts, 'generate', side_effect=AssertionError('new history ready draft regenerated')):
            new = self.rechoose(choice, 'replay-a', draft_id=choice['draft_id'])
            self.assertEqual(self.rechoose(choice, 'replay-a')['branch']['id'], new['branch']['id'])
            # Another click ID in the same history converges to the same result.
            self.assertEqual(self.rechoose(choice, 'replay-a', 'second-click')['branch']['id'], new['branch']['id'])
        self.assertNotEqual(self.rechoose(choice, 'replay-b', 'third-click')['branch']['id'], new['branch']['id'])

    def test_history_is_part_of_request_identity_and_draft_binding(self):
        choice = self.choices()[0]
        self.rechoose(choice)
        for history in (None, 'other-history'):
            with self.assertRaises(ReadError) as error:
                self.rechoose(choice, history)
            self.assertEqual(error.exception.code, 'request_conflict')
        base, _ = self.play._turn_snapshot(self.sid, self.parent)
        old_key = digest(dict(base, choice_id=choice['id']))
        with patch.object(self.play.drafts, 'generate', side_effect=AssertionError('invalid binding generated')):
            with self.assertRaises(ReadError) as error:
                self.rechoose(choice, 'another-history', 'new-request', draft_id=old_key)
        self.assertEqual(error.exception.code, 'draft_expired')

    def test_custom_action_records_history_but_does_not_send_it_to_generator(self):
        generate = self.play.drafts.generate
        observed = []
        def capture(snapshot, payload, *args):
            observed.append(payload)
            return generate(snapshot, payload, *args)
        self.play.drafts.generate = capture
        result = self.play.continue_turn(self.sid, self.parent, text=self.action,
                                        history_id='custom-history', request_id='custom')
        self.assertEqual(observed, [{'text': self.action}])
        self.assertEqual(result['branch']['turnInput'], {'text': self.action, 'history_id': 'custom-history'})
        self.assertTrue(self.play.continue_turn(self.sid, self.parent, text=self.action,
                                               history_id='custom-history', request_id='custom')['deduplicated'])

    def test_old_new_and_unused_attempt_costs_remain_separate(self):
        generate = self.play.drafts.generate
        def counted(*args):
            artifact = generate(*args)
            artifact['usage'] = dict(calls=1, reported_tokens=17, unreported_calls=0)
            return artifact
        self.play.drafts.generate = counted
        choice = self.choices()[0]
        old = self.turn(choice, 'original')['branch']
        self.play.record_display(self.sid, old['id'])
        self.play.end_route(self.sid, old['id'])
        prepared = self.play.prepare_choices(self.sid, self.parent, 'reader', 'new-history')['choices']
        jobs = [j for j in self.play.drafts.jobs.values() if j['binding'].get('history_id') == 'new-history']
        for job in jobs:
            self.play.drafts.wait(job)
        new = self.rechoose(prepared[0], draft_id=prepared[0]['draft_id'])['branch']
        self.play.record_display(self.sid, new['id'])
        self.play.drafts.release(self.sid, self.parent, 'reader')
        usage = self.play.turn_usage(self.sid)
        self.assertEqual(usage['display_confirmed']['tokens'], 34)
        self.assertEqual(usage['display_unconfirmed']['tokens'], 17 * (len(prepared) - 1))
        self.restart()
        self.assertEqual(self.play.turn_usage(self.sid), usage)
        self.play.record_display(self.sid, old['id'])
        self.play.record_display(self.sid, new['id'])
        self.assertEqual(self.play.turn_usage(self.sid), usage)

    def test_http_and_stream_forward_history_and_reject_invalid_values(self):
        choice = self.choices()[0]
        old = self.turn(choice, 'old')['branch']
        self.play.end_route(self.sid, old['id'])
        base = f'/api/v1/sessions/{self.sid}'
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=True)) as client:
            body = dict(parent_branch_id=self.parent, choice_id=choice['id'], history_id='http-history', request_id='http')
            prepared = client.post(base + '/choices/prepare', json=dict(parent_branch_id=self.parent,
                                  subscriber_id='http-reader', history_id='http-history'))
            self.assertEqual(prepared.status_code, 200, prepared.text)
            key = next(c['draft_id'] for c in prepared.json()['choices'] if c['id'] == choice['id'])
            body['draft_id'] = key
            result = client.post(base + '/branches', json=body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertNotEqual(result.json()['branch']['id'], old['id'])
            streamed = client.post(base + '/branches/stream', json=dict(body, history_id='stream-history',
                                   request_id='stream', draft_id=None))
            frames = [f for f in streamed.text.split('\n\n') if f.startswith('event: done\n')]
            self.assertEqual(len(frames), 1, streamed.text)
            written = json.loads(frames[0].split('\ndata: ', 1)[1])['branch']
            self.assertEqual(written['turnInput']['history_id'], 'stream-history')
            self.assertNotEqual(written['id'], result.json()['branch']['id'])
            for invalid in ('', ' ', 'a' * 201, True):
                response = client.post(base + '/choices/prepare', json=dict(parent_branch_id=self.parent,
                                       subscriber_id='invalid-reader', history_id=invalid))
                self.assertEqual(response.status_code, 422, response.text)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformRechoice_' + case['package_id'],
                                                        (LongformRechoiceHistoryTests,), {'case': case})))
    return suite
