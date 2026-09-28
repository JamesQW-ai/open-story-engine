"""Capture bounded production/experimental planner traces without committing."""

from __future__ import annotations

import argparse
import copy
import json
import os
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.llm import OpenAICompatibleGateway, writer_config_from_env
from test_support.context_prose_ab import (
    ROOT,
    _entry_samples,
    _load_env,
    _writer_prompt,
)


DEFAULT_OUTPUT = ROOT / "docs/evidence/context-management-2026-09-22/context-planner-smoke-2026-09-25-controls.json"


class RecordingGateway:
    def __init__(self, gateway: OpenAICompatibleGateway) -> None:
        self.gateway = gateway
        self.model = gateway.model
        self.calls: List[Dict[str, Any]] = []

    def complete_text(self, messages, *args, **kwargs):
        try:
            result = self.gateway.complete_text(messages, *args, **kwargs)
        except Exception as error:
            self.calls.append({
                "kind": "complete_text",
                "messages": messages,
                "error": str(error),
                "errorType": type(error).__name__,
                "rawResponse": getattr(error, "raw_response", None),
                "observations": getattr(error, "observations", []),
            })
            raise
        self.calls.append({
            "kind": "complete_text",
            "messages": messages,
            "rawResponse": result.raw_response,
            "content": result.content,
            "observations": result.observations,
        })
        return result

    def complete_json(self, messages, *args, **kwargs):
        try:
            result = self.gateway.complete_json(messages, *args, **kwargs)
        except Exception as error:
            self.calls.append({
                "kind": "complete_json",
                "messages": messages,
                "error": str(error),
                "errorType": type(error).__name__,
                "rawResponse": getattr(error, "raw_response", None),
                "observations": getattr(error, "observations", []),
            })
            raise
        self.calls.append({
            "kind": "complete_json",
            "messages": messages,
            "rawResponse": result.raw_response,
            "content": result.content,
            "observations": result.observations,
        })
        return result


def _gateway(config: Dict[str, str], limit: int) -> RecordingGateway:
    try:
        timeout = int(os.environ.get("STORY_LLM_TIMEOUT_SECONDS", "120"))
    except ValueError:
        timeout = 120
    try:
        max_tokens = int(os.environ.get("STORY_LLM_MAX_TOKENS", "8192"))
    except ValueError:
        max_tokens = 8192
    try:
        text_max_tokens = int(os.environ.get("STORY_LLM_TEXT_MAX_TOKENS", str(max_tokens)))
    except ValueError:
        text_max_tokens = max_tokens
    gateway = OpenAICompatibleGateway(
        config["base_url"], config["api_key"], config["model"], stream=False,
        timeout_seconds=timeout, max_tokens=max_tokens, text_max_tokens=text_max_tokens,
        allow_transport_fallback=False,
        reasoning_effort=os.environ.get("STORY_LLM_REASONING_EFFORT") or None,
    )
    gateway.remaining_calls = limit
    return RecordingGateway(gateway)


def _install_experimental_prompt(planner: PlayerNarrativePlanner, variant: str) -> None:
    if variant == "production":
        return
    if variant not in {"chapter", "full"}:
        raise ValueError("未知实验路径：" + variant)
    original = planner._prompt

    def experimental_prompt(self, context, selected, state, repair, terminal_source_branch=False):
        original(context, selected, state, repair, terminal_source_branch=terminal_source_branch)
        bundle = self.last_context_bundle
        if bundle is None:
            raise RuntimeError("smoke prompt 未生成 ContextBundle")
        payload = bundle.as_dict() if variant == "full" else bundle.project("chapter")
        return _writer_prompt(payload, context, selected, writer_controls={
            "resultContract": context.get("resultContract"),
            "pacing": self.last_prompt_context.get("pacing", {}),
            "repair": repair or "",
            "terminalSourceBranch": terminal_source_branch,
        })

    planner._prompt = types.MethodType(experimental_prompt, planner)


