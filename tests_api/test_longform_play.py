"""HTTP write reliability, parameterized over every verified 100k-CJK novel.

Only MockPlanner and temporary package/database copies are used. These assertions
establish persistence and delivery contracts, not model narrative quality.
"""
import json
import os
import shutil
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from test_support.longform import longform_cases
from open_story_engine.api import create_app
from open_story_engine.api_turn_drafts import TurnDrafts
from open_story_engine.cocreation import MockPlanner
from open_story_engine.content import load_runtime_story_package


class LongformPlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / 'packages'
        self.package_dir = self.packages / self.case['package_id'] / self.case['version']
        shutil.copytree(self.case['path'].parent, self.package_dir)
        package = load_runtime_story_package(self.package_dir / 'package.json', lazy=True)
        self.character = package['story']['entryModel']['sourceCharacterIds'][0]
        entry_id = next(c['defaultEntryPointId'] for c in package['characters'] if c['id'] == self.character)
        self.entry = next(e for e in package['story']['entryModel']['entryPoints'] if e['id'] == entry_id)
        self.database = self.root / 'sessions.sqlite'
        self.enterContext(patch.dict(os.environ, {'STORY_PLANNER': 'mock', 'STORY_API_PLAY': '1'}))
        self.client = self.enterContext(TestClient(create_app(self.packages, self.database, play=True)))

    def create_session(self, character=None):
        return self.client.post('/api/v1/sessions', json={
            'package': {'package_id': self.case['package_id'], 'version': self.case['version']},
            'entry_point_id': self.entry['id'],
            'source_character_id': character or self.character,
            'identity_opening': True,
        })

    def test_delete_save_removes_all_dependents_and_preserves_other_save(self):
        first = self.create_session().json()
        sid = first["session"]["id"]
        branch = first["branch"]
        other_start = self.create_session().json()
        other = other_start["session"]["id"]
        result = self.client.post(f"/api/v1/sessions/{sid}/branches", json={
            "parent_branch_id": branch["id"], "text": "留在原地观察周围", "request_id": "delete-child",
        })
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "written")
        for target_sid, target_bid in ((sid, result.json()['branch']['id']), (other, other_start['branch']['id'])):
            planned = self.client.post(f'/api/v1/sessions/{target_sid}/route-closure',
                                       json=dict(branch_id=target_bid, intended_type='normal'))
            self.assertEqual(planned.status_code, 200, planned.text)
        before_packages = {p.relative_to(self.packages): p.read_bytes() for p in self.packages.rglob("*") if p.is_file()}
        with sqlite3.connect(self.database) as db:
            # Seed the CLI-only and model-audit records as well as the real play tree.
            db.execute("INSERT INTO game_events(session_id,sequence,request_id,player_input,event_type,action_json,resolution_json,state_patch_json,state_after_json,created_at) VALUES (?,0,'delete-event','x','x','{}','{}','{}','{}','now')", (sid,))
            db.execute("INSERT INTO event_narrations(session_id,sequence,text,created_at) VALUES (?,0,'test','now')", (sid,))
            db.execute("INSERT OR IGNORE INTO session_story_contracts VALUES (?,'{}','now')", (sid,))
            db.execute("INSERT OR IGNORE INTO session_derived_story_packages VALUES (?,'{}','now')", (sid,))
            db.execute("INSERT INTO llm_audits(session_id,operation,model,prompt_version,request_summary,created_at) VALUES (?,'test','mock','test','test','now')", (sid,))
            db.execute("INSERT INTO direction_evaluator_audits(session_id,parent_branch_id,model,prompt_version,request_summary,created_at) VALUES (?,?,'mock','test','test','now')", (sid, branch["id"]))
            db.execute("INSERT INTO direction_evaluations(session_id,parent_branch_id,player_direction,evaluation_json,created_at) VALUES (?,?,'test','{}','now')", (sid, branch["id"]))
            db.execute("INSERT OR IGNORE INTO session_play_preferences VALUES (?, ?, ?)", (sid, "临时存档", "{}"))
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if row[0] != 'sqlite_sequence']
            dependents = [table for table in tables if any(row[1] == 'session_id' for row in db.execute(f"PRAGMA table_info({table})"))]
            for table in dependents:
                self.assertGreater(db.execute(f"SELECT count(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0], 0, table)
            other_before = {table: db.execute(f"SELECT * FROM {table} WHERE session_id=?", (other,)).fetchall() for table in dependents}
        response = self.client.delete(f"/api/v1/sessions/{sid}")
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b'')
        self.assertEqual(self.client.get(f"/api/v1/sessions/{sid}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/sessions/{sid}/branches").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/sessions/{other}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/sessions/{sid}").status_code, 404)
        with sqlite3.connect(self.database) as db:
            self.assertIsNone(db.execute("SELECT id FROM game_sessions WHERE id=?", (sid,)).fetchone())
            for table in dependents:
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0], 0, table)
                self.assertEqual(db.execute(f"SELECT * FROM {table} WHERE session_id=?", (other,)).fetchall(), other_before[table], table)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(before_packages, {p.relative_to(self.packages): p.read_bytes() for p in self.packages.rglob("*") if p.is_file()})

    def test_delete_missing_database_does_not_create_it(self):
        response = self.client.delete('/api/v1/sessions/missing')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.database.exists())

    def test_read_mode_cannot_delete_existing_save(self):
        sid = self.create_session().json()["session"]["id"]
        before = self.database.read_bytes()
        with TestClient(create_app(self.packages, self.database, play=False)) as client:
            response = client.delete(f"/api/v1/sessions/{sid}")
            self.assertEqual(response.status_code, 404)
        self.assertEqual(before, self.database.read_bytes())

    def test_delete_transaction_rolls_back_all_records_on_failure(self):
        from open_story_engine.storage import SessionStore
        sid = self.create_session().json()["session"]["id"]
        store = SessionStore(str(self.database))
        self.addCleanup(store.close)
        before = list(store.connection.iterdump())
        store.connection.execute("CREATE TRIGGER reject_save_delete BEFORE DELETE ON game_sessions BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.delete_session(sid)
        store.connection.execute("DROP TRIGGER reject_save_delete")
        self.assertEqual(before, list(store.connection.iterdump()))

    def test_delete_available_with_unavailable_package(self):
        sid = self.create_session().json()["session"]["id"]
        shutil.rmtree(self.package_dir)
        self.assertEqual(self.client.delete(f"/api/v1/sessions/{sid}").status_code, 204)

    def test_play_cors_allows_delete(self):
        response = self.client.options('/api/v1/sessions/test', headers={
            'Origin': 'http://127.0.0.1:5173', 'Access-Control-Request-Method': 'DELETE',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('DELETE', response.headers['access-control-allow-methods'])

    def stream_events(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/event-stream", response.headers["content-type"])
        events = []
        for frame in response.text.split("\n\n"):
            if not frame.startswith("event:"):
                continue
            lines = frame.splitlines()
            events.append((lines[0][7:], json.loads(lines[1][6:])))
        return events

    def test_stream_sends_prose_then_committed_result_and_deduplicates(self):
        body = self.create_session().json()
        sid, root = body["session"]["id"], body["branch"]["id"]
        payload = {"parent_branch_id": root, "text": "留在原地观察周围", "request_id": "stream-free"}
        events = self.stream_events(self.client.post(f"/api/v1/sessions/{sid}/branches/stream", json=payload))
        self.assertEqual(events[0][0], "delta")
        self.assertTrue(events[0][1]["text"])
        self.assertEqual(events[-1][0], "done")
        result = events[-1][1]
        self.assertEqual(result["status"], "written")
        saved = self.client.get(f"/api/v1/sessions/{sid}/branches/{result['branch']['id']}").json()
        self.assertEqual(saved["narrativeText"], result["branch"]["narrativeText"])
        retry = self.stream_events(self.client.post(f"/api/v1/sessions/{sid}/branches/stream", json=payload))
        self.assertEqual([event for event, _ in retry], ["done"])
        self.assertTrue(retry[-1][1]["deduplicated"])
        self.assertEqual(retry[-1][1]["branch"]["id"], result["branch"]["id"])

    def test_stream_reset_and_failure_do_not_publish_a_branch(self):
        from open_story_engine.cocreation import MockPlanner
        from open_story_engine.llm import LlmError
        body = self.create_session().json()
        sid, root = body["session"]["id"], body["branch"]["id"]
        def fail(_planner, context, selected, resolved, stream, stream_reset, **kwargs):
            stream("未完成的片段")
            stream_reset("transport_fallback")
            stream("第二次尝试的片段")
            raise LlmError("test transport failure", "transport_error")
        with patch.object(MockPlanner, 'plan', fail):
            response = self.client.post(f"/api/v1/sessions/{sid}/branches/stream", json={
                "parent_branch_id": root, "text": "留在原地观察周围", "request_id": "stream-failed",
            })
        events = self.stream_events(response)
        self.assertEqual([event for event, _ in events], ["delta", "reset", "delta", "error"])
        self.assertEqual(events[-1][1]["code"], "generation_failed")
        self.assertNotIn('test transport', response.text)
        self.assertEqual(len(self.client.get(f"/api/v1/sessions/{sid}/branches").json()["branches"]), 1)
        retry = self.stream_events(self.client.post(f"/api/v1/sessions/{sid}/branches/stream", json={
            "parent_branch_id": root, "text": "留在原地观察周围", "request_id": "stream-failed",
        }))
        self.assertEqual(retry[-1][0], "done")
        self.assertEqual(retry[-1][1]["status"], "written")
        self.assertEqual(len(self.client.get(f"/api/v1/sessions/{sid}/branches").json()["branches"]), 2)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM direction_evaluations WHERE session_id=? AND request_id='stream-failed'", (sid,)).fetchone()[0], 1)

    def test_stream_unknown_save_returns_terminal_error(self):
        events = self.stream_events(self.client.post('/api/v1/sessions/missing/branches/stream', json={
            "parent_branch_id": "missing", "text": "观察", "request_id": "missing-stream",
        }))
        self.assertEqual(events[-1][0], 'error')
        self.assertEqual(events[-1][1]['code'], 'session_not_found')

    def test_read_mode_has_no_streaming_write_route(self):
        with TestClient(create_app(self.packages, self.database, play=False)) as client:
            response = client.post('/api/v1/sessions/missing/branches/stream', json={
                "parent_branch_id": "missing", "text": "观察",
            })
            self.assertEqual(response.status_code, 404)
        self.assertFalse(self.database.exists())

    def test_health_reports_play_phase(self):
        body = self.client.get("/api/v1/health").json()
        self.assertEqual(body["phase"], "play")
        self.assertTrue(body["generation_available"])
        self.assertTrue(body["state_updates_available"])

    def test_create_session_rejects_foreign_character(self):
        response = self.create_session(character="character_not_allowed")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "invalid_entry")

    def test_continue_direction_is_idempotent(self):
        session_id = self.create_session().json()["session"]["id"]
        branches = self.client.get(f"/api/v1/sessions/{session_id}/branches").json()["branches"]
        root = self.client.get(f"/api/v1/sessions/{session_id}/branches/{branches[0]['id']}").json()
        direction = root["nextDirections"][0]

        first = self.client.post(f"/api/v1/sessions/{session_id}/branches", json={
            "parent_branch_id": root["id"], "direction_id": direction["id"], "request_id": "req-1",
        })
        self.assertEqual(first.status_code, 200, first.text)
        first_body = first.json()
        self.assertEqual(first_body["status"], "written")
        self.assertFalse(first_body["deduplicated"])
        self.assertEqual(first_body["branch"]["parentId"], root["id"])

        second = self.client.post(f"/api/v1/sessions/{session_id}/branches", json={
            "parent_branch_id": root["id"], "direction_id": direction["id"], "request_id": "req-1",
        })
        second_body = second.json()
        self.assertEqual(second_body["status"], "written")
        self.assertTrue(second_body["deduplicated"])
        self.assertEqual(second_body["branch"]["id"], first_body["branch"]["id"])

        count = len(self.client.get(f"/api/v1/sessions/{session_id}/branches").json()["branches"])
        self.assertEqual(count, 2)

    def test_continue_direction_conflict_rejected(self):
        session_id = self.create_session().json()["session"]["id"]
        branches = self.client.get(f"/api/v1/sessions/{session_id}/branches").json()["branches"]
        root = self.client.get(f"/api/v1/sessions/{session_id}/branches/{branches[0]['id']}").json()
        direction = root["nextDirections"][0]
        self.client.post(f"/api/v1/sessions/{session_id}/branches", json={
            "parent_branch_id": root["id"], "direction_id": direction["id"], "request_id": "req-c",
        })
        other = root["nextDirections"][1] if len(root["nextDirections"]) > 1 else {"id": "never"}
        conflict = self.client.post(f"/api/v1/sessions/{session_id}/branches", json={
            "parent_branch_id": root["id"], "direction_id": other["id"], "request_id": "req-c",
        })
        self.assertEqual(conflict.status_code, 409)

    def test_free_text_writes_custom_direction(self):
        session_id = self.create_session().json()["session"]["id"]
        branches = self.client.get(f"/api/v1/sessions/{session_id}/branches").json()["branches"]
        root_id = branches[0]["id"]
        response = self.client.post(f"/api/v1/sessions/{session_id}/branches", json={
            "parent_branch_id": root_id, "text": "留在原地观察周围", "request_id": "req-free",
        })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "written")
        self.assertEqual(body["branch"]["parentId"], root_id)

    def test_invalid_direction_rejected_without_write(self):
        session_id = self.create_session().json()["session"]["id"]
        branches = self.client.get(f"/api/v1/sessions/{session_id}/branches").json()["branches"]
        response = self.client.post(f"/api/v1/sessions/{session_id}/branches", json={
            "parent_branch_id": branches[0]["id"], "direction_id": "direction_missing",
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "play_rejected")
        after = len(self.client.get(f"/api/v1/sessions/{session_id}/branches").json()["branches"])
        self.assertEqual(len(branches), after)

    def test_failed_commit_rolls_back_prose_state_and_receipt_then_retries_cached_draft(self):
        body = self.create_session().json()
        sid, bid = body['session']['id'], body['branch']['id']
        payload = {'parent_branch_id': bid, 'text': '留在原地观察周围', 'request_id': 'commit-retry'}
        with sqlite3.connect(self.database) as db:
            before = list(db.iterdump())
            db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON turn_requests BEGIN SELECT RAISE(ABORT, 'private database failure'); END")
        url = f'/api/v1/sessions/{sid}/branches/stream'
        failed = self.client.post(url, json=payload)
        events = self.stream_events(failed)
        self.assertTrue(any(event == 'delta' for event, _ in events))
        self.assertEqual(events[-1][0], 'error')
        self.assertNotIn('done', [event for event, _ in events])
        self.assertNotIn('private database failure', failed.text)
        with sqlite3.connect(self.database) as db:
            db.execute('DROP TRIGGER reject_receipt')
            self.assertEqual(before, list(db.iterdump()))
        # A storage failure must not force another paid generation of a ready draft.
        with patch.object(MockPlanner, 'plan', side_effect=AssertionError('不应重新生成')):
            retry = self.stream_events(self.client.post(url, json=payload))
        self.assertEqual(retry[-1][0], 'done')
        self.assertEqual(retry[-1][1]['status'], 'written')
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM branch_nodes WHERE session_id=?', (sid,)).fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM turn_requests WHERE session_id=?', (sid,)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM direction_evaluations WHERE session_id=?', (sid,)).fetchone()[0], 1)

    def test_concurrent_duplicate_streams_commit_one_branch(self):
        body = self.create_session().json()
        sid, bid = body['session']['id'], body['branch']['id']
        payload = {'parent_branch_id': bid, 'text': '留在原地观察周围', 'request_id': 'double-click'}
        entered, release = threading.Event(), threading.Event()
        original = MockPlanner.plan
        def slow(planner, *args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('测试生成等待超时')
            return original(planner, *args, **kwargs)
        original_wait = TurnDrafts.wait
        joined = threading.Event()
        def wait(drafts, job, *args, **kwargs):
            with drafts.condition:
                if job['selected_waiters'] == 2:
                    joined.set()
            return original_wait(drafts, job, *args, **kwargs)
        with patch.object(MockPlanner, 'plan', slow), patch.object(TurnDrafts, 'wait', wait), ThreadPoolExecutor(2) as executor:
            first = executor.submit(self.client.post, f'/api/v1/sessions/{sid}/branches/stream', json=payload)
            try:
                self.assertTrue(entered.wait(10))
                second = executor.submit(self.client.post, f'/api/v1/sessions/{sid}/branches/stream', json=payload)
                self.assertTrue(joined.wait(10), '两个 HTTP 请求应同时等待同一草稿')
            finally:
                release.set()
            results = [self.stream_events(f.result(timeout=10))[-1] for f in (first, second)]
        self.assertEqual([event for event, _ in results], ['done', 'done'])
        self.assertEqual(results[0][1]['branch']['id'], results[1][1]['branch']['id'])
        self.assertEqual(sorted(r['deduplicated'] for _, r in results), [False, True])
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM branch_nodes WHERE session_id=?', (sid,)).fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM turn_requests WHERE session_id=?', (sid,)).fetchone()[0], 1)

    def test_delete_during_generation_cannot_revive_save_or_draft(self):
        body = self.create_session().json()
        sid, bid = body['session']['id'], body['branch']['id']
        entered, release = threading.Event(), threading.Event()
        managers = []
        original_wait = TurnDrafts.wait
        def wait(drafts, *args, **kwargs):
            managers.append(drafts)
            return original_wait(drafts, *args, **kwargs)
        original = MockPlanner.plan
        def slow(planner, *args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('测试生成等待超时')
            return original(planner, *args, **kwargs)
        with patch.object(MockPlanner, 'plan', slow), patch.object(TurnDrafts, 'wait', wait), ThreadPoolExecutor(1) as executor:
            future = executor.submit(self.client.post, f'/api/v1/sessions/{sid}/branches/stream', json={
                'parent_branch_id': bid, 'text': '留在原地观察周围', 'request_id': 'deleted-running',
            })
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(self.client.delete(f'/api/v1/sessions/{sid}').status_code, 204)
            finally:
                release.set()
            events = self.stream_events(future.result(timeout=10))
            self.assertEqual(len(managers), 1)
            drafts = managers[0]
            with drafts.condition:
                self.assertTrue(drafts.condition.wait_for(lambda: sum(drafts.workers) == 0, timeout=10))
                self.assertFalse(any(job['binding']['session_id'] == sid for job in drafts.jobs.values()))
        self.assertEqual(events[-1][0], 'error')
        self.assertNotIn('done', [event for event, _ in events])
        self.assertEqual(self.client.get(f'/api/v1/sessions/{sid}').status_code, 404)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM game_sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM branch_nodes').fetchone()[0], 0)
        with sqlite3.connect(self.database.with_suffix('.turn-drafts.sqlite')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM turn_drafts').fetchone()[0], 0)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        book_tests = type('LongformPlay_' + case['package_id'], (LongformPlayTests,), {'case': case})
        suite.addTests(loader.loadTestsFromTestCase(book_tests))
    return suite
