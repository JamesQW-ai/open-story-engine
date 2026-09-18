"""Play-mode write API tests: temporary packages and databases, MockPlanner only."""

import os
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.content import load_runtime_story_package


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID = "rainy-waiting-room-source"
VERSION = "0.1.16"


class PlayApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / "packages"
        self.package_dir = self.packages / PACKAGE_ID / VERSION
        shutil.copytree(ROOT / "tests_py/fixtures/content/packages" / PACKAGE_ID / VERSION, self.package_dir)
        package = load_runtime_story_package(self.package_dir / "package.json", lazy=True)
        self.entry = next(iter(package["story"]["entryModel"]["entryPoints"]))
        self.character = self.entry["sourceCharacterIds"][0]
        self.database = self.root / "sessions.sqlite"
        self.env = patch.dict(os.environ, {"STORY_PLANNER": "mock", "STORY_API_PLAY": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(create_app(self.packages, self.database, play=True))
        self.addCleanup(self.client.close)

    def create_session(self, character=None):
        response = self.client.post("/api/v1/sessions", json={
            "package": {"package_id": PACKAGE_ID, "version": VERSION},
            "entry_point_id": self.entry["id"],
            "source_character_id": character or self.character,
        })
        return response

    def test_identity_openings_preserve_cast_locations_prose_and_continuation(self):
        before = {p.relative_to(self.packages): p.read_bytes() for p in self.packages.rglob('*') if p.is_file()}
        catalog = self.client.get(f'/api/v1/packages/{PACKAGE_ID}/{VERSION}').json()
        locations = {p['name']: p['id'] for p in catalog['locations']}
        expected = {'许川': '候车厅', '唐栖': '信号室', '陈砚': '站台', '姜序': '候车厅'}
        texts = set()
        for character in catalog['characters']:
            with self.subTest(character=character['name']):
                response = self.client.post('/api/v1/sessions', json={
                    'package': {'package_id': PACKAGE_ID, 'version': VERSION},
                    'entry_point_id': self.entry['id'], 'source_character_id': character['id'],
                    'identity_opening': True,
                })
                self.assertEqual(response.status_code, 201, response.text)
                result = response.json()
                sid, branch = result['session']['id'], result['branch']
                self.assertNotIn('你是' + character['name'], branch['narrativeText'])
                self.assertIn('你', branch['narrativeText'])
                self.assertNotIn('你是' + character['name'], branch['summary'])
                self.assertGreater(len(branch['narrativeText']), 200)
                self.assertNotIn('母本事实', branch['narrativeText'])
                self.assertEqual(branch['canonicalRelation'], 'diverged')
                location = locations[expected[character['name']]]
                self.assertEqual(branch['branchState']['playerLocationId'], location)
                self.assertEqual(branch['branchState']['characterLocationIds'][character['id']], location)
                self.assertEqual(result['session']['currentState']['playerLocationId'], location)
                self.assertGreaterEqual(len(branch['openingActions']), 2)
                texts.add(branch['narrativeText'])
                saved = self.client.get(f"/api/v1/sessions/{sid}/branches/{branch['id']}").json()
                self.assertEqual(saved['narrativeText'], branch['narrativeText'])
                action = branch['openingActions'][0]
                turn = self.client.post(f'/api/v1/sessions/{sid}/branches', json={
                    'parent_branch_id': branch['id'], 'text': action['title'] + '。' + action['summary'],
                    'request_id': 'identity-first-action',
                })
                self.assertEqual(turn.status_code, 200, turn.text)
                self.assertEqual(turn.json()['status'], 'written')
                self.assertTrue(turn.json()['branch']['narrativeText'])
        self.assertEqual(len(texts), 4)
        self.assertEqual(before, {p.relative_to(self.packages): p.read_bytes() for p in self.packages.rglob('*') if p.is_file()})
        # A normal API/CLI-style start must not inherit another call's overlay.
        original = self.create_session().json()['branch']
        self.assertNotIn('openingActions', original)

    def test_identity_opening_fallback_uses_only_its_own_book(self):
        from open_story_engine.api_openings import identity_opening_package
        from open_story_engine.cocreation import entry_node, create_contract
        package = load_runtime_story_package(self.package_dir / 'package.json', lazy=True)
        package = {**package, 'sourceAnalysis': {'sha256': 'different-book'},
                   'characters': [{**c, 'name': '林舟', 'menuDescription': '守在灯塔里的记录员。'} for c in package['characters']]}
        selection = {'kind': 'source_character', 'sourceCharacterId': self.character, 'entryPointId': self.entry['id']}
        overlay = identity_opening_package(package, selection)
        root = entry_node(overlay, create_contract(overlay, 'test', selection), True)
        self.assertNotIn('你是林舟', root['narrativeText'])
        self.assertIn('你停下脚步', root['narrativeText'])
        self.assertNotIn('openingActions', root)
        self.assertNotIn('sourceCharacterLocationIds', self.entry)

    def test_delete_save_removes_all_dependents_and_preserves_other_save(self):
        first = self.create_session().json()
        sid = first["session"]["id"]
        branch = first["branch"]
        other = self.create_session().json()["session"]["id"]
        result = self.client.post(f"/api/v1/sessions/{sid}/branches", json={
            "parent_branch_id": branch["id"], "text": "许川把手机递给唐栖", "request_id": "delete-child",
        })
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "written")
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
        payload = {"parent_branch_id": root, "text": "许川把手机递给唐栖", "request_id": "stream-free"}
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
                "parent_branch_id": root, "text": "许川把手机递给唐栖", "request_id": "stream-failed",
            })
        events = self.stream_events(response)
        self.assertEqual([event for event, _ in events], ["delta", "reset", "delta", "error"])
        self.assertEqual(events[-1][1]["code"], "generation_failed")
        self.assertNotIn('test transport', response.text)
        self.assertEqual(len(self.client.get(f"/api/v1/sessions/{sid}/branches").json()["branches"]), 1)
        retry = self.stream_events(self.client.post(f"/api/v1/sessions/{sid}/branches/stream", json={
            "parent_branch_id": root, "text": "许川把手机递给唐栖", "request_id": "stream-failed",
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

    def test_read_mode_rejects_writes(self):
        read_client = TestClient(create_app(self.packages, self.database / "other.sqlite", play=False))
        self.addCleanup(read_client.close)
        response = read_client.post("/api/v1/sessions", json={
            "package": {"package_id": PACKAGE_ID, "version": VERSION},
            "entry_point_id": self.entry["id"],
            "source_character_id": self.character,
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "endpoint_unavailable")

    def test_create_session_writes_root_branch(self):
        response = self.create_session()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        session_id = body["session"]["id"]
        self.assertEqual(body["branch"]["kind"], "source_entry")
        self.assertIsNone(body["branch"].get("parentId"))

        branches = self.client.get(f"/api/v1/sessions/{session_id}/branches").json()
        self.assertEqual(len(branches["branches"]), 1)
        state = self.client.get(f"/api/v1/sessions/{session_id}/state?branch_id={body['branch']['id']}")
        self.assertEqual(state.status_code, 200, state.text)

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
            "parent_branch_id": root_id, "text": "许川把手机递给唐栖", "request_id": "req-free",
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

    def test_create_session_with_new_character(self):
        new_entry = "entry_chapter-001-event-002"
        profile = {
            "name": "林晚", "gender": "女", "age": 27,
            "occupation": "临潮站夜班售票员",
            "sourceRelationship": "唐栖的前同事，认得上晚班的许川",
            "background": "在这个车站工作了三年，熟悉每一班末班车的时间表。",
        }
        response = self.client.post("/api/v1/sessions", json={
            "package": {"package_id": PACKAGE_ID, "version": VERSION},
            "entry_point_id": new_entry,
            "new_character": profile,
        })
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["branch"]["kind"], "source_entry")

        response = self.client.post("/api/v1/sessions", json={
            "package": {"package_id": PACKAGE_ID, "version": VERSION},
            "entry_point_id": new_entry,
            "new_character": {**profile, "background": "太短"},
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "invalid_entry")

    def test_relaxed_character_entry_choice(self):
        # 任意原著角色 × 任意入口：选择与入口声明无关的角色应成功
        catalog = self.client.get(f"/api/v1/packages/{PACKAGE_ID}/{VERSION}").json()
        all_characters = {c["id"] for c in catalog["characters"]}
        combo = None
        for entry in catalog["entries"]:
            missing = all_characters - set(entry["source_character_ids"])
            if missing:
                combo = (entry["id"], sorted(missing)[0])
                break
        self.assertIsNotNone(combo, "测试包缺少跨入口角色组合")
        response = self.client.post("/api/v1/sessions", json={
            "package": {"package_id": PACKAGE_ID, "version": VERSION},
            "entry_point_id": combo[0],
            "source_character_id": combo[1],
        })
        self.assertEqual(response.status_code, 201, response.text)

    def test_unknown_session_returns_404(self):
        response = self.client.post("/api/v1/sessions/session_missing/branches", json={
            "parent_branch_id": "branch_x", "direction_id": "direction_x",
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "session_not_found")


    def test_streamed_opening_retry_conflict_and_named_save(self):
        payload = {'package': {'package_id': PACKAGE_ID, 'version': VERSION},
                   'entry_point_id': self.entry['id'], 'source_character_id': self.character,
                   'identity_opening': True, 'request_id': 'opening-retry'}
        response = self.client.post('/api/v1/sessions/stream', json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn('event: delta', response.text)
        done = json.loads(response.text.split('event: done\ndata: ')[1].strip())
        retry = self.client.post('/api/v1/sessions', json=payload).json()
        self.assertEqual(retry['session']['id'], done['session']['id'])
        saves = self.client.get('/api/v1/sessions').json()['sessions']
        self.assertEqual(len(saves), 1)
        self.assertIn(saves[0]['role_name'], saves[0]['title'])
        self.assertTrue(saves[0]['recent_progress'])
        payload['identity_opening'] = False
        self.assertEqual(self.client.post('/api/v1/sessions', json=payload).status_code, 409)
        sid = done['session']['id']
        self.assertEqual(self.client.post(f'/api/v1/sessions/{sid}/rename', json={'title': '另一场雨'}).status_code, 200)
        self.assertEqual(self.client.get('/api/v1/sessions').json()['sessions'][0]['title'], '另一场雨')
        self.assertEqual(self.client.post(f'/api/v1/sessions/{sid}/rename', json={'title': '   '}).status_code, 422)

    def test_journal_is_lineage_scoped_and_rewind_preserves_ended_route(self):
        result = self.create_session().json()
        sid, root = result['session']['id'], result['branch']['id']
        def turn(parent, text, rid):
            response = self.client.post(f'/api/v1/sessions/{sid}/branches', json={'parent_branch_id': parent, 'text': text, 'request_id': rid})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()['branch']
        first = turn(root, '留在原地观察门边的动静', 'a')
        second = turn(root, '重新回想已经听见的话', 'b')
        self.client.post(f'/api/v1/sessions/{sid}/end', json={'branch_id': first['id']}).raise_for_status()
        journal = self.client.get(f"/api/v1/sessions/{sid}/journey?branch_id={first['id']}").json()
        self.assertEqual(journal['status'], 'abandoned')
        self.assertEqual([n['branch_id'] for n in journal['recap']], [first['id']])
        self.assertEqual(journal['progress'], 0)  # words / free-text turn count never awards progress
        blocked = self.client.post(f'/api/v1/sessions/{sid}/branches', json={'parent_branch_id': first['id'], 'text': '继续观察', 'request_id': 'c'})
        self.assertEqual(blocked.status_code, 409)
        replay = self.client.post(f'/api/v1/sessions/{sid}/branches', json={'parent_branch_id': root, 'text': '留在原地观察门边的动静', 'request_id': 'a'})
        self.assertEqual(replay.json()['branch']['id'], first['id'])
        alternative = turn(second['id'], '继续听门边的声音', 'd')
        journal = self.client.get(f"/api/v1/sessions/{sid}/journey?branch_id={alternative['id']}").json()
        self.assertEqual(journal['status'], 'active')
        self.assertNotIn(first['id'], [n['branch_id'] for n in journal['recap']])
        self.assertEqual(len(self.client.get(f'/api/v1/sessions/{sid}/branches').json()['branches']), 4)

    def test_choice_labels_and_character_cards_follow_selected_route(self):
        result = self.create_session().json()
        sid, root = result['session']['id'], result['branch']['id']
        choices = ['仔细询问陈砚刚才说过的话', '静静观察门边的动静']
        nodes = []
        for i, text in enumerate(choices):
            response = self.client.post(f'/api/v1/sessions/{sid}/branches', json={
                'parent_branch_id': root, 'text': text, 'request_id': f'card-{i}'})
            self.assertEqual(response.status_code, 200, response.text)
            nodes.append(response.json()['branch'])
        # Distinct, already-read discoveries on sibling routes; no catalog biography.
        with sqlite3.connect(self.database) as connection:
            for bid, prose in [(root, '你站在雨中的门边。'),
                               (nodes[0]['id'], '陈砚说：“我会留在这里等消息。”'),
                               (nodes[1]['id'], '姜序低声说：“雨还没有停。”')]:
                node = json.loads(connection.execute('SELECT node_json FROM branch_nodes WHERE id=?', (bid,)).fetchone()[0])
                node['narrativeText'] = prose
                connection.execute('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(node, ensure_ascii=False), bid))
        page = self.client.get(f'/api/v1/sessions/{sid}/branches?include_actions=true').json()['branches']
        self.assertEqual(page[0]['action'], '故事开篇')
        self.assertEqual([n['action'] for n in page[1:]], choices)
        first = self.client.get(f"/api/v1/sessions/{sid}/journey?branch_id={nodes[0]['id']}").json()
        chen = next(p for p in first['people'] if p['name'] == '陈砚')
        self.assertNotIn('memories', chen)
        self.assertIn('identity', chen)
        self.assertEqual(chen['last_page'], 2)
        self.assertEqual(first['lineage'], [root, nodes[0]['id']])
        self.assertNotIn('姜序', [p['name'] for p in first['people']])
        alternative = self.client.get(f"/api/v1/sessions/{sid}/journey?branch_id={nodes[1]['id']}").json()
        self.assertNotIn('陈砚', [p['name'] for p in alternative['people']])
        self.assertIn('姜序', [p['name'] for p in alternative['people']])
        earlier = self.client.get(f'/api/v1/sessions/{sid}/journey?branch_id={root}').json()
        self.assertNotIn('陈砚', [p['name'] for p in earlier['people']])

    def test_journey_character_states_follow_branch_and_read_without_writes(self):
        import hashlib
        from open_story_engine.api_read import ReadService
        result = self.create_session().json()
        sid, root = result['session']['id'], result['branch']['id']
        package = load_runtime_story_package(self.package_dir / 'package.json', lazy=True)
        cid = next(c['id'] for c in package['characters'] if c['name'] == '陈砚')
        children = [self.client.post(f'/api/v1/sessions/{sid}/branches', json={
            'parent_branch_id': root, 'text': '留在原地观察', 'request_id': 'person-state-' + str(i)
        }).json()['branch'] for i in range(2)]
        with sqlite3.connect(self.database) as connection:
            for bid, code in [(root, None), (children[0]['id'], 'dead'), (children[1]['id'], 'departed')]:
                node = json.loads(connection.execute('SELECT node_json FROM branch_nodes WHERE id=?', (bid,)).fetchone()[0])
                node['narrativeText'] = '你站在门边。陈砚看向你。' if code is None else (
                    '陈砚已经死亡。' if code == 'dead' else '陈砚已离队，此后不再同行。')
                if code:
                    update = {'characterId': cid, 'status': code, 'permanence': 'permanent', 'evidence': node['narrativeText']}
                    node['consequenceUpdate'] = {'outcomes': [update]}
                    node['branchState']['characterOutcomeStates'] = {cid: {**update, 'causeBranchId': bid}}
                connection.execute('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(node, ensure_ascii=False), bid))
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        for bid, code in [(children[0]['id'], 'dead'), (children[1]['id'], 'departed'), (root, 'unknown')]:
            response = self.client.get(f'/api/v1/sessions/{sid}/journey', params={'branch_id': bid})
            self.assertEqual(response.status_code, 200, response.text)
            journal = response.json()
            self.assertEqual(journal['branch_id'], bid)
            person = next(p for p in journal['people'] if p['id'] == cid)
            self.assertEqual(person['status']['code'], code)
            self.assertIn('portrait', person)
            # A fresh service reads the same persisted projection.
            self.assertEqual(journal, ReadService(self.packages, self.database).journey(sid, bid))
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_character_summary_cache_is_scoped_to_branch_and_does_not_write(self):
        from open_story_engine.api_play import PlayService
        from open_story_engine.api_read import ReadService, ReadError
        from open_story_engine.llm import Completion
        from types import SimpleNamespace
        from unittest.mock import Mock
        import hashlib
        result = self.create_session().json()
        sid, root = result['session']['id'], result['branch']['id']
        child = self.client.post(f'/api/v1/sessions/{sid}/branches', json={
            'parent_branch_id': root, 'text': '留在原地观察', 'request_id': 'profile-child'}).json()['branch']
        with sqlite3.connect(self.database) as connection:
            for bid, prose in [(root, '你站在门边。陈砚穿着站务制服。'),
                               (child['id'], '陈砚说：“我会留在这里等消息。”')]:
                node = json.loads(connection.execute('SELECT node_json FROM branch_nodes WHERE id=?', (bid,)).fetchone()[0])
                node['narrativeText'] = prose
                connection.execute('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(node, ensure_ascii=False), bid))
        service = PlayService(ReadService(self.packages, self.database), self.root)
        chen = next(p for p in service.read.journey(sid, root)['people'] if p['name'] == '陈砚')
        gateway = SimpleNamespace(complete_text=Mock(side_effect=[
            Completion(json.dumps({'identity': '车站工作人员', 'summary': '身穿站务制服，具体立场尚未明确。', 'evidence': ['P1']}), '', []),
            Completion(json.dumps({'identity': '车站工作人员', 'summary': '身穿站务制服，表示会留下等待消息。', 'evidence': ['P2']}), '', []),
        ]))
        service._planner = SimpleNamespace(gateway=gateway)
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        profile = service.character_profile(sid, root, chen['id'])
        self.assertEqual(profile['identity'], chen['identity'])  # the model cannot overwrite a public identity
        self.assertEqual(profile, service.character_profile(sid, root, chen['id']))
        updated = service.character_profile(sid, child['id'], chen['id'])
        self.assertIn('等待消息', updated['summary'])
        self.assertEqual(gateway.complete_text.call_count, 2)
        self.assertNotIn('等消息', gateway.complete_text.call_args_list[0].args[0][1]['content'])
        with self.assertRaises(ReadError):
            service.character_profile(sid, root, 'unknown-character')
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        self.assertNotIn('memories', profile)
        self.assertLessEqual(len(updated['summary']), 160)

    def test_opening_generation_failure_leaves_no_empty_save(self):
        from open_story_engine.api_narrative import PlayerNarrativePlanner
        from open_story_engine.llm import LlmError
        payload = {'package': {'package_id': PACKAGE_ID, 'version': VERSION}, 'entry_point_id': self.entry['id'],
                   'source_character_id': self.character, 'identity_opening': True}
        with patch('open_story_engine.api_play.PlayService._build_runtime') as build:
            def runtime(service):
                service._planner = PlayerNarrativePlanner(None)
                service._evaluator = None
            build.side_effect = None
            from open_story_engine.api_play import PlayService
            from open_story_engine.api_read import ReadService, ReadError
            service = PlayService(ReadService(self.packages, self.database), self.root)
            runtime(service)
            with patch.object(PlayerNarrativePlanner, 'opening', side_effect=LlmError('test', 'model_output_rejected')):
                with self.assertRaises(ReadError):
                    service.create_session(PACKAGE_ID, VERSION, self.entry['id'], self.character, identity_opening=True)
        self.assertEqual(self.client.get('/api/v1/sessions').json()['sessions'], [])

    def test_identity_cursor_remains_playable_and_progress_uses_confirmed_milestones(self):
        from open_story_engine.api_journey import player_directions, journey
        from open_story_engine.storage import SessionStore
        from open_story_engine.cocreation import apply_branch_patch
        package = load_runtime_story_package(self.package_dir / 'package.json', lazy=True)
        tang = next(c for c in package['characters'] if c['name'] == '唐栖')
        result = self.client.post('/api/v1/sessions', json={
            'package': {'package_id': PACKAGE_ID, 'version': VERSION},
            'entry_point_id': self.entry['id'], 'source_character_id': tang['id'], 'identity_opening': True,
        }).json()
        sid, root = result['session']['id'], result['branch']
        self.assertTrue(root['nextDirections'])
        store = SessionStore(str(self.database))
        try:
            parent = root
            for count in range(2):
                directions = player_directions(package, parent['branchState'])
                self.assertTrue(directions)
                selected = directions[0]
                state = apply_branch_patch(package, parent['branchState'], selected['statePatch'], parent['sourceNodeRef'])
                # Test fixture explicitly records the script-validated transition.
                node = {**parent, 'id': 'checkpoint-' + str(count), 'selectedDirectionId': selected['id'],
                        'selectedDirection': selected, 'branchState': state, 'nextDirections': player_directions(package, state),
                        'playerDirection': None, 'narrativeText': '你迈出了眼前的这一步。'}
                parent = store.append_branch(sid, parent['id'], node)
            view = journey(store, sid, parent['id'], package)
            self.assertEqual(view['progress'], 50)
            self.assertEqual(journey(store, sid, root['id'], package)['progress'], 0)
            self.assertEqual(view['status'], 'active')
            for count in range(2, 4):
                selected = player_directions(package, parent['branchState'])[0]
                state = apply_branch_patch(package, parent['branchState'], selected['statePatch'], parent['sourceNodeRef'])
                node = {**parent, 'id': 'checkpoint-' + str(count), 'selectedDirectionId': selected['id'],
                        'selectedDirection': selected, 'branchState': state, 'nextDirections': player_directions(package, state)}
                parent = store.append_branch(sid, parent['id'], node)
            ending = journey(store, sid, parent['id'], package)
            self.assertEqual(ending['progress'], 100)
            self.assertEqual(ending['status'], 'completed')
            self.assertEqual(journey(store, sid, root['id'], package)['progress'], 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
