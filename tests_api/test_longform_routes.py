"""Long-novel route endings cancel speculative work without closing sibling history."""
import os
import copy
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_support.longform import ROOT, longform_cases
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService, ReadError
from open_story_engine.content import load_runtime_story_package


class LongformRouteTests(unittest.TestCase):
    def test_nonofficial_mode_is_hidden_and_cannot_start_or_continue(self):
        for case in longform_cases():
            with self.subTest(book=case['package_id']), TemporaryDirectory() as tmp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
                package = load_runtime_story_package(case['path'], lazy=True)
                cid = package['story']['entryModel']['sourceCharacterIds'][0]
                entry = next(c['defaultEntryPointId'] for c in package['characters'] if c['id'] == cid)
                read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
                play = PlayService(read, Path(tmp))
                self.addCleanup(play.drafts.close)
                start = play.create_session(case['package_id'], case['version'], entry, cid, identity_opening=True)
                # Use the same real long novel with a disabled policy, never a retired story fixture.
                disabled = copy.deepcopy(package)
                disabled['story']['entryModel']['policy'] = 'disabled'
                with patch.object(read, 'load_package', return_value=(case['path'], disabled)):
                    self.assertEqual(read.packages()['packages'], [])
                    for call in (lambda: play.create_session(case['package_id'], case['version'], entry, cid),
                                 lambda: play.continue_turn(start['session']['id'], start['branch']['id'], text='继续等待')):
                        with self.assertRaises(ReadError) as caught:
                            call()
                        self.assertEqual(caught.exception.code, 'package_retired')

    def test_all_books_all_roles_end_ready_jobs_and_reject_stale_snapshot(self):
        for case in longform_cases():
            package = load_runtime_story_package(case['path'], lazy=True)
            for cid in package['story']['entryModel']['sourceCharacterIds']:
                with self.subTest(book=case['package_id'], role=cid), TemporaryDirectory() as tmp, \
                        patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
                    read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
                    play = PlayService(read, Path(tmp))
                    self.addCleanup(play.drafts.close)
                    entry = next(c['defaultEntryPointId'] for c in package['characters'] if c['id'] == cid)
                    start = play.create_session(case['package_id'], case['version'], entry, cid, identity_opening=True)
                    sid, root = start['session']['id'], start['branch']
                    child = play.continue_turn(sid, root['id'], text='留在原地观察周围', request_id='child')['branch']
                    binding, snapshot = play._turn_snapshot(sid, child['id'])
                    # Queue a real, independently prepared turn before closing this leaf.
                    job = play._ensure_turn(dict(binding, request_id='prepared'), {'text': '留在原地观察周围'}, snapshot, subscriber='tab')
                    play.drafts.wait(job)
                    self.assertEqual(job['status'], 'ready')
                    artifact = job['artifact']
                    ledger = child['branchState']['threadLedger']
                    self.assertEqual(play.end_route(sid, child['id'])['status'], 'abandoned')
                    self.assertEqual(job['status'], 'expired')
                    self.assertNotIn('artifact', job)
                    with self.assertRaises(ReadError) as caught:
                        play._commit_turn(job, artifact, {'text': '留在原地观察周围'}, 'late-commit')
                    self.assertEqual(caught.exception.code, 'route_ended')
                    self.assertEqual(read.journey(sid, child['id'])['threads'], ledger)
                    for call in (lambda: play.prepare_choices(sid, child['id'], 'other-tab'),
                                 lambda: play.continue_turn(sid, child['id'], text='继续等待', request_id='after-end'),
                                 lambda: play._ensure_turn(binding, {'text': '旧快照重试'}, snapshot, foreground=True)):
                        with self.assertRaises(ReadError) as caught:
                            call()
                        self.assertEqual(caught.exception.code, 'route_ended')
                    play.drafts.close()
                    restored = PlayService(read, Path(tmp))
                    self.addCleanup(restored.drafts.close)
                    with self.assertRaises(ReadError) as caught:
                        restored.continue_turn(sid, child['id'], text='继续等待', request_id='restart')
                    self.assertEqual(caught.exception.code, 'route_ended')
                    sibling = restored.continue_turn(sid, root['id'], text='留在原地观察周围', request_id='sibling')['branch']
                    self.assertEqual(read.journey(sid, sibling['id'])['status'], 'active')
                    with self.assertRaises(ReadError) as caught:
                        restored.end_route(sid, root['id'])
                    self.assertEqual(caught.exception.code, 'route_has_continuation')

    def test_end_running_job_stops_at_next_callback_and_never_saves_branch(self):
        for case in longform_cases():
            with self.subTest(book=case['package_id']), TemporaryDirectory() as tmp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
                package = load_runtime_story_package(case['path'], lazy=True)
                cid = package['story']['entryModel']['sourceCharacterIds'][0]
                entry = next(c['defaultEntryPointId'] for c in package['characters'] if c['id'] == cid)
                read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
                play = PlayService(read, Path(tmp))
                self.addCleanup(play.drafts.close)
                start = play.create_session(case['package_id'], case['version'], entry, cid, identity_opening=True)
                sid, bid = start['session']['id'], start['branch']['id']
                binding, snapshot = play._turn_snapshot(sid, bid)
                entered, release, finished = threading.Event(), threading.Event(), threading.Event()
                def generate(snapshot, payload, delta, reset, validating, check):
                    entered.set()
                    try:
                        self.assertTrue(release.wait(5))
                        delta('你仍留在原地。')
                        self.fail('结束后的生成不得越过下一次取消检查')
                    finally:
                        finished.set()
                play.drafts.generate = generate
                try:
                    job = play._ensure_turn(binding, {'text': '继续等待'}, snapshot, foreground=True)
                    self.assertTrue(entered.wait(5))
                    play.end_route(sid, bid)
                    self.assertEqual(job['status'], 'expired')
                    with self.assertRaises(ReadError):
                        play.drafts.wait(job)
                finally:
                    release.set()
                    self.assertTrue(finished.wait(5))
                with play.drafts.condition:
                    self.assertTrue(play.drafts.condition.wait_for(lambda: sum(play.drafts.workers) == 0, timeout=5))
                self.assertEqual(job['status'], 'expired')
                self.assertNotIn('artifact', job)
                with read.store() as store:
                    self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM branch_nodes').fetchone()[0], 1)
