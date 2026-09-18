"""Prepared turns: private state, concurrency, idempotency and restart recovery."""
import copy
import json
import os
import shutil
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService, ReadError
from open_story_engine.api_turn_drafts import TurnDrafts
from open_story_engine.cocreation import MockPlanner
from open_story_engine.content import load_runtime_story_package
from open_story_engine.storage import SessionStore

ROOT = Path(__file__).resolve().parents[1]


class TurnDraftTests(unittest.TestCase):
    def test_restored_dynamic_menu_rechecks_state_before_preparing_or_selecting(self):
        from open_story_engine.reader_choices import choice_context, validate_choices
        current = self.play.continue_turn(self.sid, self.parent, text='留在原地观察周围', request_id='menu-parent')['branch']
        self.parent = current['id']
        _, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        context = choice_context(snapshot.package, snapshot.story_contract, snapshot.history, current)
        action = '我留在原地观察眼前情况，暂时不移动或交出物品。'
        current['readerChoices'] = validate_choices({'choices': [dict(title='观察眼前情况', action=action,
            paragraphIds=['P1'], interactWith=[])]}, context, snapshot.package)
        def save(node):
            store = SessionStore(str(self.read.database_path))
            try:
                with store.connection:
                    store.connection.execute('UPDATE branch_nodes SET node_json=? WHERE id=?',
                                             (json.dumps(node, ensure_ascii=False), self.parent))
            finally:
                store.close()
        save(current)
        self.play.drafts.close()
        self.play = PlayService(self.read, self.root)
        self.addCleanup(self.play.drafts.close)
        menu = self.play.prepare_choices(self.sid, self.parent, 'restored')['choices']
        self.assertEqual(len(menu), 1)
        self.ready(menu)
        before = self.counts()
        changed = copy.deepcopy(current)
        changed['branchState']['goalLedger'] = [dict(id='new-goal', title='放下旧事', status='active', dependencies=[])]
        stale_direction = dict(id='goal-direction-ended-1', title='继续旧目标', summary='继续完成已经放下的事', statePatch={})
        changed['nextDirections'].append(stale_direction)
        save(changed)
        with patch.object(self.play.drafts, 'ensure', side_effect=AssertionError('invalid menu must not generate')):
            self.assertEqual(self.play.prepare_choices(self.sid, self.parent, 'changed')['choices'], [])
            with self.assertRaises(ReadError) as caught:
                self.play.continue_turn(self.sid, self.parent, choice_id=menu[0]['id'], request_id='invalid-click')
            self.assertEqual(caught.exception.code, 'choice_unavailable')
            with self.assertRaises(ReadError) as caught:
                self.play.continue_turn(self.sid, self.parent, direction_id=stale_direction['id'], request_id='old-direction')
            self.assertEqual(caught.exception.code, 'choice_unavailable')
        self.assertEqual(self.counts(), before)
        legacy = copy.deepcopy(current)
        del legacy['readerChoices'][0]['dependencies']
        save(legacy)
        with patch.object(self.play.drafts, 'ensure', side_effect=AssertionError('legacy menu must not generate')):
            self.assertEqual(self.play.prepare_choices(self.sid, self.parent, 'legacy')['choices'], [])
        self.assertEqual(self.counts(), before)
        # Hidden legacy suggestions do not lock the reader out of free input.
        self.assertEqual(self.play.continue_turn(self.sid, self.parent, text='留在原地观察周围', request_id='legacy-custom')['status'], 'written')
        save(current)
        self.assertEqual(len(self.play.prepare_choices(self.sid, self.parent, 'original')['choices']), 1)
        selected = self.play.continue_turn(self.sid, self.parent, choice_id=menu[0]['id'], request_id='valid-click')
        self.assertEqual(selected['status'], 'written')
        self.assertTrue(self.play.continue_turn(self.sid, self.parent, choice_id=menu[0]['id'], request_id='valid-click')['deduplicated'])

    def test_shared_foreground_cancels_only_after_last_waiter_leaves(self):
        started = threading.Event()
        release = threading.Event()
        def generate(snapshot, payload, delta, reset, validating, check):
            started.set()
            self.assertTrue(release.wait(5))
            check()
            return {'node': {}}
        manager = TurnDrafts(self.root / 'foreground-cancel.sqlite', generate)
        try:
            binding = dict(session_id='s', parent_branch_id='p', request_id='shared')
            job = manager.ensure(binding, {}, None, foreground=True)
            self.assertTrue(started.wait(5))
            self.assertIs(manager.ensure(binding, {}, None, foreground=True), job)
            manager.release_selection(job)
            self.assertTrue(job['selected'])
            self.assertEqual(job['selected_waiters'], 1)
            manager.release_selection(job)
            self.assertFalse(job['selected'])
            release.set()
            with manager.condition:
                self.assertTrue(manager.condition.wait_for(lambda: job['status'] == 'expired', 5))
            self.assertNotIn('artifact', job)
            with manager._db() as db:
                stored = json.loads(db.execute('SELECT body FROM turn_drafts').fetchone()[0])
            self.assertNotIn('selected_waiters', stored)
        finally:
            release.set()
            manager.close()

    def test_one_disconnected_waiter_does_not_cancel_shared_generation(self):
        before = self.counts()
        release = threading.Event()
        waiters = threading.Barrier(3)
        generate = self.play.drafts.generate
        wait = self.play.drafts.wait
        calls = []

        def blocked(*args):
            calls.append(1)
            args[2]('共享任务的预览。')
            self.assertTrue(release.wait(5))
            args[5]()
            return generate(*args)

        def synchronized_wait(*args):
            waiters.wait(5)
            return wait(*args)

        def disconnected(_text):
            raise ConnectionError('reader disconnected')

        with patch.object(self.play.drafts, 'generate', blocked), \
                patch.object(self.play.drafts, 'wait', synchronized_wait), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.play.continue_turn, self.sid, self.parent,
                                text='留在原地观察周围', request_id='shared', stream=disconnected)
            second = pool.submit(self.play.continue_turn, self.sid, self.parent,
                                 text='留在原地观察周围', request_id='shared')
            try:
                waiters.wait(5)
                with self.assertRaises(ConnectionError):
                    first.result(5)
                release.set()
                self.assertEqual(second.result(10)['status'], 'written')
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.counts()[0], before[0] + 1)
                self.assertFalse(next(iter(self.play.drafts.jobs.values()))['selected'])
            finally:
                release.set()

    def test_failed_revision_retains_complete_unconfirmed_body_without_committing(self):
        from open_story_engine.llm import LlmError
        before = self.counts()
        retained = '你留在原地。\n\n他回答还不知道。'
        def generate(snapshot, payload, stream, reset, validating, check):
            stream(retained)
            reset('revision')
            stream('半句')
            error = LlmError('连接中断', 'provider_timeout')
            error.retained_body = retained
            raise error
        self.play.drafts.generate = generate
        chunks = []
        with self.assertRaises(ReadError):
            self.play.continue_turn(self.sid, self.parent, text='留在原地', request_id='failed-body',
                                    stream=chunks.append, stream_reset=lambda _: chunks.clear())
        self.assertEqual(''.join(chunks), retained)
        self.assertEqual(self.counts(), before)
        self.play.drafts.close()
        restored = TurnDrafts(self.play.drafts.path, generate)
        self.addCleanup(restored.close)
        restored._initialize()
        job = next(j for j in restored.jobs.values() if j['binding'].get('request_id') == 'failed-body')
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['retained_draft']['status'], 'unconfirmed')
        self.assertNotIn('artifact', job)
        chunks.clear()
        with self.assertRaises(ReadError):
            restored.wait(job, chunks.append, lambda _: chunks.clear())
        self.assertEqual(''.join(chunks), retained)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / 'packages'
        ref = 'taixu-relics-part1/0.1.2'
        shutil.copytree(ROOT / 'content/packages' / ref, self.packages / ref)
        package = load_runtime_story_package(self.packages / ref / 'package.json', lazy=True)
        self.entry = next(iter(package['story']['entryModel']['entryPoints']))
        self.env = patch.dict(os.environ, {'STORY_PLANNER': 'mock'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.read = ReadService(self.packages, self.root / 'sessions.sqlite')
        self.play = PlayService(self.read, self.root)
        self.addCleanup(self.play.drafts.close)
        result = self.play.create_session(package['id'], package['version'], self.entry['id'], self.entry['sourceCharacterIds'][0], identity_opening=True)
        self.sid, self.parent = result['session']['id'], result['branch']['id']

    def ready(self, choices):
        manager = self.play.drafts
        with manager.condition:
            self.assertTrue(manager.condition.wait_for(lambda: all(manager.jobs[c['draft_id']]['status'] in ('ready', 'failed', 'expired') for c in choices), timeout=10))
            for c in choices:
                self.assertEqual(manager.jobs[c['draft_id']]['status'], 'ready', manager.jobs[c['draft_id']].get('error'))

    def counts(self):
        with self.read.store() as store:
            return [store.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('branch_nodes', 'game_events', 'direction_evaluations', 'llm_audits')]

    def test_ready_choices_resolve_public_art_without_image_generation(self):
        before = self.counts()
        choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
        self.ready(choices)
        with patch('open_story_engine.scene_library.SceneLibrary.resolve', return_value={'url': '/api/v1/scene-assets/book/0.1.0/scene'}) as resolve, \
                patch('open_story_engine.api_illustrations.ImageGateway.generate') as generate:
            menu = self.play.prepare_choices(self.sid, self.parent, 'reader')
        self.assertTrue(all(c['image_prefetch_url'].endswith('/scene') for c in menu['choices']))
        self.assertEqual(resolve.call_count, 2)
        generate.assert_not_called()
        self.assertEqual(before, self.counts())

    def test_all_four_visible_choices_prepare_only_one_layer(self):
        before = self.counts()
        menu = [dict(id='choice-' + str(i), title='行动' + str(i), summary='明确行动',
                     payload={'text': '留在原地观察门边的动静'}) for i in range(4)]
        with patch('open_story_engine.api_play.visible_choices', return_value=menu):
            choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
            self.assertEqual(len(choices), 4)
            self.ready(choices)
            self.assertEqual(len(self.play.drafts.jobs), 4)
            self.assertEqual(self.counts(), before)
            selected = self.select(choices[3])
            self.assertEqual(selected['status'], 'written')
            self.assertEqual(len(self.play.drafts.jobs), 4)  # no children prepared by committing
            self.assertEqual(self.counts()[0], before[0] + 1)

    def select(self, choice, request='click'):
        return self.play.continue_turn(self.sid, self.parent, choice_id=choice['id'], draft_id=choice['draft_id'], subscriber_id='reader', request_id=request)

    def test_ready_unselected_never_writes_and_parallel_clicks_commit_once(self):
        before = self.counts()
        choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
        self.assertEqual(len(choices), 2)
        self.ready(choices)
        self.assertEqual(before, self.counts())
        for c in choices:
            artifact = self.play.drafts.jobs[c['draft_id']]['artifact']
            self.assertTrue(artifact['node']['narrativeText'])
            self.assertIn('branchState', artifact['node'])
            self.assertTrue(artifact['node']['nextDirections'])
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.select(choices[0]), range(2)))
        self.assertEqual(results[0]['branch']['id'], results[1]['branch']['id'])
        self.assertEqual(self.counts()[0], before[0] + 1)
        same = self.select(choices[0], 'another-click')
        self.assertEqual(same['branch']['id'], results[0]['branch']['id'])
        with self.assertRaises(ReadError):
            self.select(choices[1], 'click')
        with self.assertRaises(ReadError):
            self.select(choices[1], 'another-click')
        self.assertEqual(before[1], self.counts()[1])

    def test_ready_choice_replays_cached_text_to_selected_stream(self):
        choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
        self.ready(choices)
        choice = choices[0]
        expected = self.play.drafts.jobs[choice['draft_id']]['artifact']['node']['narrativeText']
        chunks = []
        result = self.play.continue_turn(
            self.sid, self.parent,
            choice_id=choice['id'],
            draft_id=choice['draft_id'],
            subscriber_id='reader',
            request_id='replay-ready',
            stream=chunks.append,
        )
        self.assertEqual(result['status'], 'written')
        self.assertEqual(''.join(chunks), expected)

    def test_ready_survives_restart_without_a_model_call(self):
        choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
        self.ready(choices)
        self.play.drafts.close()
        self.play = PlayService(self.read, self.root)
        self.addCleanup(self.play.drafts.close)
        with patch.object(MockPlanner, 'plan', side_effect=AssertionError('must reuse ready prose')):
            resumed = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
            self.assertEqual(resumed[0]['status'], 'ready')
            self.assertEqual(self.select(resumed[0])['status'], 'written')

    def test_changed_prompt_catalog_rejects_old_ready_draft_without_writes(self):
        from open_story_engine.api_turn_drafts import RULES_VERSION
        from open_story_engine.prompts import catalog_version
        self.assertIn(catalog_version(), RULES_VERSION)
        choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
        self.ready(choices)
        before = self.counts()
        with patch('open_story_engine.api_play.RULES_VERSION', RULES_VERSION + '-changed'):
            with self.assertRaises(ReadError) as caught:
                self.select(choices[0])
        self.assertEqual(caught.exception.code, 'draft_expired')
        self.assertEqual(self.counts(), before)

    def test_parent_change_and_foreign_draft_are_rejected(self):
        choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
        self.ready(choices)
        wrong = dict(choices[0], draft_id='0' * 64)
        with self.assertRaises(ReadError) as caught:
            self.select(wrong)
        self.assertEqual(caught.exception.code, 'draft_expired')
        store = SessionStore(str(self.read.database_path))
        with store.connection:
            row = store.branch(self.sid, self.parent)
            row['branchState']['playerName'] = 'changed'
            store.connection.execute('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(row), self.parent))
        store.close()
        with self.assertRaises(ReadError) as caught:
            self.select(choices[0])
        self.assertEqual(caught.exception.code, 'draft_expired')
        self.assertEqual(self.counts()[0], 1)

    def test_atomic_commit_rolls_back_evaluation_and_branch_on_failure(self):
        choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
        self.ready(choices)
        before = self.counts()
        original = SessionStore.append_branch
        def fail(store, *args):
            original(store, *args)
            raise ValueError('simulated failure after insert')
        with patch.object(SessionStore, 'append_branch', fail):
            with self.assertRaises(ReadError):
                self.select(choices[0])
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.select(choices[0])['status'], 'written')

    def test_inflight_selection_reuses_call_and_foreground_has_reserved_slot(self):
        original = MockPlanner.plan
        calls = []
        two_started = threading.Event()
        foreground_started = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        def blocked(planner, *args, **kwargs):
            with guard:
                calls.append(id(planner))
                if len(calls) >= 2:
                    two_started.set()
                if len(calls) >= 3:
                    foreground_started.set()
            self.assertTrue(release.wait(10))
            return original(planner, *args, **kwargs)
        try:
            with patch.object(MockPlanner, 'plan', blocked), ThreadPoolExecutor(2) as pool:
                choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
                self.assertTrue(two_started.wait(5))
                selected = pool.submit(self.select, choices[0])
                custom = pool.submit(self.play.continue_turn, self.sid, self.parent, text='留在原地观察周围', request_id='custom')
                self.assertTrue(foreground_started.wait(5))
                self.assertEqual(len(calls), 3)  # two drafts + custom, no fourth generation
                self.assertEqual(len(set(calls)), 3)  # private planners
                self.assertEqual(self.counts()[0], 1)
                release.set()
                self.assertEqual(selected.result(10)['status'], 'written')
                self.assertEqual(custom.result(10)['status'], 'written')
        finally:
            release.set()

    def test_cancel_queued_and_running_jobs_preserves_other_subscription(self):
        started = threading.Barrier(3)
        release = threading.Event()
        calls = []
        def generate(snapshot, payload, delta, reset, validating, check):
            calls.append(payload)
            started.wait(5)
            release.wait(5)
            check()
            return {'node': {}}
        manager = TurnDrafts(self.root / 'cancel.sqlite', generate)
        binding = dict(session_id='s', parent_branch_id='p')
        try:
            first = manager.ensure(dict(binding, choice_id='one'), {}, None, 'tab-a')
            manager.ensure(dict(binding, choice_id='one'), {}, None, 'tab-b')
            second = manager.ensure(dict(binding, choice_id='two'), {}, None, 'tab-a')
            started.wait(5)
            queued = manager.ensure(dict(binding, choice_id='three'), {}, None, 'tab-a')
            manager.release('s', 'p', 'tab-a')
            self.assertEqual(queued['status'], 'expired')
            release.set()
            with manager.condition:
                self.assertTrue(manager.condition.wait_for(lambda: first['status'] == 'ready' and second['status'] == 'expired', 5))
            self.assertEqual(len(calls), 2)
        finally:
            release.set()
            manager.close()

    def test_compound_action_guard_rejects_omissions_and_invented_evidence(self):
        from open_story_engine.api_reader_quality import action_requirements, validate_action_requirements
        requested = action_requirements('先查看手伤，再询问伤者呼吸，不进入山门。')
        body = '你查看手背的红肿。你询问伤者的呼吸，门内答说仍有气息。你一直站在山门外。'
        review = {'issues': [], 'actions': [
            {'id': 'A1', 'status': 'performed', 'summary': '查看了手伤', 'evidence': '你查看手背的红肿。'},
            {'id': 'A2', 'status': 'performed', 'summary': '确认伤者仍有呼吸', 'evidence': '你询问伤者的呼吸，门内答说仍有气息。'},
            {'id': 'A3', 'status': 'performed', 'summary': '留在山门外', 'evidence': '你一直站在山门外。'},
        ]}
        self.assertEqual(len(validate_action_requirements(review, requested, body)['actions']), 3)
        from open_story_engine.api_narrative import player_action
        full = '先查看手伤，再询问伤者呼吸，不进入山门。'
        selected = {'title': '自定行动：先查看手伤', 'summary': full, 'isFreeText': True}
        self.assertEqual(action_requirements(player_action({}, selected)), requested)
        by_paragraph = copy.deepcopy(review)
        for item in by_paragraph['actions']:
            item.pop('evidence')
            item['paragraphId'] = 'P1'
        self.assertEqual(validate_action_requirements(by_paragraph, requested, body)['actions'][0]['evidence'], body)
        by_paragraph['actions'][0]['paragraphId'] = 'P999'
        with self.assertRaises(ValueError):
            validate_action_requirements(by_paragraph, requested, body)

        missing = copy.deepcopy(review)
        missing['actions'].pop()
        with self.assertRaises(ValueError):
            validate_action_requirements(missing, requested, body)
        review['actions'][1]['evidence'] = '没有发生的回答'
        with self.assertRaises(ValueError):
            validate_action_requirements(review, requested, body)

    def test_delete_save_removes_its_private_drafts(self):
        choices = self.play.prepare_choices(self.sid, self.parent, 'reader')['choices']
        self.ready(choices)
        self.play.delete_session(self.sid)
        self.assertFalse(any(j['binding']['session_id'] == self.sid for j in self.play.drafts.jobs.values()))
        with self.play.drafts._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM turn_drafts').fetchone()[0], 0)

    def test_interrupted_draft_expires_after_restart(self):
        manager = self.play.drafts
        with manager.condition:
            manager._initialize()
            manager.jobs['orphan'] = dict(key='orphan', binding={}, status='generating', created=0, metrics={})
            manager._save(manager.jobs['orphan'])
        restarted = TurnDrafts(manager.path, lambda *args: None)
        with restarted.condition:
            restarted._initialize()
            self.assertEqual(restarted.jobs['orphan']['status'], 'expired')
        restarted.close()
