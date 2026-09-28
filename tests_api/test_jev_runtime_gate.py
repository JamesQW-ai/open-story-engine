"""Opt-in Jev runtime gate sequencing tests using a fake gateway."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from open_story_engine.api_play import PlayService
from open_story_engine.llm import LlmError


class _Bundle:
    def project(self, stage):
        assert stage == "grounding_review"
        return {
            "package": {"id": "test", "version": "0.1"},
            "branch": {"branchId": "parent"},
            "hardConstraints": {"globalConstraints": ["不得新增未经证实的事实"]},
            "turnIntent": {"rawInput": "观察", "selectedDirection": {"title": "观察"}},
            "continuityWindow": [],
            "dynamicMemory": [],
            "authoritativeState": {"playerLocationId": "hall"},
            "outputContract": {"mustStop": True, "selectedStatePatch": {}},
            "allowedEvidence": [{"sourceId": "opening", "quote": "登记堂"}],
            "contextSha256": "ctx-hash",
        }


class _Gateway:
    response = {"status": "ok", "decision": "allow", "reason": "no_blocking_signal", "durationMs": 1}

    def __init__(self, _config):
        pass

    def review(self, state, *, request_id=None):
        self.state = state
        return dict(self.response, requestId=request_id)


class JevRuntimeGateTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(PlayService)
        self.snapshot = SimpleNamespace(
            history=[{"id": "parent"}],
            audits=[],
        )
        self.planner = SimpleNamespace(last_context_bundle=_Bundle())
        self.payload = {"text": "观察登记堂", "_request_id": "request-1"}
        self.node = {"narrativeText": "你留在登记堂，记下门上的刻痕。"}

    def test_shadow_mode_records_review_without_blocking(self):
        with patch.dict(os.environ, {"JEV_API_KEY": "test-key"}), patch(
            "open_story_engine.api_play.JevRuntimeGateway", _Gateway
        ):
            result = self.service._run_jev_runtime_review(
                self.planner, self.snapshot, self.payload, self.node,
                "request-1", lambda: None, "shadow",
            )
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(len(self.snapshot.audits), 1)
        audit, parent_id = self.snapshot.audits[0]
        self.assertEqual(parent_id, "parent")
        self.assertEqual(audit["operation"], "jev_runtime_review")
        self.assertEqual(audit["mode"], "shadow")
        self.assertEqual(audit["stateFields"], sorted({
            "context", "branchState", "playerAction", "resultContract",
            "allowedEvidence", "allowedMechanisms", "candidateNarrative", "reviewMeta",
        }))

    def test_block_mode_rejects_non_allow_before_commit(self):
        class RejectingGateway(_Gateway):
            response = {"status": "ok", "decision": "rewrite", "reason": "model_rewrite", "durationMs": 1}

        with patch.dict(os.environ, {"JEV_API_KEY": "test-key"}), patch(
            "open_story_engine.api_play.JevRuntimeGateway", RejectingGateway
        ):
            with self.assertRaises(LlmError) as caught:
                self.service._run_jev_runtime_review(
                    self.planner, self.snapshot, self.payload, self.node,
                    "request-1", lambda: None, "block",
                )
        self.assertEqual(caught.exception.code, "jev_review_rejected")
        self.assertEqual(caught.exception.failure_stage, "jev_runtime_review")
        self.assertEqual(len(self.snapshot.audits), 1)

    def test_missing_projection_is_audited_and_blocks_without_network_call(self):
        self.planner.last_context_bundle = None
        with patch("open_story_engine.api_play.JevRuntimeGateway") as gateway:
            with self.assertRaises(LlmError) as caught:
                self.service._run_jev_runtime_review(
                    self.planner, self.snapshot, self.payload, self.node,
                    "request-1", lambda: None, "block",
                )
        gateway.assert_not_called()
        self.assertEqual(caught.exception.code, "jev_review_unavailable")
        self.assertEqual(self.snapshot.audits[0][0]["result"]["reason"], "context_projection_unavailable")


if __name__ == "__main__":
    unittest.main()
