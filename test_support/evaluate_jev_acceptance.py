#!/usr/bin/env python3
"""Evaluate the Jev experiment against explicit shadow and integration gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence/jev-narrative-review-2026-09-22"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(real: dict[str, Any], repeat: dict[str, Any], shadow: dict[str, Any], phase: dict[str, Any]) -> dict[str, Any]:
    by_expected = real.get("quality", {}).get("byExpectedDecision", {})
    class_gates = {
        name: {
            "passed": bucket.get("accuracy", 0) >= 0.90,
            "accuracy": bucket.get("accuracy"),
            "count": bucket.get("count"),
        }
        for name, bucket in by_expected.items()
    }
    stable = repeat.get("stability", {})
    stable_total = stable.get("caseCount", 0)
    stable_count = stable.get("stableCaseCount", 0)
    stability_rate = stable_count / stable_total if stable_total else 0.0
    gates = {
        "real_beat_derived_accuracy": {
            "passed": real.get("quality", {}).get("derivedDecisionAccuracy", 0) >= 0.90,
            "value": real.get("quality", {}).get("derivedDecisionAccuracy"),
            "threshold": 0.90,
        },
        "per_decision_class_accuracy": {
            "passed": bool(class_gates) and all(item["passed"] for item in class_gates.values()),
            "classes": class_gates,
            "threshold": 0.90,
        },
        "repeat_stability": {
            "passed": stability_rate >= 0.98,
            "stableCaseCount": stable_count,
            "caseCount": stable_total,
            "rate": stability_rate,
            "threshold": 0.98,
        },
        "shadow_review_p95": {
            "passed": (shadow.get("latencyMs", {}).get("p95") or float("inf")) <= 2000,
            "valueMs": shadow.get("latencyMs", {}).get("p95"),
            "thresholdMs": 2000,
        },
        "integrated_runtime": {
            "passed": phase.get("integratedRuntime") is True,
            "value": phase.get("integratedRuntime"),
            "threshold": True,
        },
    }
    blockers = [name for name, gate in gates.items() if not gate["passed"]]
    return {
        "schemaVersion": "jev-acceptance-report/0.1",
        "status": "ready_for_shadow_only" if not blockers or blockers == ["integrated_runtime"] else "not_ready",
        "recommendedIntegration": "shadow_only" if blockers else "controlled_review_gate",
        "package": {"id": "taixu-relics-part1", "version": "0.1.3", "cjk": 102610},
        "directJevAccuracyInformational": real.get("quality", {}).get("directDecisionAccuracy"),
        "gates": gates,
        "blockers": blockers,
        "decision": {
            "allowDefaultPlannerImport": False,
            "allowStateWriteFromJev": False,
            "reason": "完整回合仍未在同一运行时中串联生成、Jev 审核和提交；当前只能确认 shadow 阶段。" if "integrated_runtime" in blockers else "所有门槛通过。",
        },
        "sources": {
            "realBeat": "live-real-beat-report-v2.json",
            "repeat": "live-real-beat-repeat2-report.json",
            "shadow": "shadow-replay-report.json",
            "phase": "full-turn-phase-report.json",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", type=Path, default=EVIDENCE / "live-real-beat-report-v2.json")
    parser.add_argument("--repeat", type=Path, default=EVIDENCE / "live-real-beat-repeat2-report.json")
    parser.add_argument("--shadow", type=Path, default=EVIDENCE / "shadow-replay-report.json")
    parser.add_argument("--phase", type=Path, default=EVIDENCE / "full-turn-phase-report.json")
    parser.add_argument("--output", type=Path, default=EVIDENCE / "acceptance-report.json")
    args = parser.parse_args()
    report = evaluate(_read(args.real), _read(args.repeat), _read(args.shadow), _read(args.phase))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "recommendedIntegration": report["recommendedIntegration"], "blockers": report["blockers"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
