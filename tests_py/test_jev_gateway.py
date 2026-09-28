import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from open_story_engine.jev_gateway import JevGatewayConfig, JevRuntimeGateway, JevShadowGateway, runtime_review_mode
from test_support.jev_review_rules import QUESTIONS


def _answers():
    result = {"review_decision": {"type": "choice", "choice": "allow"}}
    for key, question in QUESTIONS.items():
        if key != "review_decision":
            result[key] = {"type": question["type"], "noul": 1.0 if key == "claim_supported" else 0.0}
    return result


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    last_request_body = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        _Handler.last_request_body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.mode == "timeout":
            time.sleep(0.25)
        if self.mode == "http_error":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"unavailable")
            return
        payload = {"model": "typesafe/jev-test", "provider": "test", "usage": {"input_tokens": 1}, "answers": _answers()}
        if self.mode == "invalid":
            payload = {"answers": {}}
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            # Expected when the client timeout closes the test socket first.
            pass

    def log_message(self, *_args):
        return


class JevGatewayTests(unittest.TestCase):
    def setUp(self):
        _Handler.last_request_body = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def gateway(self, mode="ok", timeout=1):
        _Handler.mode = mode
        return JevShadowGateway(JevGatewayConfig(self.url, "test-key", "jev-test", timeout))

    def state(self):
        return {
            "context": "当前证据",
            "branchState": "location=hall",
            "playerAction": "观察",
            "resultContract": "停留原地",
            "candidateNarrative": "你观察眼前的门。",
            "allowedEvidence": "门在眼前。",
        }

    def test_success_is_typed_and_shadow_only(self):
        result = self.gateway().review(self.state(), request_id="req-1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["decision"], "allow")
        self.assertTrue(result["shadow"])
        self.assertEqual(result["requestId"], "req-1")
        self.assertNotIn("statePatch", result)

    def test_compact_transport_preserves_review_state(self):
        state = self.state()
        result = self.gateway().review(state, request_id="compact-1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(_Handler.last_request_body["state"], state)
        self.assertEqual(sorted(_Handler.last_request_body["questions"]), sorted(QUESTIONS))

    def test_provider_sort_is_opt_in(self):
        with patch.dict(os.environ, {"JEV_PROVIDER_SORT": "latency"}):
            gateway = JevShadowGateway(JevGatewayConfig.from_env())
        self.assertEqual(gateway.config.provider_sort, "latency")
        gateway.config = gateway.config.__class__(self.url, "test-key", "jev-test", 1, True, "latency")
        result = gateway.review(self.state(), request_id="provider-sort-1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(_Handler.last_request_body["provider"], {"sort": "latency"})

    def test_runtime_wrapper_marks_explicit_experiment_without_changing_result(self):
        gateway = JevRuntimeGateway(JevGatewayConfig(self.url, "test-key", "jev-test", 1, True))
        result = gateway.review(self.state(), request_id="runtime-1")
        self.assertFalse(result["shadow"])
        self.assertTrue(result["runtimeExperiment"])
        self.assertEqual(result["decision"], "allow")

    def test_runtime_mode_is_explicit_and_reversible(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEV_RUNTIME_REVIEW_MODE", None)
            self.assertEqual(runtime_review_mode(), "off")
        with patch.dict(os.environ, {"JEV_RUNTIME_REVIEW_MODE": "shadow"}):
            self.assertEqual(runtime_review_mode(), "shadow")
        with patch.dict(os.environ, {"JEV_RUNTIME_REVIEW_MODE": "invalid"}):
            with self.assertRaises(ValueError):
                runtime_review_mode()

    def test_missing_key_does_not_call_network(self):
        gateway = JevShadowGateway(JevGatewayConfig(self.url, "", "jev-test", 1))
        result = gateway.review(self.state())
        self.assertEqual(result["errorKind"], "missing_api_key")

    def test_preflight_rejection_is_code_owned_without_network_call(self):
        _Handler.mode = "invalid"
        state = self.state()
        state["reviewMeta"] = {"contractPresent": False}
        result = self.gateway().review(state)
        self.assertEqual(result["status"], "preflight_rejected")
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["reasonCodes"], ["missing_result_contract"])

    def test_http_error_is_non_blocking_result(self):
        result = self.gateway("http_error").review(self.state())
        self.assertEqual(result["errorKind"], "http_error")

    def test_invalid_response_is_rejected(self):
        result = self.gateway("invalid").review(self.state())
        self.assertEqual(result["errorKind"], "invalid_response")

    def test_timeout_has_bounded_result(self):
        result = self.gateway("timeout", timeout=0.1).review(self.state())
        self.assertIn(result["errorKind"], {"transport_error", "invalid_response"})
        self.assertLess(result["durationMs"], 1000)


if __name__ == "__main__":
    unittest.main()
