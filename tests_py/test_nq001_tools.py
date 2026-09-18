import json
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_support.nq001_live_eval import main as live_main
from test_support.repro_audit import cjk_count, duplicate_candidates


class Nq001ToolTests(unittest.TestCase):
    def test_mock_http_run_writes_request_response_and_failure_stage_evidence(self):
        class Handler(BaseHTTPRequestHandler):
            counter = 0

            def log_message(self, *_args):
                return

            def _write(self, value, status=200):
                raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if "/packages/" in self.path:
                    self._write({"story": {"entryModel": {"entryPoints": [{"id": "entry-1", "sourceCharacterId": "char-1"}]}}})
                else:
                    self._write({"session": {"id": "session-1"}, "branch": {"id": "branch-1", "nextDirections": [{"id": "direction-1"}]}})

            def do_POST(self):
                Handler.counter += 1
                if self.path.endswith("/end"):
                    self._write({"status": "completed"})
                elif self.path.endswith("/sessions"):
                    self._write({"session": {"id": "session-1"}, "branch": {"id": "branch-1", "nextDirections": [{"id": "direction-1"}], "narrativeText": "开场正文"}}, 201)
                else:
                    self._write({"status": "committed", "branch": {"id": f"branch-{Handler.counter}", "nextDirections": [{"id": "direction-1"}], "narrativeText": "回合正文"}})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "run"
                result = live_main([
                    "--output", str(output), "--base-url", f"http://127.0.0.1:{server.server_port}",
                    "--package-id", "taixu-relics-part1", "--package-version", "0.1.2", "--model", "mock-model",
                    "--prompt-version", "prompt-test-1", "--route-id", "route-test", "--mode", "mock",
                ])
                self.assertEqual(result, 1)
                report = json.loads((output / "report.json").read_text())
                self.assertEqual(report["status"], "offline_mock_only")
                self.assertEqual(len(list((output / "calls").glob("*.json"))), 1 + 8 * 4)
                evidence = json.loads(next((output / "calls").glob("*.json")).read_text())
                self.assertIn("request", evidence)
                self.assertIn("response", evidence)
                self.assertIn("model_response", evidence)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_scenario_dry_run_preserves_non_acceptance_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            result = live_main([
                "--output", str(output), "--package-id", "taixu-relics-part1", "--package-version", "0.1.2",
                "--model", "test-model", "--prompt-version", "prompt-test-1", "--route-id", "route-test", "--dry-run",
            ])
            self.assertEqual(result, 1)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "offline_plan_only")
            self.assertFalse(report["config"]["acceptance_claim"])
            self.assertEqual(len(report["scenarios"]), 8)
            self.assertEqual(sum(item["step_count"] for item in report["scenarios"]), 24)

    def test_cjk_count_does_not_count_punctuation_or_ascii(self):
        self.assertEqual(cjk_count("甲乙 A1，。\n丙"), 3)

    def test_duplicate_candidates_are_report_only(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            paragraph = "这是一个足够长的段落，用于检查重复候选不会被审计器删除，也不会被拿来补足字符数。"
            source.write_text(paragraph + "\n\n" + paragraph + "\n", encoding="utf-8")
            result = duplicate_candidates(source)
            self.assertGreaterEqual(result["candidate_count"], 1)
            self.assertEqual(source.read_text(encoding="utf-8").count(paragraph), 2)


if __name__ == "__main__":
    unittest.main()
