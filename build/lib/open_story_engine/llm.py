"""OpenAI-compatible JSON/SSE client with fresh per-request connections."""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LlmError(RuntimeError):
    def __init__(self, message: str, code: str = "response_error") -> None:
        super().__init__(message)
        self.code = code
        self.observations: List[Dict[str, Any]] = []
        self.raw_response: Optional[str] = None
        self.http_status: Optional[int] = None


@dataclass
class Completion:
    content: str
    raw_response: str
    observations: List[Dict[str, Any]]
    used_transport_fallback: bool = False
    body_was_streamed: bool = False


class OpenAICompatibleGateway:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        stream: bool,
        timeout_seconds: int = 45,
        max_tokens: int = 8192,
        allow_transport_fallback: bool = True,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.stream = stream
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.allow_transport_fallback = allow_transport_fallback
        self.reasoning_effort = reasoning_effort
        # Only the fixed JSON, no-fallback live evaluation sets this budget.
        self.remaining_calls: Optional[int] = None

    def _first_sse_delta_timeout_seconds(self) -> int:
        """Allow compatible endpoints time to start prose or reasoning events."""
        # A player cannot distinguish a silent connection from a stalled one.
        # Reserve most of a turn's budget for the transport that can expose
        # prose, instead of spending half the turn waiting with no feedback.
        return min(self.timeout_seconds, min(15, max(5, self.timeout_seconds // 4)))

    def _fallback_json_timeout_seconds(self) -> int:
        """Reserve the remainder of one turn for JSON after a silent SSE start."""
        remaining = self.timeout_seconds - self._first_sse_delta_timeout_seconds()
        return max(0, remaining)

    def _first_json_response_timeout_seconds(self) -> int:
        """Prefer visible SSE output when a non-stream response stays silent."""
        return min(self.timeout_seconds, min(15, max(5, self.timeout_seconds // 4)))

    def _fallback_sse_timeout_seconds(self) -> int:
        """Reserve the remainder of one turn for SSE after a silent JSON start."""
        remaining = self.timeout_seconds - self._first_json_response_timeout_seconds()
        return max(0, remaining)

    @staticmethod
    def _remaining_until(deadline: float, timeout_message: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout(timeout_message)
        return remaining

    def complete_json(
        self,
        messages: List[Dict[str, str]],
        on_delta: Optional[Callable[[str], None]] = None,
        on_reset: Optional[Callable[[str], None]] = None,
    ) -> Completion:
        return self._complete(
            messages,
            {"type": "json_object"},
            on_delta,
            on_reset,
        )

    def complete_text(
        self,
        messages: List[Dict[str, str]],
        on_delta: Optional[Callable[[str], None]] = None,
        on_reset: Optional[Callable[[str], None]] = None,
    ) -> Completion:
        """Request prose without making the provider serialize application state."""
        return self._complete(messages, None, on_delta, on_reset)

    def _complete(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, str]],
        on_delta: Optional[Callable[[str], None]],
        on_reset: Optional[Callable[[str], None]],
    ) -> Completion:
        if self.remaining_calls is not None:
            if self.remaining_calls <= 0:
                raise LlmError("已达到真实模型验收调用上限", "model_call_limit")
            self.remaining_calls -= 1
        request_body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.35,
            "max_tokens": self.max_tokens,
            "stream": self.stream,
        }
        if response_format is not None:
            request_body["response_format"] = response_format
        if self.reasoning_effort:
            request_body["reasoning_effort"] = self.reasoning_effort
        start = time.monotonic()
        deadline = start + self.timeout_seconds
        try:
            if not self.stream:
                if not self.allow_transport_fallback:
                    content, raw = self._json(
                        request_body,
                        self._remaining_until(deadline, "LLM 请求总超时"),
                    )
                    return Completion(
                        content=content,
                        raw_response=raw,
                        observations=[self._observation(1, "completed", "json", start)],
                    )
                try:
                    content, raw = self._json(
                        request_body,
                        min(
                            self._first_json_response_timeout_seconds(),
                            self._remaining_until(deadline, "LLM 请求总超时"),
                        ),
                    )
                    return Completion(
                        content=content,
                        raw_response=raw,
                        observations=[self._observation(1, "completed", "json", start)],
                    )
                except (TimeoutError, socket.timeout) as error:
                    # JSON responses often remain completely silent until a long
                    # story is finished. Retry the identical prompt through SSE
                    # so the player can see actual prose before the turn expires.
                    if on_reset:
                        on_reset("json_transport_fallback")
                    fallback_started = time.monotonic()
                    try:
                        content, raw = self._stream(
                            {**request_body, "stream": True},
                            on_delta,
                            min(
                                self._fallback_sse_timeout_seconds(),
                                self._remaining_until(deadline, "LLM 请求总超时"),
                            ),
                        )
                    except (HTTPError, URLError, TimeoutError, socket.timeout, LlmError) as fallback_error:
                        failure = LlmError(str(fallback_error), getattr(fallback_error, "code", "transport_error"))
                        failure.observations = [
                            self._observation(1, "failed", "json", start, str(error)),
                            self._observation(2, "failed", "sse", fallback_started, str(fallback_error), retry_reason="json_transport_fallback"),
                        ]
                        raise failure from fallback_error
                    return Completion(
                        content=content,
                        raw_response=raw,
                        observations=[
                            self._observation(1, "failed", "json", start, str(error)),
                            self._observation(2, "completed", "sse", fallback_started, retry_reason="json_transport_fallback"),
                        ],
                        used_transport_fallback=True,
                        body_was_streamed=True,
                    )

            if not self.allow_transport_fallback:
                content, raw = self._stream(
                    request_body,
                    on_delta,
                    self._remaining_until(deadline, "LLM 请求总超时"),
                )
                return Completion(
                    content=content,
                    raw_response=raw,
                    observations=[self._observation(1, "completed", "sse", start)],
                    body_was_streamed=on_delta is not None,
                )
            try:
                content, raw = self._stream(
                    request_body,
                    on_delta,
                    self._remaining_until(deadline, "LLM 请求总超时"),
                )
                return Completion(
                    content=content,
                    raw_response=raw,
                    observations=[self._observation(1, "completed", "sse", start)],
                    body_was_streamed=on_delta is not None,
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
                try:
                    content, raw = self._json(
                        fallback_body,
                        min(
                            self._fallback_json_timeout_seconds(),
                            self._remaining_until(deadline, "LLM 请求总超时"),
                        ),
                    )
                except (HTTPError, URLError, TimeoutError, socket.timeout, LlmError) as fallback_error:
                    failure = LlmError(str(fallback_error), getattr(fallback_error, "code", "transport_error"))
                    failure.observations = [
                        self._observation(1, "failed", "sse", start, str(error)),
                        self._observation(2, "failed", "json", fallback_started, str(fallback_error), retry_reason="transport_fallback"),
                    ]
                    raise failure from fallback_error
                return Completion(
                    content=content,
                    raw_response=raw,
                    observations=[
                        self._observation(1, "failed", "sse", start, str(error)),
                        self._observation(2, "completed", "json", fallback_started, retry_reason="transport_fallback"),
                    ],
                    used_transport_fallback=True,
                )
        except LlmError as error:
            if not error.observations:
                error.observations = [
                    self._observation(
                        1,
                        "failed",
                        "sse" if request_body["stream"] else "json",
                        start,
                        str(error),
                        failure_kind=error.code,
                    )
                ]
            raise
        except (HTTPError, URLError, TimeoutError, socket.timeout) as error:
            failure = LlmError(str(error), "transport_error")
            failure.observations = [
                self._observation(
                    1,
                    "failed",
                    "sse" if request_body["stream"] else "json",
                    start,
                    str(error),
                )
            ]
            raise failure from error
        except ValueError as error:
            failure = LlmError(str(error), "response_error")
            failure.observations = [
                self._observation(
                    1,
                    "failed",
                    "sse" if request_body["stream"] else "json",
                    start,
                    str(error),
                )
            ]
            raise failure from error

    def _observation(
        self,
        attempt: int,
        outcome: str,
        mode: str,
        started: float,
        error: Optional[str] = None,
        retry_reason: Optional[str] = None,
        failure_kind: Optional[str] = None,
        http_status: Optional[int] = None,
    ) -> Dict[str, Any]:
        observation: Dict[str, Any] = {
            "attempt": attempt,
            "outcome": outcome,
            "transport": {
                "responseMode": mode,
                "httpStatus": 200 if outcome == "completed" else http_status,
                "durationMs": round((time.monotonic() - started) * 1000),
            },
        }
        if error:
            observation["error"] = error
        if retry_reason:
            observation["retryReason"] = retry_reason
        if failure_kind:
            observation["failureKind"] = failure_kind
        return observation

    def _request(self, body: Dict[str, Any], timeout_seconds: Optional[int] = None) -> Any:
        request = Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            # A player may spend minutes reading a completed chapter. Do not
            # retain that chapter's HTTP connection for the next direction.
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if body["stream"] else "application/json",
                "Connection": "close",
            },
            method="POST",
        )
        timeout = timeout_seconds if timeout_seconds is not None else (self._first_sse_delta_timeout_seconds() if body["stream"] else self.timeout_seconds)
        result: Queue[tuple[str, Any]] = Queue()
        cancelled = [False]
        lock = Lock()

        def open_response() -> None:
            try:
                response = urlopen(request, timeout=timeout)
                with lock:
                    if cancelled[0]:
                        response.close()
                    else:
                        result.put(("response", response))
            except Exception as error:  # pragma: no cover - depends on network timing
                with lock:
                    if not cancelled[0]:
                        result.put(("error", error))

        Thread(target=open_response, daemon=True).start()
        try:
            kind, value = result.get(timeout=timeout)
        except Empty as error:
            with lock:
                cancelled[0] = True
                try:
                    queued_kind, queued_value = result.get_nowait()
                except Empty:
                    queued_kind = None
                    queued_value = None
                if queued_kind == "response":
                    queued_value.close()
            message = "SSE 连接或首段正文返回前超时" if body["stream"] else "JSON 响应连接超时"
            raise socket.timeout(message) from error
        if kind == "error":
            raise value
        return value

    def _json(self, body: Dict[str, Any], timeout_seconds: Optional[int] = None) -> tuple[str, str]:
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        deadline = time.monotonic() + timeout
        response = self._request(body, self._remaining_until(deadline, "JSON 响应连接超时"))
        try:
            raw_bytes = self._read_all(
                response,
                self._remaining_until(deadline, "JSON 响应超时"),
                "JSON 响应超时",
            )
        finally:
            response.close()
        raw = raw_bytes.decode("utf-8")
        payload = json.loads(raw)
        choices = payload.get("choices") or []
        choice = choices[0] if choices else {}
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str):
            # A few OpenAI-compatible providers return a completed response in
            # the streaming-shaped delta field even when stream=false.
            content = (choice.get("delta") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            message = "LLM 响应缺少 choices[0].message.content 或 choices[0].delta.content"
            response_message = choice.get("message")
            reasoning_only = isinstance(response_message, dict) and isinstance(response_message.get("reasoning_content"), str)
            if reasoning_only and choice.get("finish_reason") == "length":
                message += "；响应仅含 reasoning_content 且被长度截断，未产生正文。可尝试设置 STORY_LLM_REASONING_EFFORT=none，或改用不占用正文预算的模型。"
            error = LlmError(message, "empty_json")
            error.raw_response = raw
            raise error
        return content, raw

    def _stream(
        self,
        body: Dict[str, Any],
        on_delta: Optional[Callable[[str], None]],
        timeout_seconds: Optional[int] = None,
    ) -> tuple[str, str]:
        chunks: List[str] = []
        raw_events: List[str] = []
        total_timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        deadline = time.monotonic() + total_timeout
        first_delta_timeout = min(self._first_sse_delta_timeout_seconds(), total_timeout)
        response = self._request(
            body,
            min(first_delta_timeout, self._remaining_until(deadline, "SSE 连接或首段正文返回前超时")),
        )
        events: Queue[tuple[str, Any]] = Queue()

        def read_lines() -> None:
            try:
                for encoded in response:
                    events.put(("line", encoded))
                events.put(("done", None))
            except Exception as error:  # pragma: no cover - depends on socket implementation
                events.put(("error", error))

        # urllib's iterator can remain blocked after the HTTP handshake even
        # when the server has accepted stream=true. Read it off-thread so the
        # CLI owns the first-visible-delta deadline and can close the response.
        reader = Thread(target=read_lines, daemon=True)
        reader.start()
        first_delta_deadline = min(deadline, time.monotonic() + first_delta_timeout)
        close_asynchronously = False
        try:
            while True:
                wait_seconds = (deadline if chunks else first_delta_deadline) - time.monotonic()
                if wait_seconds <= 0:
                    message = "SSE 正文生成超时" if chunks else "SSE 在首段正文返回前超时"
                    raise socket.timeout(message)
                try:
                    kind, value = events.get(timeout=wait_seconds)
                except Empty as error:
                    message = "SSE 正文生成超时" if chunks else "SSE 在首段正文返回前超时"
                    raise socket.timeout(message) from error
                if kind == "error":
                    raise value
                if kind == "done":
                    break
                encoded = value
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
        except BaseException:
            # Some compatible servers leave the response iterator blocked even
            # after a caller times out. Closing that socket synchronously can
            # itself block for another network timeout, turning a 15-second
            # first-prose deadline into an apparently hung CLI turn.
            close_asynchronously = True
            raise
        finally:
            if close_asynchronously:
                Thread(target=response.close, daemon=True).start()
            else:
                response.close()
        content = "".join(chunks)
        if not content.strip():
            raise LlmError("LLM 响应缺少 choices[0].delta.content 或 choices[0].message.content", "empty_stream")
        return content, "\n".join(raw_events)

    def _read_all(self, response: Any, timeout_seconds: int, timeout_message: str) -> bytes:
        result: Queue[tuple[str, Any]] = Queue(maxsize=1)

        def read() -> None:
            try:
                result.put(("data", response.read()))
            except Exception as error:  # pragma: no cover - depends on socket implementation
                result.put(("error", error))

        Thread(target=read, daemon=True).start()
        try:
            kind, value = result.get(timeout=timeout_seconds)
        except Empty as error:
            response.close()
            raise socket.timeout(timeout_message) from error
        if kind == "error":
            raise value
        if not isinstance(value, bytes):
            raise ValueError("LLM 响应不是字节流")
        return value


def parse_json_content(content: str) -> Dict[str, Any]:
    normalized = content.strip()
    if "</think>" in normalized:
        normalized = normalized.rsplit("</think>", 1)[1].strip()
    elif "<think>" in normalized:
        raise LlmError("LLM 仅返回未结束的推理内容，缺少最终 JSON")
    if normalized.startswith("```"):
        first_newline = normalized.find("\n")
        normalized = normalized[first_newline + 1:] if first_newline >= 0 else normalized
        if normalized.rstrip().endswith("```"):
            normalized = normalized.rstrip()[:-3].rstrip()
    if normalized.endswith("[DONE]"):
        normalized = normalized[:-6].rstrip()
    if normalized.endswith("```"):
        normalized = normalized[:-3].rstrip()
    # Only a terminal object is eligible. An example in reasoning or a preface
    # must never substitute for a missing/malformed final response.
    opening = normalized.find("{")
    decoder = json.JSONDecoder()
    while opening >= 0:
        try:
            parsed, end = decoder.raw_decode(normalized[opening:])
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict) and not normalized[opening + end:].strip():
                return parsed
        opening = normalized.find("{", opening + 1)
    raise LlmError("LLM 未返回有效的最终 JSON 对象")
