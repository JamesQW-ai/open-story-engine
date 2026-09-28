#!/usr/bin/env python3
"""Build a traceable phase-only latency report for the Jev review experiment.

The reviewer probe and the project's prose generation are currently separate
executions.  This report deliberately labels their serialized sum as an
estimate; it is not an integrated runtime measurement.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GENERATION_1 = ROOT / "docs/evidence/context-management-2026-09-22/real-rerun-summary-2026-09-22.json"
DEFAULT_GENERATION_2 = ROOT / "docs/evidence/context-management-2026-09-22/real-rerun2-summary-2026-09-22.json"
DEFAULT_REVIEW = ROOT / "docs/evidence/jev-narrative-review-2026-09-22/live-real-longform-report.json"
DEFAULT_OUTPUT = ROOT / "docs/evidence/jev-narrative-review-2026-09-22/full-turn-phase-report.json"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _metrics(scene: dict[str, Any]) -> dict[str, Any]:
    metrics = scene.get("metrics") or {}
    complete = metrics.get("selection_to_complete_ms")
    if complete is None:
        complete = metrics.get("complete_ms")
    if complete is None and scene.get("elapsedSeconds") is not None:
        complete = round(float(scene["elapsedSeconds"]) * 1000)
    return {
        "status": scene.get("status"),
        "actualCjk": scene.get("actualCjk", 0),
        "firstTextMs": scene.get("firstTextMs", metrics.get("first_text_ms")),
        "completeMs": complete,
        "selectionToCompleteMs": metrics.get("selection_to_complete_ms"),
        "commitMs": metrics.get("commit_ms"),
        "sourceElapsedMs": round(float(scene["elapsedSeconds"]) * 1000)
        if scene.get("elapsedSeconds") is not None
        else None,
        "branchId": scene.get("branchId"),
    }


def _find_review(records: list[dict[str, Any]], record_id: str) -> dict[str, Any]:
    for record in records:
        if record.get("id") == record_id:
            return record
    raise KeyError(record_id)


def build(generation_1: dict[str, Any], generation_2: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    first_scenes = {item["id"]: item for item in generation_1.get("scenes", [])}
    second_runs = {
        item.get("source", ""): item["scene"]
        for item in generation_2.get("runs", [])
        if "scene" in item
    }

    # Prefer the latest successful reruns.  ask_basis has no later follow-up,
    # so its original complete/commit metrics remain the source of truth.
    generation_sources = {
        "taixu_inform_rerun2": next(scene for source, scene in second_runs.items() if source.endswith("reruns2-20260922/inform.json")),
        "taixu_ask_basis_rerun1": first_scenes["ask_basis"],
        "taixu_brief_wait_rerun3": next(scene for source, scene in second_runs.items() if source.endswith("reruns3-20260922/brief_wait.json")),
    }
    failed_source = next(
        item["scene"] for item in generation_2.get("runs", [])
        if item.get("source", "").endswith("reruns2-20260922/brief_wait.json")
    )
    generation_sources["taixu_empty_failed_draft"] = failed_source

    records: list[dict[str, Any]] = []
    for record in review.get("records", []):
        record_id = record["id"]
        generation = _metrics(generation_sources[record_id])
        review_ms = record.get("durationMs")
        complete_ms = generation.get("completeMs")
        serialized_sum = complete_ms + review_ms if complete_ms is not None and review_ms is not None else None
        records.append({
            "id": record_id,
            "expectedDecision": record.get("expectedDecision"),
            "jevDecision": record.get("derivedDecision"),
            "generation": generation,
            "jevReview": {
                "durationMs": review_ms,
                "status": record.get("status"),
                "preflight": record.get("preflight"),
                "model": record.get("model"),
            },
            "serializedPhaseSumEstimateMs": serialized_sum,
            "measurementNote": "生成与 Jev 审核来自独立回放；此字段仅为顺序相加估计，不是已接入运行时端到端耗时。",
        })

    generation_values = [r["generation"]["completeMs"] for r in records if r["generation"]["status"] == "written" and r["generation"]["completeMs"] is not None]
    review_values = [r["jevReview"]["durationMs"] for r in records if r["jevReview"]["durationMs"] is not None]
    sum_values = [r["serializedPhaseSumEstimateMs"] for r in records if r["serializedPhaseSumEstimateMs"] is not None]

    def stats(values: list[float]) -> dict[str, Any]:
        return {
            "count": len(values),
            "p50Ms": _percentile(values, 0.50),
            "p95Ms": _percentile(values, 0.95),
            "maxMs": max(values) if values else None,
            "meanMs": round(statistics.mean(values)) if values else None,
        }

    return {
        "schemaVersion": "jev-full-turn-phase-report/0.1",
        "status": "phase_measurement_only",
        "integratedRuntime": False,
        "generatedAt": "2026-09-22",
        "package": {"id": "taixu-relics-part1", "version": "0.1.3", "cjk": 102610},
        "reviewModel": review.get("resolvedModels", [review.get("configuredModel")]),
        "records": records,
        "summary": {
            "successfulGenerationBeats": stats(generation_values),
            "jevReviewRequests": stats(review_values),
            "serializedPhaseSumEstimates": stats(sum_values),
            "successfulGenerationBeatCount": len(generation_values),
            "failedGenerationBeatCount": sum(1 for r in records if r["generation"]["status"] != "written"),
            "reviewDecisionAccuracy": review.get("quality", {}).get("derivedDecisionAccuracy"),
        },
        "limitations": [
            "Jev 当前仅作为独立审核探针运行，尚未接入项目生成提交链，因此没有可宣称的端到端完整回合耗时。",
            "四条真实候选中三条为成功生成 beat，一条为空失败草稿；它们覆盖当前包但不能替代大量连续真实 beat。",
            "ask_basis 使用已有真实回放的 complete/commit 指标，其余成功 beat 使用后续复核指标；不同回放批次不可用于严格单次请求比较。",
            "serializedPhaseSumEstimateMs 只是生成完成后再调用审核的顺序相加估计，未计入真实适配器、排队、序列化和重试开销。",
        ],
        "sources": {
            "generationRerun1": str(DEFAULT_GENERATION_1),
            "generationRerun2": str(DEFAULT_GENERATION_2),
            "jevReview": str(DEFAULT_REVIEW),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation-rerun1", type=Path, default=DEFAULT_GENERATION_1)
    parser.add_argument("--generation-rerun2", type=Path, default=DEFAULT_GENERATION_2)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build(_read(args.generation_rerun1), _read(args.generation_rerun2), _read(args.review))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
