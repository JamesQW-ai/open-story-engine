#!/usr/bin/env python3
"""Replay saved real longform candidates through the optional Jev shadow gateway."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from open_story_engine.environment import load_env_file
from open_story_engine.jev_gateway import JevGatewayConfig, JevShadowGateway


FIXTURE = ROOT / "test_support/fixtures/jev-real-longform-review.json"
PHASE_REPORT = ROOT / "docs/evidence/jev-narrative-review-2026-09-22/full-turn-phase-report.json"
DEFAULT_OUTPUT = ROOT / "docs/evidence/jev-narrative-review-2026-09-22/shadow-replay-report.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--phase-report", type=Path, default=PHASE_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    load_env_file(ROOT / ".env")
    # This command is the explicit experiment switch; the project default
    # remains disabled and no planner imports this gateway.
    config = replace(JevGatewayConfig.from_env(), enabled=True)
    gateway = JevShadowGateway(config)
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    phase = json.loads(args.phase_report.read_text(encoding="utf-8"))
    generation_by_id = {record["id"]: record["generation"] for record in phase["records"]}
    records = []
    for case in fixture["cases"]:
        state = {key: case[key] for key in (
            "context", "branchState", "playerAction", "resultContract",
            "allowedMechanisms", "candidateNarrative",
        ) if key in case}
        for optional in ("allowedEvidence", "patchHint", "reviewMeta"):
            if optional in case:
                state[optional] = case[optional]
        started = time.perf_counter()
        result = gateway.review(state, request_id=f"shadow-{case['id']}")
        wall_ms = round((time.perf_counter() - started) * 1000)
        records.append({
            "id": case["id"],
            "expectedDecision": case["expectedDecision"],
            "gateway": result,
            "wallMs": wall_ms,
            "generation": generation_by_id.get(case["id"]),
        })

    successful = [record for record in records if record["gateway"].get("status") == "ok"]
    preflight = [record for record in records if record["gateway"].get("status") == "preflight_rejected"]
    latencies = [record["gateway"]["durationMs"] for record in successful]
    report = {
        "schemaVersion": "jev-shadow-replay-report/0.1",
        "status": "shadow_only",
        "integratedRuntime": False,
        "model": config.model,
        "endpoint": config.base_url.rstrip("/") + "/api/alpha/decisions",
        "package": {"id": "taixu-relics-part1", "version": "0.1.3", "cjk": 102610},
        "caseCount": len(records),
        "providerReviewCount": len(successful),
        "preflightRejectedCount": len(preflight),
        "quality": {
            "derivedCorrect": sum(record["gateway"].get("decision") == record["expectedDecision"] for record in records),
            "derivedAccuracy": sum(record["gateway"].get("decision") == record["expectedDecision"] for record in records) / len(records) if records else None,
        },
        "latencyMs": {
            "count": len(latencies),
            "p50": sorted(latencies)[round((len(latencies) - 1) * 0.5)] if latencies else None,
            "p95": sorted(latencies)[round((len(latencies) - 1) * 0.95)] if latencies else None,
            "max": max(latencies) if latencies else None,
            "mean": round(statistics.mean(latencies)) if latencies else None,
        },
        "records": records,
        "limitations": [
            "这是显式 shadow replay，不改变 StoryPackage、BranchState 或提交路径。",
            "generation 字段来自已保存的真实生成证据；Jev 调用在本次 replay 中独立发生，不能宣称端到端完整回合耗时。",
            "默认 JEV_SHADOW_ENABLED=false；本命令仅为试验临时启用 gateway。",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("caseCount", "providerReviewCount", "preflightRejectedCount", "quality", "latencyMs")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