def _run_variant(sample: Dict[str, Any], config: Dict[str, str], variant: str) -> Dict[str, Any]:
    recorder = _gateway(config, 30)
    planner = PlayerNarrativePlanner(recorder, context_resolver=sample["resolver"])
    _install_experimental_prompt(planner, variant)
    started = time.monotonic()
    record: Dict[str, Any] = {
        "variant": variant,
        "status": "failed",
        "callLimit": 30,
        "maxTokens": recorder.gateway.max_tokens,
        "textMaxTokens": recorder.gateway.text_max_tokens,
    }
    try:
        context, selected, state = copy.deepcopy((sample["context"], sample["selected"], sample["state"]))
        result, audit = planner.plan(context, selected, state)
        record.update({
            "status": "completed",
            "result": result,
            "audit": audit,
        })
    except Exception as error:
        record.update({
            "error": str(error),
            "errorType": type(error).__name__,
            "failureStage": getattr(error, "failure_stage", None),
            "audit": getattr(error, "audit", None),
        })
    record["elapsedMs"] = round((time.monotonic() - started) * 1000)
    record["remainingCalls"] = recorder.gateway.remaining_calls
    record["calls"] = recorder.calls
    record["proseCalls"] = sum(call["kind"] == "complete_text" for call in recorder.calls)
    return record


def run(output: Path, *, variants=("production", "chapter", "full")) -> Dict[str, Any]:
    variants = tuple(variants)
    if (not variants or len(set(variants)) != len(variants)
            or any(variant not in {"production", "chapter", "full"} for variant in variants)):
        raise ValueError("诊断路径必须非空、不重复，且属于 production/chapter/full")
    # Reserve a new artifact before any paid call; existing evidence is immutable.
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps({"schemaVersion": "context-planner-smoke/0.2", "status": "incomplete"}) + "\n")
    config = writer_config_from_env()
    missing = [key for key in ("base_url", "api_key", "model") if not config.get(key)]
    if missing:
        result = {"schemaVersion": "context-planner-smoke/0.2", "status": "blocked_missing_model_configuration", "missing": missing}
    else:
        sample = _entry_samples(1)[0]
        result = {
            "schemaVersion": "context-planner-smoke/0.2",
            "recordedAt": datetime.now(timezone.utc).isoformat(),
            "status": "incomplete",
            "purpose": "所选路径各运行一次；仅诊断 Planner，不执行提交验收",
            "selectedVariants": list(variants),
            "comparisonLimit": "各路径独立生成计划，计划及修复问题可能不同；不能将成败差异归因为上下文投影",
            "fullArmPolicy": {"mode": "negative_control_only", "productionEnablement": "forbidden"},
            "execution": {"parallelReviews": False, "temporarySessionWrites": 0, "idempotencyChecked": False},
            "model": {"route": config["route"], "model": config["model"], "stream": False, "transportFallback": False},
            "sampleId": sample["sampleId"],
            "formalSessionWrites": 0,
            "jevCalls": 0,
            "variants": {},
            "acceptance": {
                "passed": False,
                "reason": "烟雾回放仅用于保存完整 planner 的失败证据，未形成质量验收",
            },
        }
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for variant in variants:
            result["variants"][variant] = _run_variant(sample, config, variant)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["status"] = "completed"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--variant", choices=("production", "chapter", "full"), action="append",
                        help="仅运行指定路径；可重复指定不同路径。未指定时保留三路径诊断。")
    args = parser.parse_args()
    _load_env(args.env_file)
    result = run(args.output, variants=args.variant if args.variant is not None else ("production", "chapter", "full"))
    print(json.dumps({
        "output": str(args.output),
        "status": result["status"],
        "variants": {
            key: {field: value for field, value in item.items() if field not in {"calls", "audit", "result"}}
            for key, item in result.get("variants", {}).items()
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
