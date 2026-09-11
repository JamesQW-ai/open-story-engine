"""Import real TXT samples into isolated package roots and databases."""

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.content import load_runtime_story_package


ROOT = Path(__file__).resolve().parents[1]


class ImportApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / "packages"
        self.database = self.root / "sessions.sqlite"
        env = patch.dict(os.environ, {"STORY_PLANNER": "mock"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(create_app(self.packages, self.database, play=True))
        self.addCleanup(self.client.close)
        self.source = (ROOT / "content/source/rainy-waiting-room.v0.1.txt").read_text(encoding="utf-8")
        self.payload = {"file_name": "雨夜候车室.txt", "source_text": self.source}

    def test_sample_import_without_identifiers_supports_role_and_plot_selection(self):
        response = self.client.post("/api/v1/import/novel", json=self.payload)
        self.assertEqual(response.status_code, 201, response.text)
        imported = response.json()
        self.assertEqual((imported["title"], imported["chapter_count"], imported["character_count"], imported["beat_count"]),
                         ("雨夜候车室", 4, 4, 14))
        self.assertFalse(self.database.exists())
        directory = self.packages / imported["package_id"] / imported["version"]
        package = load_runtime_story_package(directory / "package.json")
        self.assertEqual(package["sourceAnalysis"]["sha256"], hashlib.sha256(self.source.encode("utf-8")).hexdigest())
        self.assertEqual(package["sourceAnalysis"]["fileName"], "雨夜候车室.txt")
        for path in (directory / "audit.json", directory / "modules/module-audit.json"):
            self.assertEqual(json.loads(path.read_text())["status"], "passed")
        catalog_response = self.client.get(f'/api/v1/packages/{imported["package_id"]}/{imported["version"]}')
        self.assertEqual(catalog_response.status_code, 200, catalog_response.text)
        catalog = catalog_response.json()
        self.assertTrue(catalog["package"]["context_preview"]["available"])
        self.assertEqual(len(catalog["entries"]), 14)
        self.assertEqual({c["name"] for c in catalog["characters"]}, {"许川", "唐栖", "陈砚", "姜序"})
        entry = catalog["entries"][0]
        reference = {"package_id": imported["package_id"], "version": imported["version"]}
        preview = self.client.post("/api/v1/context/build", json={
            "package": reference, "entry_point_id": entry["id"],
            "source_character_id": entry["source_character_ids"][0],
        })
        self.assertEqual(preview.status_code, 200, preview.text)
        for character in catalog["characters"]:
            created = self.client.post("/api/v1/sessions", json={
                "package": reference, "entry_point_id": entry["id"], "source_character_id": character["id"],
            })
            self.assertEqual(created.status_code, 201, created.text)

    def test_reimport_reuses_package_without_rewriting_files(self):
        first = self.client.post("/api/v1/import/novel", json=self.payload)
        self.assertEqual(first.status_code, 201, first.text)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.packages.rglob("*") if p.is_file()}
        second = self.client.post("/api/v1/import/novel", json={**self.payload, "file_name": "重命名.TXT"})
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.packages.rglob("*") if p.is_file()})
        self.assertEqual(len(self.client.get("/api/v1/packages").json()["packages"]), 1)
        self.assertFalse(self.database.exists())

    def test_invalid_filename_or_source_publishes_nothing(self):
        for fields in ({"file_name": "小说.pdf"}, {"file_name": "../小说.txt"},
                       {"file_name": "C:\\小说.txt"}, {"source_text": "太短"},
                       {"source_text": "\x00" * 200}):
            with self.subTest(fields=fields):
                response = self.client.post("/api/v1/import/novel", json={**self.payload, **fields})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(self.packages.exists())
        self.assertFalse(self.database.exists())

    def test_failed_audit_does_not_publish_package(self):
        with patch("open_story_engine.api_import.audit_story_package_modules",
                   return_value={"status": "failed", "issues": [{"message": "测试审计失败"}]}):
            response = self.client.post("/api/v1/import/novel", json=self.payload)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "audit_failed")
        self.assertFalse(self.packages.exists())

    def test_interrupted_write_never_exposes_partial_package(self):
        def fail_write(*_args):
            self.assertEqual(self.client.get("/api/v1/packages").json(), {"packages": [], "issues": []})
            raise OSError("simulated write failure")

        with patch("open_story_engine.api_import.write_story_package_modules", side_effect=fail_write):
            with self.assertRaisesRegex(OSError, "simulated write failure"):
                self.client.post("/api/v1/import/novel", json=self.payload)
        self.assertEqual(list(self.packages.iterdir()), [])
        retry = self.client.post("/api/v1/import/novel", json=self.payload)
        self.assertEqual(retry.status_code, 201, retry.text)


if __name__ == "__main__":
    unittest.main()
