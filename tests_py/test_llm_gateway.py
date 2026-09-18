import io
import json
import unittest
from copy import deepcopy

from open_story_engine.llm import Completion, OpenAICompatibleGateway


class LlmGatewayTests(unittest.TestCase):
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
