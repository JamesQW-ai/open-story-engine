import json
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_support.nq001_live_eval import SseEventParser, _choose_entry, _sse_payload, main as live_main
from test_support.repro_audit import cjk_count, duplicate_candidates
from open_story_engine.api_turn_drafts import decorate_audits
from open_story_engine.reader_scene_review import (
    SceneReviewError, validate_knowledge_access, validate_repair_resolution,
)


class Nq001ToolTests(unittest.TestCase):
    def test_first_inform_replay_rejects_overreach_accepts_limited_knowledge_and_tracks_repair(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "nq001_first_inform_replay.json").read_text(encoding="utf-8"))
        people = {"guard": "守门弟子", "lu": "陆沉舟"}
        for attempt in fixture["attempts"]:
            body = attempt["body"]
            quote = next(item for item in attempt["rejectedClaims"] if item in body)
            with self.assertRaises(SceneReviewError):
                validate_knowledge_access({"knowledgeChecks": [{
                    "id": "D1", "speakerId": "guard", "kind": "background",
                    "verdict": "supported", "reason": "守门弟子知道这件事",
                    "accessSources": [], "missingEvidence": [],
                }]}, body, fixture["evidence"], people, "lu")
            self.assertIn(quote, body)

        limited = fixture["legalLimitedKnowledge"]
        validate_knowledge_access({"knowledgeChecks": [{
            "id": "D1", "speakerId": "guard", "kind": "unknown",
            "verdict": "supported", "reason": "只表达当前无法判断",
            "accessSources": [], "missingEvidence": [],
        }]}, limited, fixture["evidence"], people, "lu")

        repaired = fixture["repairedBody"]
        validate_knowledge_access({"knowledgeChecks": [{
            "id": "D1", "speakerId": "guard", "kind": "current",
            "verdict": "supported", "reason": "只转述刚刚听到的话",
            "accessSources": [], "missingEvidence": [],
        }]}, repaired, fixture["evidence"], people, "lu")

        target = {"R1": {"paragraphId": "P1", "type": "background",
                         "quote": "封山线内不得靠近，山门将闭。",
                         "beforeOccurrences": 1}}
        with self.assertRaises(SceneReviewError):
            validate_repair_resolution({"repairChecks": [{
                "id": "R1", "verdict": "resolved", "reason": "已修复",
                "paragraphIds": ["P1"],
            }]}, fixture["failedRepairBody"], target)

        validate_repair_resolution({"repairChecks": [{
            "id": "R1", "verdict": "resolved", "reason": "删除无来源的背景断言",
            "paragraphIds": ["P1"],
        }]}, repaired, target)

    def test_failed_replay_retains_unconfirmed_draft_without_artifact(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "nq001_first_inform_replay.json").read_text(encoding="utf-8"))
        audit = {"callObservations": [{
            "generationStage": "validation", "revision": 0,
            "outcome": "failed", "bodySha256": "body-before-review",
        }, {
            "generationStage": "local_repair_validation", "revision": 0,
            "outcome": "rejected", "error": "仍有无来源断言",
            "bodySha256": "body-after-repair",
        }], "retainedDraft": {"text": fixture["failedRepairBody"], "status": "unconfirmed"}}
        audits = [[audit, "branch-root"]]
        decorate_audits(audits, "nq001-request", {"calls": 4, "reported_tokens": 20}, 1234)
        self.assertEqual(audit["requestId"], "nq001-request")
        self.assertEqual(audit["turnDurationMs"], 1234)
        self.assertEqual([item["callSequence"] for item in audit["callObservations"]], [1, 2])
        self.assertEqual(audit["callObservations"][0]["reviewTargetBodySha256"], "body-before-review")
        self.assertEqual(audit["callObservations"][1]["reviewTargetBodySha256"], "body-after-repair")
        self.assertEqual(audit["retainedDraft"]["status"], "unconfirmed")
        self.assertNotIn("artifact", audit)

    def test_targeted_three_issue_replay_requires_all_repairs_before_artifact(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "nq001_first_inform_replay.json").read_text(encoding="utf-8"))
        failed = fixture["threeIssueBody"]
        repaired = fixture["threeIssueRepairedBody"]
        targets = {
            "R1": {"paragraphId": "P1", "type": "background",
                   "quote": "是新的。先前没有，方才那阵青光之后才裂开。", "beforeOccurrences": 1},
            "R2": {"paragraphId": "P1", "type": "background",
                   "quote": "铁链再响两回。", "beforeOccurrences": 1},
            "R3": {"paragraphId": "P1", "type": "continuity",
                   "quote": "门快关了。", "beforeOccurrences": 1},
        }
        with self.assertRaises(SceneReviewError):
            validate_repair_resolution({"repairChecks": [
                {"id": key, "verdict": "resolved", "paragraphIds": ["P1"], "reason": "已修复"}
                for key in ("R1", "R2", "R3")
            ]}, failed, targets)
        validate_repair_resolution({"repairChecks": [
            {"id": key, "verdict": "resolved", "paragraphIds": ["P1"], "reason": "删除无来源断言并保留当场可观察内容"}
            for key in ("R1", "R2", "R3")
        ]}, repaired, targets)
        self.assertIn("听见铁链在近处响了一声", repaired)
        self.assertIn("我不知道", repaired)
        failed_audit = {"retainedDraft": {"text": repaired, "status": "unconfirmed"}}
        self.assertNotIn("artifact", failed_audit)

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
                    self._write({"package": {"id": "taixu-relics-part1", "version": "0.1.2"}, "entries": [{"id": "entry-1", "source_character_ids": ["char-1"]}]})
                else:
                    self._write({"session": {"id": "session-1"}, "branch": {"id": "branch-1", "nextDirections": [{"id": "direction-1"}]}})

            def do_POST(self):
                Handler.counter += 1
                if self.path.endswith("/end"):
                    self._write({"status": "completed"})
                elif self.path.endswith("/sessions"):
                    self._write({"session": {"id": "session-1"}, "branch": {"id": "branch-1", "nextDirections": [{"id": "direction-1"}], "narrativeText": "开场正文"}}, 201)
                else:
                    self._write({"status": "written", "request_id": "request", "deduplicated": False, "kind": "accepted", "branch": {"id": f"branch-{Handler.counter}", "nextDirections": [{"id": "direction-1"}], "narrativeText": "回合正文"}})

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

    def test_choose_entry_reads_current_catalog_entries(self):
        entry_id, character_id, gaps = _choose_entry({"entries": [{"id": "entry-1", "source_character_ids": ["char-1"]}]})
        self.assertEqual((entry_id, character_id, gaps), ("entry-1", "char-1", []))

    def test_sse_chunk_boundaries_do_not_change_result(self):
        raw = 'event: delta\ndata: {"text":"甲"}\n\nevent: delta\ndata: {"text":"乙"}\n\nevent: done\ndata: {"status":"written","branch":{"id":"b"}}\n\n'
        expected = _sse_payload(raw)
        for split in range(1, len(raw)):
            parser = SseEventParser()
            events = parser.feed(raw[:split]) + parser.feed(raw[split:])
            parser.finish()
            self.assertEqual([(e.event, e.data) for e in events], [(e.event, e.data) for e in expected.events])
        self.assertTrue(expected.formal_commit)
        self.assertEqual(expected.preview_text, "甲乙")

    def test_sse_body_then_error_is_failure_and_keeps_unconfirmed_body(self):
        result = _sse_payload('event: delta\ndata: {"text":"未确认正文"}\n\nevent: error\ndata: {"code":"generation_failed"}\n\n')
        self.assertEqual(result.preview_text, "未确认正文")
        self.assertFalse(result.formal_commit)
        self.assertEqual(result.failure_code, "generation_failed")

    def test_sse_done_without_commit_is_not_success(self):
        result = _sse_payload('event: done\ndata: {"status":"rejected","branch":{"id":"b"}}\n\n')
        self.assertFalse(result.formal_commit)
        self.assertEqual(result.failure_code, "stream_ended_without_commit")

    def test_sse_multiline_data_and_eof_without_terminal_are_not_success(self):
        result = _sse_payload('event: done\ndata: {"status":"written",\ndata: "branch"}\n\n')
        self.assertEqual(result.events[0].data, '{"status":"written",\n"branch"}')
        self.assertFalse(result.formal_commit)
        incomplete = _sse_payload('event: done\ndata: {"status":"written"}\n')
        self.assertEqual(incomplete.failure_code, "stream_ended_without_terminal")


if __name__ == "__main__":
    unittest.main()
