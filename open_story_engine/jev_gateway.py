"""Optional gateway for the OpenRouter Decisions API.

The default mode is shadow-only. It sends a bounded review state, records a
typed result, and never writes story state or invents a fallback narrative.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from test_support.jev_review_rules import QUESTIONS, evaluate_answers, preflight_state


DEFAULT_BASE_URL = "https://openrouter.ai"
DEFAULT_MODEL = "~typesafe/jev-latest"
DEFAULT_TIMEOUT_SECONDS = 3
RUNTIME_REVIEW_MODES = {"off", "shadow", "block"}
STATE_FIELDS = (
    "context",
    "branchState",
    "playerAction",
    "resultContract",
    "allowedEvidence",
    "allowedMechanisms",
    "candidateNarrative",
    "patchHint",
    "reviewMeta",
)


@dataclass(frozen=True)
class JevGatewayConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    enabled: bool = True
    provider_sort: str = ""

    @classmethod
    def from_env(cls, *, runtime: bool = False) -> "JevGatewayConfig":
        try:
            timeout_name = "JEV_TIMEOUT_SECONDS" if runtime else "JEV_SHADOW_TIMEOUT_SECONDS"
            timeout = float(os.environ.get(timeout_name, str(DEFAULT_TIMEOUT_SECONDS)))
        except ValueError:
            timeout = DEFAULT_TIMEOUT_SECONDS
        provider_sort = os.environ.get("JEV_PROVIDER_SORT", "").strip().lower()
        if provider_sort not in {"", "latency", "throughput", "price"}:
            provider_sort = ""
        return cls(
            base_url=os.environ.get("JEV_OPENROUTER_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
            api_key=os.environ.get("JEV_API_KEY", os.environ.get("STORY_LLM_API_KEY", "")).strip(),
            model=os.environ.get("JEV_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            timeout_seconds=max(0.1, min(timeout, 30.0)),
            enabled=os.environ.get("JEV_SHADOW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
            provider_sort=provider_sort,
        )


def runtime_review_mode() -> str:
    """Return the explicit, reversible production-path experiment switch."""

    mode = os.environ.get("JEV_RUNTIME_REVIEW_MODE", "off").strip().lower() or "off"
    if mode not in RUNTIME_REVIEW_MODES:
        raise ValueError("JEV_RUNTIME_REVIEW_MODE 仅支持 off、shadow 或 block")
    return mode


def _bounded_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {key: state[key] for key in STATE_FIELDS if key in state}


def _validate_answers(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("response missing answers object")
    result: dict[str, dict[str, Any]] = {}
    for key, question in QUESTIONS.items():
        answer = answers.get(key)
        if not isinstance(answer, dict):
            raise ValueError(f"response missing typed answer: {key}")
        if key == "review_decision":
            if answer.get("choice") not in {"allow", "allow_with_patch", "rewrite", "reject"}:
                raise ValueError("review_decision.choice is outside the configured rule set")
        else:
            value = answer.get("noul")
            if not isinstance(value, (int, float)):
                raise ValueError(f"{key}.noul is not numeric")
        result[key] = answer
    return result


class JevShadowGateway:
    """Call Jev without retries and return an auditable, non-blocking result."""

    def __init__(self, config: JevGatewayConfig | None = None) -> None:
        self.config = config or JevGatewayConfig.from_env()

    def review(self, state: Mapping[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        bounded = _bounded_state(state)
        preflight = preflight_state(bounded)
        result: dict[str, Any] = {
            "shadow": True,
            "status": "error",
            "requestId": request_id,
            "model": self.config.model,
            "preflight": preflight,
            "inputFields": sorted(bounded),
        }
        if not self.config.enabled:
            return self._finish(result, started, "shadow_disabled", "JEV_SHADOW_ENABLED is false")
        if not preflight.get("ok"):
            result.update({
                "status": "preflight_rejected",
                "decision": "reject",
                "reason": preflight.get("reason", "preflight_failed"),
                "reasonCodes": [preflight.get("reason", "missing_required_state")],
            })
            return self._finish(result, started)
        if not self.config.api_key:
            return self._finish(result, started, "missing_api_key", "JEV_API_KEY or STORY_LLM_API_KEY is empty")
        body = {
            "model": self.config.model,
            "state": bounded,
            "questions": QUESTIONS,
        }
        if self.config.provider_sort:
            body["provider"] = {"sort": self.config.provider_sort}
        request = Request(
            self.config.base_url.rstrip("/") + "/api/alpha/decisions",
            # Keep the semantic payload lossless but remove JSON whitespace so
            # large context projections spend fewer bytes in transit.
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json",
                "HTTP-Referer": "https://openrouter.ai",
                "X-OpenRouter-Title": "Open Story Engine Jev Narrative Review",
                **({"X-Request-ID": request_id} if request_id else {}),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status != 200:
                    raise RuntimeError(f"unexpected HTTP status {response.status}")
            answers = _validate_answers(payload)
            review = evaluate_answers(answers, preflight=preflight)
            result.update({
                "status": "ok",
                "decision": review["decision"],
                "reason": review["reason"],
                "reasonCodes": review.get("reasonCodes", []),
                "review": review,
                "answers": answers,
                "resolvedModel": payload.get("model"),
                "provider": payload.get("provider"),
                "usage": payload.get("usage", {}),
            })
            return self._finish(result, started)
        except HTTPError as error:
            try:
                detail = error.read().decode("utf-8", "replace")[:500]
            except OSError:
                detail = "HTTP error body unavailable"
            return self._finish(result, started, "http_error", f"HTTP {error.code}: {detail}")
        except (URLError, TimeoutError, socket.timeout) as error:
            return self._finish(result, started, "transport_error", str(error))
        except (ValueError, RuntimeError) as error:
            return self._finish(result, started, "invalid_response", str(error))

    @staticmethod
    def _finish(result: dict[str, Any], started: float, error_kind: str | None = None, detail: str | None = None) -> dict[str, Any]:
        result["durationMs"] = round((time.perf_counter() - started) * 1000)
        if error_kind:
            result["errorKind"] = error_kind
            result["error"] = detail or error_kind
        return result


class JevRuntimeGateway(JevShadowGateway):
    """Explicit runtime experiment wrapper; it still never writes story state."""

    def review(self, state: Mapping[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        result = super().review(state, request_id=request_id)
        result["shadow"] = False
        result["runtimeExperiment"] = True
        return result
