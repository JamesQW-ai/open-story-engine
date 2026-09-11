"""Chapter reader endpoint tests: temporary packages and databases only."""

import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.cocreation import CoCreationService, MockPlanner
from open_story_engine.content import load_runtime_story_package
from open_story_engine.storage import SessionStore


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID = "rainy-waiting-room-source"
VERSION = "0.1.16"


class ReaderApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / "packages"
        self.package_dir = self.packages / PACKAGE_ID / VERSION
        shutil.copytree(ROOT / "tests_py/fixtures/content/packages" / PACKAGE_ID / VERSION, self.package_dir)
        package = load_runtime_story_package(self.package_dir / "package.json", lazy=True)
        self.database = self.root / "sessions.sqlite"
        self.client = TestClient(create_app(self.packages, self.database))
        self.addCleanup(self.client.close)

        store = SessionStore(str(self.database))
        session = store.create_session(package)
        service = CoCreationService(package, store, MockPlanner())
        _, root = service.start(session["id"])
        store.close()
        self.session_id = session["id"]
        self.root_id = root["id"]

    def test_branch_source_chapter_returns_full_text(self):
        response = self.client.get(
            f"/api/v1/sessions/{self.session_id}/branches/{self.root_id}/source-chapter"
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["chapter_id"], "chapter-001")
        self.assertEqual(body["title"], "二十三点十分")
        self.assertGreater(len(body["text"]), 1000)

    def test_generated_branch_may_not_have_source_chapter(self):
        response = self.client.get(
            f"/api/v1/sessions/{self.session_id}/branches/branch_missing/source-chapter"
        )
        self.assertEqual(response.status_code, 404)

    def test_reader_requires_matching_package_binding(self):
        # 篡改 reader.json 的包绑定后应返回 409，而不是透出内容
        import json

        reader_path = self.package_dir / "reader.json"
        reader = json.loads(reader_path.read_text(encoding="utf-8"))
        reader["package"] = {"id": "tampered", "version": VERSION}
        reader_path.write_text(json.dumps(reader, ensure_ascii=False), encoding="utf-8")
        store = SessionStore(str(self.root / "other.sqlite"))
        package = load_runtime_story_package(self.package_dir / "package.json", lazy=True)
        client = TestClient(create_app(self.packages, self.root / "other.sqlite"))
        self.addCleanup(client.close)
        session = store.create_session(package)
        service = CoCreationService(package, store, MockPlanner())
        _, root = service.start(session["id"])
        store.close()
        response = client.get(
            f"/api/v1/sessions/{session['id']}/branches/{root['id']}/source-chapter"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "invalid_reader")


if __name__ == "__main__":
    unittest.main()
