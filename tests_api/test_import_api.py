"""玩家端不提供小说上传；即使开启游玩也不能发布故事包。"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app


class ImportApiTests(unittest.TestCase):
    def test_upload_is_unavailable_without_writes_in_both_modes(self):
        for play in (False, True):
            with self.subTest(play=play), TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.dict(os.environ, {"STORY_PLANNER": "mock"}), \
                     patch("open_story_engine.api_import.ImportService.import_novel",
                           side_effect=AssertionError("玩家请求不得调用作者构建器")), \
                     TestClient(create_app(root / "packages", root / "sessions.sqlite", play=play)) as client:
                    before = list(root.rglob("*"))
                    for payload in ({}, {"file_name": "故事.txt", "source_text": "原著内容" * 100}):
                        response = client.post("/api/v1/import/novel", json=payload)
                        self.assertEqual(response.status_code, 404, response.text)
                        self.assertEqual(response.json()["error"]["code"], "endpoint_unavailable")
                    schema = client.get("/openapi.json").json()
                    self.assertNotIn("/api/v1/import/novel", schema["paths"])
                    self.assertNotIn("ImportNovelRequest", schema["components"]["schemas"])
                    self.assertEqual(before, list(root.rglob("*")))
                    self.assertEqual(client.get("/api/v1/packages").json()["packages"], [])


if __name__ == "__main__":
    unittest.main()
