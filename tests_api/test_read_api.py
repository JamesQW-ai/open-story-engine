"""HTTP integration tests with temporary packages and databases only."""

import hashlib
import json
import shutil
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.api_read import ReadOnlySessionStore
from open_story_engine.branch_ledger import append_branch_ledger
from open_story_engine.cocreation import CoCreationService, MockPlanner, create_contract, entry_node
from open_story_engine.content import load_runtime_story_package
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.storage import SessionStore


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID = "taixu-relics-part1"
VERSION = "0.1.2"


class ReadApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / "packages"
        self.package_dir = self.packages / PACKAGE_ID / VERSION
        shutil.copytree(ROOT / "content/packages" / PACKAGE_ID / VERSION, self.package_dir)
        self.package = load_runtime_story_package(self.package_dir / "package.json", lazy=True)
        self.entry = next(iter(self.package["story"]["entryModel"]["entryPoints"]))
        self.character = self.entry["sourceCharacterIds"][0]
        self.database = self.root / "sessions.sqlite"
        self.client = TestClient(create_app(self.packages, self.database))
        self.addCleanup(self.client.close)

    def seed(self):
        store = SessionStore(str(self.database))
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        store.close()
        return session, root

    def preview_request(self):
        return {"package": {"package_id": PACKAGE_ID, "version": VERSION},
                "entry_point_id": self.entry["id"], "source_character_id": self.character}

    def test_catalog_is_real_and_omits_prose_excerpts(self):
        with patch.object(SessionStore, "_initialize", side_effect=AssertionError("migration")):
            response = self.client.get("/api/v1/packages")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["packages"][0]["beat_count"], len(self.package["story"]["narrativeGraph"]["beats"]))
            catalog = self.client.get(f"/api/v1/packages/{PACKAGE_ID}/{VERSION}").json()
        self.assertEqual(len(catalog["entries"]), 3)
        self.assertNotIn("sourceExcerpt", json.dumps(catalog))
        self.assertFalse(self.database.exists())

    def test_catalog_exposes_every_public_identity_card_and_portrait(self):
        from open_story_engine.api_read import ReadService

        catalog = ReadService(self.packages, self.database).catalog(self.package)
        people = catalog["characters"] + catalog["supporting_characters"]
        self.assertEqual(
            [person["name"] for person in people],
            ["陆照临", "顾长离", "叶观澜", "陆沉舟", "沈砚秋", "萧问蝉", "叶青冥"],
        )
        self.assertTrue(all(person.get("portraitAsset", "").startswith("/images/") for person in people))
        self.assertNotIn("sourceDescriptions", json.dumps(catalog["supporting_characters"]))

    def test_unreviewed_public_identity_gets_a_session_opening_without_mutating_package(self):
        from open_story_engine.api_openings import identity_opening_package
        from open_story_engine.cocreation import create_contract, entry_node, normalize_entry_selection

        entry = next(iter(self.package["story"]["entryModel"]["entryPoints"]))
        selection = normalize_entry_selection(
            self.package,
            {"kind": "source_character", "sourceCharacterId": "character_37531636ecf5", "entryPointId": entry["id"]},
            allow_any_source_character=True,
        )
        overlay = identity_opening_package(self.package, selection)
        root = entry_node(
            overlay,
            create_contract(overlay, "identity-card-test", selection, allow_any_source_character=True),
            allow_any_source_character=True,
        )
        self.assertEqual(root["branchState"]["playerCharacterId"], "character_37531636ecf5")
        self.assertIn("你停下脚步", root["narrativeText"])
        self.assertEqual(next(iter(self.package["story"]["entryModel"]["entryPoints"]))["sourceCharacterIds"], ["character_ae4cb42b9b49"])

    def test_missing_database_and_unavailable_writes_are_explicit(self):
        self.assertEqual(self.client.get("/api/v1/sessions").json(), {"available": False, "sessions": []})
        for path in ("generate/beat", "state/update"):
            response = self.client.post("/api/v1/" + path, json={})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "endpoint_unavailable")
        self.assertFalse(self.client.get("/api/v1/health").json()["generation_available"])
        self.assertFalse(self.database.exists())

    def test_supporting_roster_is_opt_in_public_and_not_a_playable_identity(self):
        from open_story_engine.api_models import PackageCatalog
        from open_story_engine.api_read import ReadService

        for person in self.package["characters"]:
            person["rosterVisible"] = False
        npc = self.package["characters"][-1]
        npc.update({"rosterVisible": True, "roleGroup": "key_supporting",
                    "identitySummary": "只展示公开身份", "secret": "不可泄漏的后文"})
        self.package["story"]["entryModel"]["sourceCharacterIds"] = [self.character]
        service = ReadService(self.packages, self.database)
        result = PackageCatalog.model_validate(service.catalog(self.package)).model_dump()
        self.assertEqual([c["id"] for c in result["characters"]], [self.character])
        self.assertEqual(result["supporting_characters"][0]["id"], npc["id"])
        self.assertEqual(result["supporting_characters"][0]["identitySummary"], "只展示公开身份")
        self.assertNotIn("不可泄漏的后文", json.dumps(result["supporting_characters"]))
        npc["rosterVisible"] = False
        self.assertEqual(service.catalog(self.package)["supporting_characters"], [])

    def test_preview_capability_distinguishes_legacy_and_unregistered_packages(self):
        # An unmodularized copy of this same long novel stays browsable.
        target = self.packages / PACKAGE_ID / "0.1.0"
        target.mkdir(parents=True)
        package = json.loads((self.package_dir / "package.json").read_text())
        package["version"] = "0.1.0"
        package.pop("moduleIndexSha256", None)
        (target / "package.json").write_text(json.dumps(package, ensure_ascii=False))
        read_text = Path.read_text

        def indexes_only(path, *args, **kwargs):
            self.assertFalse({"reader", "chapters", "beats", "characters", "locations", "items"} & set(path.parts))
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", indexes_only):
            response = self.client.get("/api/v1/packages")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        capabilities = {(p["package_id"], p["version"]): p["context_preview"] for p in data["packages"]}
        self.assertEqual(data["issues"], [])  # Browsable historical packages are not corrupt.
        self.assertTrue(capabilities[(PACKAGE_ID, VERSION)]["available"])
        self.assertEqual(capabilities[(PACKAGE_ID, "0.1.0")]["code"], "modular_context_required")
        package = json.loads((self.package_dir / "package.json").read_text())
        parsed = self.client.post("/api/v1/package/parse", json={"package": package}).json()
        self.assertEqual(parsed["package"]["context_preview"]["code"], "package_not_registered")

    def test_json_parse_validates_without_registering_or_writing(self):
        package = json.loads((self.package_dir / "package.json").read_text())
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        response = self.client.post("/api/v1/package/parse", json={"package": package})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["package"]["version"], VERSION)
        self.assertEqual(before, {path.relative_to(self.root) for path in self.root.rglob("*")})
        for invalid in ({}, {"characters": 3}, {**package, "characters": [None]}):
            response = self.client.post("/api/v1/package/parse", json={"package": invalid})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "invalid_package")

    def test_context_matches_core_and_reads_no_reader_modules(self):
        selection = {"kind": "source_character", "sourceCharacterId": self.character, "entryPointId": self.entry["id"]}
        contract = create_contract(self.package, "preview", selection)
        parent = entry_node(self.package, contract)
        resolver = ModuleContextResolver.for_package(self.package_dir / "package.json", self.package)
        expected = resolver.resolve({"contract": contract, "parent": parent, "lineage": [parent]},
                                    {"statePatch": {}}, parent["branchState"])
        read_text = Path.read_text

        def guarded(path, *args, **kwargs):
            self.assertNotIn("reader", path.parts)
            self.assertNotEqual(path.name, "reader.json")
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", guarded):
            response = self.client.post("/api/v1/context/build", json=self.preview_request())
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["context"], expected)
        self.assertEqual(value["state"], parent["branchState"])
        again = self.client.post("/api/v1/context/build", json=self.preview_request()).json()
        self.assertEqual(value["context_sha256"], again["context_sha256"])
        self.assertFalse(self.database.exists())

    def test_rejects_mixed_context_and_unavailable_character(self):
        for body in ({}, {**self.preview_request(), "session_id": "x"},
                     {**self.preview_request(), "active_character_ids": [self.character]},
                     {**self.preview_request(), "source_character_id": "missing"}):
            response = self.client.post("/api/v1/context/build", json=body)
            self.assertEqual(response.status_code, 422, response.text)

    def test_state_uses_requested_branch_and_never_migrates(self):
        session, branch = self.seed()
        digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with patch.object(SessionStore, "_initialize", side_effect=AssertionError("migration")):
            view = self.client.get(f"/api/v1/sessions/{session['id']}").json()
            self.assertEqual(view["branches"][0]["id"], branch["id"])
            ambiguous = self.client.get(f"/api/v1/sessions/{session['id']}/state")
            self.assertEqual(ambiguous.json()["error"]["code"], "branch_required")
            result = self.client.get(f"/api/v1/sessions/{session['id']}/state", params={"branch_id": branch["id"]})
            self.assertEqual(result.json()["state"], branch["branchState"])
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), digest)
        readonly = ReadOnlySessionStore(self.database)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                readonly.connection.execute("DELETE FROM branch_nodes")
        finally:
            readonly.close()

    def test_sessions_do_not_share_branches(self):
        first, branch = self.seed()
        second, _ = self.seed()
        response = self.client.get(f"/api/v1/sessions/{second['id']}/state", params={"branch_id": branch["id"]})
        self.assertEqual(response.status_code, 404)
        body = {"session_id": first["id"], "parent_branch_id": branch["id"]}
        result = self.client.post("/api/v1/context/build", json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["mode"], "branch_preview")
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE game_sessions SET story_package_module_index_sha256='changed' WHERE id=?", (first["id"],))
        self.assertEqual(self.client.post("/api/v1/context/build", json=body).status_code, 409)

    def test_binding_status_preserves_history_and_never_rebinds_sessions(self):
        session, branch = self.seed()
        url = f"/api/v1/sessions/{session['id']}"
        preview = {"session_id": session["id"], "parent_branch_id": branch["id"]}
        view = self.client.get(url).json()
        self.assertEqual(view["context_preview"]["binding_status"], "matched")
        self.assertTrue(view["context_preview"]["available"])
        self.assertEqual(self.client.post("/api/v1/context/build", json=preview).status_code, 200)
        for digest, expected_status in (("changed", "changed"), (None, "legacy_unverified")):
            with sqlite3.connect(self.database) as connection:
                connection.execute("UPDATE game_sessions SET story_package_module_index_sha256=? WHERE id=?",
                                   (digest, session["id"]))
            before = hashlib.sha256(self.database.read_bytes()).hexdigest()
            view = self.client.get(url).json()
            self.assertEqual(view["context_preview"]["binding_status"], expected_status)
            self.assertEqual(view["branches"][0]["narrativeText"], branch["narrativeText"])
            self.assertEqual(self.client.get(url + "/state", params={"branch_id": branch["id"]}).status_code, 200)
            response = self.client.post("/api/v1/context/build", json=preview)
            self.assertEqual(response.status_code, 409 if digest else 200)
            self.assertEqual(view["context_preview"]["available"], digest is None)
            self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        shutil.rmtree(self.package_dir)  # Temporary fixture only.
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["context_preview"]["code"], "package_not_found")
        self.assertEqual(response.json()["context_preview"]["binding_status"], "unavailable")

    def test_session_without_contract_or_branches_is_not_preview_ready(self):
        session, _ = self.seed()
        url = f"/api/v1/sessions/{session['id']}"
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM session_story_contracts WHERE session_id=?", (session["id"],))
        self.assertEqual(self.client.get(url).json()["context_preview"]["code"], "invalid_session")
        store = SessionStore(str(self.database))
        ordinary = store.create_session(self.package)
        store.close()
        view = self.client.get(f"/api/v1/sessions/{ordinary['id']}").json()
        self.assertEqual(view["context_preview"]["code"], "no_confirmed_branches")

    def test_state_without_branch_id_never_reads_branch_bodies_at_scale(self):
        session, branch = self.seed()
        store = SessionStore(str(self.database))
        ordinary_session = store.create_session(self.package)
        store.close()
        body = json.dumps({**branch, "narrative": "雨声持续落在玻璃上。" * 300}, ensure_ascii=False)
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                "INSERT INTO branch_nodes(id,session_id,sequence,parent_id,node_json,created_at) VALUES(?,?,?,?,?,?)",
                [(f"large-branch-{i}", session["id"], i + 2, branch["id"], body, branch["createdAt"])
                 for i in range(1000)],
            )
        digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
        initialize = ReadOnlySessionStore.__init__
        def forbid_body_reads(store, path):
            initialize(store, path)
            store.connection.set_authorizer(
                lambda action, table, column, *_: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "branch_nodes" and column == "node_json"
                else sqlite3.SQLITE_OK
            )
        with patch.object(ReadOnlySessionStore, "__init__", forbid_body_reads):
            view = self.client.get(f"/api/v1/sessions/{session['id']}", params={"include_branches": "false"})
            self.assertEqual(view.status_code, 200, view.text)
            self.assertFalse(view.json()["branches_included"])
            self.assertEqual(view.json()["branches"], [])
            page = self.client.get(f"/api/v1/sessions/{session['id']}/branches", params={"limit": 20})
            self.assertEqual(page.status_code, 200, page.text)
            self.assertEqual(len(page.json()["branches"]), 20)
            self.assertNotIn("narrativeText", page.text)
            response = self.client.get(f"/api/v1/sessions/{session['id']}/state")
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "branch_required")
            response = self.client.get(f"/api/v1/sessions/{ordinary_session['id']}/state")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["state"], ordinary_session["currentState"])
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), digest)

    def test_branch_pagination_handles_gaps_and_isolates_sessions(self):
        session, root = self.seed()
        other, other_root = self.seed()
        url = f"/api/v1/sessions/{session['id']}/branches"
        with sqlite3.connect(self.database) as connection:
            for sequence in (2, 7, 11):
                node = {**root, "id": f"node-{sequence}", "parentId": root["id"], "sequence": sequence}
                connection.execute("INSERT INTO branch_nodes(id,session_id,sequence,parent_id,node_json,created_at) VALUES(?,?,?,?,?,?)",
                                   (node["id"], session["id"], sequence, root["id"], json.dumps(node), root["createdAt"]))
        first = self.client.get(url, params={"limit": 2}).json()
        self.assertEqual([b["sequence"] for b in first["branches"]], [0, 2])
        second = self.client.get(url, params={"limit": 2, "after_sequence": first["next_after_sequence"]}).json()
        self.assertEqual([b["sequence"] for b in second["branches"]], [7, 11])
        self.assertTrue(all(b["parent_id"] == root["id"] for b in second["branches"]))
        self.assertIsNone(second["next_after_sequence"])
        empty = self.client.get(url, params={"after_sequence": 11}).json()
        self.assertEqual(empty["branches"], [])
        self.assertIsNone(empty["next_after_sequence"])
        self.assertEqual(self.client.get(url + "/node-7").json()["parentId"], root["id"])
        self.assertEqual(self.client.get(url + "/" + other_root["id"]).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/sessions/{other['id']}/branches/node-7").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/sessions/missing/branches").status_code, 404)
        for params in ({"limit": 0}, {"limit": 201}, {"after_sequence": -2},
                       {"after_sequence": 2 ** 63}, {"limit": "bad"}):
            response = self.client.get(url, params=params)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "invalid_request")

    def test_typed_responses_preserve_core_fields_and_package_specific_state(self):
        session, branch = self.seed()
        branch["branchState"]["customFact"] = {"known": False, "count": 0, "value": None}
        append_branch_ledger(branch["branchState"], [{
            "kind": "clue", "operation": "added", "entityId": "test-clue", "summary": "测试线索",
            "before": None, "after": {"observed": True},
        }], {"kind": "test", "ref": "test-source"})
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE branch_nodes SET node_json=? WHERE id=?", (json.dumps(branch), branch["id"]))
        url = f"/api/v1/sessions/{session['id']}"
        detail = self.client.get(url + "/branches/" + branch["id"])
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json(), branch)
        state = self.client.get(url + "/state", params={"branch_id": branch["id"]}).json()
        self.assertEqual(state["state"], branch["branchState"])
        self.assertEqual(state["ledger"], branch["branchState"]["branchLedger"])
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE branch_nodes SET node_json=? WHERE id=?",
                               (json.dumps({**branch, "narrativeText": 42}), branch["id"]))
        invalid = self.client.get(url + "/branches/" + branch["id"])
        self.assertEqual(invalid.status_code, 503)
        self.assertEqual(invalid.json()["error"]["code"], "invalid_response")
        self.assertNotIn(str(self.root), invalid.text)

    def test_legacy_branch_without_state_remains_readable_without_inventing_state(self):
        session, branch = self.seed()
        del branch["branchState"]
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE branch_nodes SET node_json=? WHERE id=?", (json.dumps(branch), branch["id"]))
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        url = f"/api/v1/sessions/{session['id']}"
        detail = self.client.get(url + "/branches/" + branch["id"])
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json(), branch)
        self.assertEqual(self.client.get(url).status_code, 200)
        state = self.client.get(url + "/state", params={"branch_id": branch["id"]})
        context = self.client.post("/api/v1/context/build", json={"session_id": session["id"], "parent_branch_id": branch["id"]})
        for response in (state, context):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "branch_state_unavailable")
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_parallel_reads_use_independent_connections(self):
        session, branch = self.seed()
        def read(_):
            return self.client.get(f"/api/v1/sessions/{session['id']}/state", params={"branch_id": branch["id"]})
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(read, range(12)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertTrue(all(response.json()["state"] == branch["branchState"] for response in responses))

    def test_corrupt_package_and_missing_refs_return_safe_errors(self):
        index = self.package_dir / "modules/world.json"
        index.write_text('{}')
        response = self.client.get(f"/api/v1/packages/{PACKAGE_ID}/{VERSION}")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(len(self.client.get("/api/v1/packages").json()["issues"]), 1)
        self.assertEqual(self.client.get("/api/v1/packages/missing/1.0.0").status_code, 404)

    def test_openapi_and_unavailable_api(self):
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/api/v1/context/build", schema["paths"])
        self.assertIn("ContextRequest", schema["components"]["schemas"])
        models = schema["components"]["schemas"]
        for model, fields in {
            "BranchView": ("parentId", "narrativeText", "branchState"),
            "EntityCard": ("id", "name", "description"),
            "ContextData": ("currentChapter", "currentBeat", "characterDetails"),
            "LedgerEntry": ("before", "after", "source"),
        }.items():
            self.assertTrue(set(fields).issubset(models[model]["properties"]))
        self.assertEqual(self.client.get("/").status_code, 404)
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/unknown").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)

    def test_browser_origins_preflight_and_error_responses(self):
        with TestClient(create_app(self.packages, self.database,
                                   cors_origins=["http://localhost:5173", "http://127.0.0.1:5173"])) as client:
            for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
                headers = {"Origin": origin, "Access-Control-Request-Method": "POST",
                           "Access-Control-Request-Headers": "content-type"}
                preflight = client.options("/api/v1/context/build", headers=headers)
                self.assertEqual(preflight.status_code, 200)
                self.assertEqual(preflight.headers["access-control-allow-origin"], origin)
                self.assertNotIn("access-control-allow-credentials", preflight.headers)
                for body, status in ((self.preview_request(), 200), ({}, 422)):
                    result = client.post("/api/v1/context/build", json=body, headers={"Origin": origin})
                    self.assertEqual(result.status_code, status)
                    self.assertEqual(result.headers["access-control-allow-origin"], origin)
                missing = client.get("/api/v1/packages/missing/1.0.0", headers={"Origin": origin})
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(missing.headers["access-control-allow-origin"], origin)
            for origin, method in (("https://untrusted.example", "POST"), ("http://localhost:5173", "DELETE")):
                result = client.options("/api/v1/context/build", headers={"Origin": origin, "Access-Control-Request-Method": method})
                self.assertEqual(result.status_code, 400)
            result = client.get("/api/v1/health", headers={"Origin": "https://untrusted.example"})
            self.assertNotIn("access-control-allow-origin", result.headers)

    def test_browser_origin_configuration_can_replace_or_disable_defaults(self):
        for setting, origin, expected in (
            ("http://localhost:5174", "http://localhost:5174", "http://localhost:5174"),
            ("http://localhost:5174", "http://localhost:5173", None),
            ("", "http://localhost:5173", None),
        ):
            with patch.dict("os.environ", {"STORY_API_CORS_ORIGINS": setting}):
                with TestClient(create_app(self.packages, self.database)) as client:
                    result = client.get("/api/v1/health", headers={"Origin": origin})
                    self.assertEqual(result.headers.get("access-control-allow-origin"), expected)


if __name__ == "__main__":
    unittest.main()
