"""OpenAI-compatible JSON/SSE client with observable retry behavior."""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LlmError(RuntimeError):
    def __init__(self, message: str, code: str = "response_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Completion:
    content: str
    raw_response: str
    observations: List[Dict[str, Any]]
    used_transport_fallback: bool = False


class OpenAICompatibleGateway:
    def __init__(self, base_url: str, api_key: str, model: str, stream: bool, timeout_seconds: int = 45) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.stream = stream
        self.timeout_seconds = timeout_seconds

    def complete_json(
        self,
        messages: List[Dict[str, str]],
        on_delta: Optional[Callable[[str], None]] = None,
        on_reset: Optional[Callable[[str], None]] = None,
    ) -> Completion:
        request_body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.65,
            "response_format": {"type": "json_object"},
            "stream": self.stream,
        }
        start = time.monotonic()
        try:
            if not self.stream:
                content, raw = self._json(request_body)
                return Completion(
                    content=content,
                    raw_response=raw,
                    observations=[self._observation(1, "completed", "json", start)],
                )

            try:
                content, raw = self._stream(request_body, on_delta)
                return Completion(
                    content=content,
                    raw_response=raw,
                    observations=[self._observation(1, "completed", "sse", start)],
                )
            except (LlmError, TimeoutError, socket.timeout) as error:
                # Some compatible gateways accept SSE but never provide delta.content.
                # A JSON retry is safe here: a generation has no runtime side effect and
                # persistence only happens after all local validation succeeds.
                if isinstance(error, LlmError) and error.code != "empty_stream":
                    raise
                if on_reset:
                    on_reset("transport_fallback")
                fallback_started = time.monotonic()
                fallback_body = {**request_body, "stream": False}
                content, raw = self._json(fallback_body)
                return Completion(
                    content=content,
                    raw_response=raw,
                    observations=[
                        self._observation(1, "failed", "sse", start, str(error)),
                        self._observation(2, "completed", "json", fallback_started, retry_reason="transport_fallback"),
                    ],
                    used_transport_fallback=True,
                )
        except (HTTPError, URLError, TimeoutError, socket.timeout, ValueError) as error:
            raise LlmError(str(error), "transport_error") from error

    def _observation(
        self,
        attempt: int,
        outcome: str,
        mode: str,
        started: float,
        error: Optional[str] = None,
        retry_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        observation: Dict[str, Any] = {
            "attempt": attempt,
            "outcome": outcome,
            "transport": {"responseMode": mode, "httpStatus": 200, "durationMs": round((time.monotonic() - started) * 1000)},
        }
        if error:
            observation["error"] = error
        if retry_reason:
            observation["retryReason"] = retry_reason
        return observation

    def _request(self, body: Dict[str, Any]) -> Any:
        request = Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json", "Accept": "text/event-stream" if body["stream"] else "application/json"},
            method="POST",
        )
        return urlopen(request, timeout=self.timeout_seconds)

    def _json(self, body: Dict[str, Any]) -> tuple[str, str]:
        with self._request(body) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        choices = payload.get("choices") or []
        choice = choices[0] if choices else {}
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str):
            # A few OpenAI-compatible providers return a completed response in
            # the streaming-shaped delta field even when stream=false.
            content = (choice.get("delta") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LlmError("LLM 响应缺少 choices[0].message.content 或 choices[0].delta.content", "empty_json")
        return content, raw

    def _stream(self, body: Dict[str, Any], on_delta: Optional[Callable[[str], None]]) -> tuple[str, str]:
        chunks: List[str] = []
        raw_events: List[str] = []
        with self._request(body) as response:
            for encoded in response:
                line = encoded.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                raw_events.append(data)
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = payload.get("choices") or []
                choice = choices[0] if choices else {}
                delta = (choice.get("delta") or {}).get("content")
                if not isinstance(delta, str):
                    delta = (choice.get("message") or {}).get("content")
                if isinstance(delta, str) and delta:
                    chunks.append(delta)
                    if on_delta:
                        on_delta(delta)
        content = "".join(chunks)
        if not content.strip():
            raise LlmError("LLM 响应缺少 choices[0].delta.content 或 choices[0].message.content", "empty_stream")
        return content, "\n".join(raw_events)


def parse_json_content(content: str) -> Dict[str, Any]:
    normalized = content.strip()
    if normalized.startswith("```"):
        first_newline = normalized.find("\n")
        normalized = normalized[first_newline + 1:] if first_newline >= 0 else normalized
        if normalized.rstrip().endswith("```"):
            normalized = normalized.rstrip()[:-3].rstrip()
    try:
        # Some OpenAI-compatible endpoints prepend a short explanation or append
        # a completion marker despite response_format=json_object. Accept exactly
        # one complete object and leave surrounding transport noise out of the
        # planner contract. An incomplete object still fails closed.
        opening = normalized.find("{")
        if opening < 0:
            raise json.JSONDecodeError("missing object", normalized, 0)
        parsed, _ = json.JSONDecoder().raw_decode(normalized[opening:])
    except json.JSONDecodeError as error:
        raise LlmError("LLM 未返回有效 JSON") from error
    if not isinstance(parsed, dict):
        raise LlmError("LLM JSON 根节点必须是对象")
    return parsed
