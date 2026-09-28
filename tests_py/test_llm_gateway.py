import io
import json
import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from open_story_engine.llm import Completion, OpenAICompatibleGateway, writer_config_from_env


class LlmGatewayTests(unittest.TestCase):
    def test_writer_config_selects_flash_direct_route(self):
        with patch.dict(os.environ, {
            "STORY_LLM_ROUTE": "direct",
            "STORY_LLM_DIRECT_BASE_URL": "https://api.deepseek.com",
            "STORY_LLM_DIRECT_API_KEY": "direct-test-key",
            "STORY_LLM_DIRECT_MODEL": "deepseek-v4-flash",
            "STORY_LLM_BASE_URL": "http://relay.invalid/v1",
            "STORY_LLM_API_KEY": "relay-test-key",
            "STORY_LLM_MODEL": "deepseek-v4-flash",
        }, clear=False):
            self.assertEqual(writer_config_from_env(), {
                "route": "direct",
                "base_url": "https://api.deepseek.com",
                "api_key": "direct-test-key",
                "model": "deepseek-v4-flash",
            })

    def test_writer_config_relay_uses_flash_model(self):
        with patch.dict(os.environ, {
            "STORY_LLM_ROUTE": "relay",
            "STORY_LLM_BASE_URL": "http://relay.invalid/v1",
            "STORY_LLM_API_KEY": "relay-test-key",
            "STORY_LLM_MODEL": "deepseek-v4-flash",
        }, clear=False):
            self.assertEqual(writer_config_from_env()["model"], "deepseek-v4-flash")

    def test_rejects_deepseek_model_when_base_url_is_openrouter(self):
        with self.assertRaisesRegex(ValueError, "禁止通过 OpenRouter 调用 DeepSeek"):
            OpenAICompatibleGateway(
                "https://openrouter.ai/api/v1",
                "key",
                "deepseek-v4-flash",
                stream=False,
            )

    def test_allows_deepseek_model_on_company_relay(self):
        gateway = OpenAICompatibleGateway(
            "http://152.70.196.2:8080/v1",
            "key",
            "deepseek-v4-flash",
            stream=False,
        )
        self.assertEqual(gateway.model, "deepseek-v4-flash")

    def test_gateway_deepcopy_recreates_thread_local_transport_metrics(self):
        gateway = OpenAICompatibleGateway("http://unused.invalid", "key", "model", stream=False)
        copied = deepcopy(gateway)

        self.assertIsNot(copied, gateway)
        self.assertIsNot(copied._transport_local, gateway._transport_local)
        self.assertEqual(copied.base_url, gateway.base_url)
        self.assertEqual(copied.model, gateway.model)

    def test_json_requests_do_not_inherit_global_stream_setting(self):
        class CapturingGateway(OpenAICompatibleGateway):
            def __init__(self):
                super().__init__("http://unused.invalid", "key", "model", stream=True, allow_transport_fallback=False)
                self.bodies = []

            def _json(self, body, timeout_seconds=None):
                self.bodies.append(body)
                return "{\"ok\": true}", "raw"

            def _stream(self, body, on_delta, timeout_seconds=None):
                self.bodies.append(body)
                return "正文", "raw"

        gateway = CapturingGateway()
        json_result = gateway.complete_json([])
        text_result = gateway.complete_text([])

        self.assertIsInstance(json_result, Completion)
        self.assertIsInstance(text_result, Completion)
        self.assertEqual([body["stream"] for body in gateway.bodies], [False, True])

    def test_first_delta_timeout_can_be_configured(self):
        configured = OpenAICompatibleGateway(
            "http://unused.invalid", "key", "model", stream=True,
            timeout_seconds=60, first_delta_timeout_seconds=42,
        )
        defaulted = OpenAICompatibleGateway("http://unused.invalid", "key", "model", stream=True, timeout_seconds=60)

        self.assertEqual(configured._first_sse_delta_timeout_seconds(), 42)
        self.assertEqual(defaulted._first_sse_delta_timeout_seconds(), 15)

    def test_structured_json_and_prose_budgets_are_independent(self):
        class CapturingGateway(OpenAICompatibleGateway):
            def _json(self, body, _timeout_seconds=None):
                self.request_body = body
                return '{"ok": true}', "json"

            def _stream(self, body, _on_delta, _timeout_seconds=None):
                self.request_body = body
                return '{"ok": true}', "stream"

        with patch.dict(os.environ, {"STORY_LLM_JSON_MAX_TOKENS": "4096"}, clear=False):
            gateway = CapturingGateway("http://localhost", "test-key", "test-model", stream=True, text_max_tokens=4096)
            gateway.complete_json([])
            self.assertEqual(gateway.request_body["max_tokens"], 4096)
            gateway.complete_text([])
            self.assertEqual(gateway.request_body["max_tokens"], 4096)
            defaulted = CapturingGateway("http://localhost", "test-key", "test-model", stream=True)
            defaulted.complete_text([])
            self.assertEqual(defaulted.request_body["max_tokens"], 8192)

    def test_json_observation_records_response_and_completion_timing(self):
        class JsonGateway(OpenAICompatibleGateway):
            def _request(self, body, timeout_seconds=None):
                self._mark_transport_metric("responseHeadersMs")
                payload = {"choices": [{"message": {"content": '{"ok":true}'}}]}
                return io.BytesIO(json.dumps(payload).encode("utf-8"))

        completion = JsonGateway(
            "http://unused.invalid", "key", "model", stream=True, allow_transport_fallback=False,
        ).complete_json([])
        transport = completion.observations[0]["transport"]

        self.assertEqual(transport["responseMode"], "json")
        self.assertIsInstance(transport["responseHeadersMs"], int)
        self.assertIsInstance(transport["completeBodyMs"], int)
        self.assertGreaterEqual(transport["completeBodyMs"], transport["responseHeadersMs"])

    def test_stream_observation_records_first_event_content_and_completion_timing(self):
        class StreamGateway(OpenAICompatibleGateway):
            def _request(self, body, timeout_seconds=None):
                self._mark_transport_metric("responseHeadersMs")
                lines = [
                    'data: {"choices":[{"delta":{"content":"第一段"}}]}\n'.encode("utf-8"),
                    b'data: [DONE]\n',
                ]
                return io.BytesIO(b"".join(lines))

        completion = StreamGateway(
            "http://unused.invalid", "key", "model", stream=True, allow_transport_fallback=False,
        ).complete_text([])
        transport = completion.observations[0]["transport"]

        self.assertEqual(completion.content, "第一段")
        self.assertEqual(transport["responseMode"], "sse")
        self.assertIsInstance(transport["responseHeadersMs"], int)
        self.assertIsInstance(transport["firstEventMs"], int)
        self.assertIsInstance(transport["firstContentMs"], int)
        self.assertIsInstance(transport["completeBodyMs"], int)
        self.assertGreaterEqual(transport["firstContentMs"], transport["firstEventMs"])
        self.assertGreaterEqual(transport["completeBodyMs"], transport["firstContentMs"])


if __name__ == "__main__":
    unittest.main()
