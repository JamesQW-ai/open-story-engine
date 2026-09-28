"""Run the reusable Jev narrative-review fixture through OpenRouter Decisions API.

This probe is deliberately separate from the story writer gateway. Jev receives
one bounded review state and several typed questions; it never writes or
persists story state. The report records only typed answers and measurements.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from open_story_engine.environment import load_env_file
from test_support.jev_review_rules import QUESTIONS, RULE_SET_VERSION, evaluate_answers, preflight_state


FIXTURE = ROOT / "test_support" / "fixtures" / "jev-narrative-review.json"
DEFAULT_OUTPUT = ROOT / "docs" / "evidence" / "jev-narrative-review-2026-09-22" / "live-report.json"


def _percentile(values: List[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1))))
    return ordered[index]


def _noul_probability(answer: Dict[str, Any]) -> float | None:
    value = answer.get("noul")
    return float(value) if isinstance(value, (int, float)) else None


def _direct_decision(answer: Dict[str, Any]) -> str | None:
    value = answer.get("choice")
    return value if isinstance(value, str) else None


def _derived_decision(answers: Dict[str, Dict[str, Any]]) -> str:
    return evaluate_answers(answers)["decision"]


def _input_sizes(state: Dict[str, Any]) -> Dict[str, int]:
    fields = ("context", "branchState", "playerAction", "resultContract", "candidateNarrative")
    sizes = {field: len(str(state.get(field, ""))) for field in fields}
    sizes["total"] = sum(sizes.values())
    return sizes


def _request(base_url: str, model: str, api_key: str, state: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    body = {"model": model, "state": state, "questions": QUESTIONS}
    request = Request(
        base_url.rstrip("/") + "/api/alpha/decisions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://openrouter.ai",
            "X-OpenRouter-Title": "Open Story Engine Jev Narrative Review",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status}")
        return payload


def _validate_answers(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("response missing answers object")
    missing = [key for key in QUESTIONS if not isinstance(answers.get(key), dict)]
    if missing:
        raise ValueError("response missing typed answers: " + ",".join(missing))
    if _direct_decision(answers["review_decision"]) not in {"allow", "allow_with_patch", "rewrite", "reject"}:
        raise ValueError("review_decision.choice is outside the configured rule set")
    for key in QUESTIONS:
        answer = answers[key]
        if answer.get("type") == "noul" and _noul_probability(answer) is None:
            raise ValueError(f"{key}.noul is not numeric")
    return {key: answers[key] for key in QUESTIONS}


def _load_cases(path: Path, case_ids: Iterable[str] | None, limit: int | None) -> List[Dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    selected = [case for case in cases if not case_ids or case["id"] in case_ids]
    if case_ids:
        found = {case["id"] for case in selected}
        missing = sorted(set(case_ids) - found)
        if missing:
            raise SystemExit("unknown case id: " + ",".join(missing))
    return selected[:limit] if limit else selected


def run(cases: List[Dict[str, Any]], output: Path, repeat: int = 1) -> Dict[str, Any]:
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    load_env_file(ROOT / ".env")
    api_key = os.environ.get("JEV_API_KEY", os.environ.get("STORY_LLM_API_KEY", "")).strip()
    if not api_key:
        raise SystemExit("JEV_API_KEY or STORY_LLM_API_KEY is empty")
    base_url = os.environ.get("JEV_OPENROUTER_BASE_URL", "https://openrouter.ai").strip()
    model = os.environ.get("JEV_MODEL", "~typesafe/jev-latest").strip()
    timeout = int(os.environ.get("JEV_TIMEOUT_SECONDS", "30"))
    records: List[Dict[str, Any]] = []
    for case in cases:
        for attempt in range(1, repeat + 1):
            state = {key: case[key] for key in ("context", "branchState", "playerAction", "resultContract", "allowedMechanisms", "candidateNarrative")}
            if case.get("patchHint"):
                state["patchHint"] = case["patchHint"]
            if case.get("reviewMeta"):
                state["reviewMeta"] = case["reviewMeta"]
            preflight = preflight_state(state)
            input_sizes = _input_sizes(state)
            started = time.perf_counter()
            try:
                payload = _request(base_url, model, api_key, state, timeout)
                answers = _validate_answers(payload)
                duration_ms = round((time.perf_counter() - started) * 1000)
                record = {
                    "id": case["id"],
                    "recordKey": f"{case['id']}#r{attempt}" if repeat > 1 else case["id"],
                    "attempt": attempt,
                    "expectedDecision": case["expectedDecision"],
                    "variant": case.get("variant"),
                    "expectedReasonCodes": case.get("expectedReasonCodes", []),
                    "realSource": case.get("realSource"),
                    "directDecision": _direct_decision(answers["review_decision"]),
                    "derivedDecision": evaluate_answers(answers, preflight=preflight)["decision"],
                    "preflight": preflight,
                    "inputChars": input_sizes,
                    "review": evaluate_answers(answers, preflight=preflight),
                    "answers": answers,
                    "durationMs": duration_ms,
                    "model": payload.get("model"),
                    "usage": payload.get("usage", {}),
                    "provider": payload.get("provider"),
                    "status": "ok",
                }
                records.append(record)
            except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError) as error:
                duration_ms = round((time.perf_counter() - started) * 1000)
                status = getattr(error, "code", None)
                detail = ""
                if isinstance(error, HTTPError):
                    detail = error.read().decode("utf-8", "replace")[:1000]
                records.append({"id": case["id"], "recordKey": f"{case['id']}#r{attempt}" if repeat > 1 else case["id"], "attempt": attempt, "expectedDecision": case["expectedDecision"], "variant": case.get("variant"), "expectedReasonCodes": case.get("expectedReasonCodes", []), "realSource": case.get("realSource"), "status": "error", "httpStatus": status, "error": str(error), "detail": detail, "inputChars": input_sizes, "durationMs": duration_ms})

    successful = [record for record in records if record["status"] == "ok"]
    latencies = [record["durationMs"] for record in successful]
    direct_correct = sum(record["directDecision"] == record["expectedDecision"] for record in successful)
    derived_correct = sum(record["derivedDecision"] == record["expectedDecision"] for record in successful)
    confusion: Dict[str, Dict[str, int]] = {}
    by_expected: Dict[str, Dict[str, Any]] = {}
    for record in successful:
        expected = record["expectedDecision"]
        predicted = record["derivedDecision"]
        confusion.setdefault(expected, {})[predicted] = confusion.setdefault(expected, {}).get(predicted, 0) + 1
        bucket = by_expected.setdefault(expected, {"count": 0, "correct": 0, "accuracy": None})
        bucket["count"] += 1
        bucket["correct"] += int(predicted == expected)
    for bucket in by_expected.values():
        bucket["accuracy"] = bucket["correct"] / bucket["count"] if bucket["count"] else None
    reason_code_counts: Dict[str, int] = {}
    reason_code_correct: Dict[str, int] = {}
    for record in successful:
        for code in record.get("review", {}).get("reasonCodes", []):
            reason_code_counts[code] = reason_code_counts.get(code, 0) + 1
        expected_codes = set(record.get("expectedReasonCodes", []))
        if expected_codes and expected_codes.intersection(record.get("review", {}).get("reasonCodes", [])):
            for code in expected_codes:
                reason_code_correct[code] = reason_code_correct.get(code, 0) + 1
    stability: Dict[str, List[str]] = {}
    for record in successful:
        stability.setdefault(record["id"], []).append(record["derivedDecision"])
    stability_summary = {
        "caseCount": len(stability),
        "stableCaseCount": sum(len(set(decisions)) == 1 for decisions in stability.values()),
        "unstableCaseCount": sum(len(set(decisions)) > 1 for decisions in stability.values()),
        "decisionsByCase": stability,
    }
    input_fields = ("context", "branchState", "playerAction", "resultContract", "candidateNarrative", "total")
    input_sizes = {
        field: {
            "min": min(record["inputChars"][field] for record in records if "inputChars" in record),
            "max": max(record["inputChars"][field] for record in records if "inputChars" in record),
            "mean": round(statistics.mean(record["inputChars"][field] for record in records if "inputChars" in record)),
        }
        for field in input_fields
    }
    report = {
        "schemaVersion": "jev-narrative-review-report/0.1",
        "ruleSetVersion": RULE_SET_VERSION,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": base_url.rstrip("/") + "/api/alpha/decisions",
        "configuredModel": model,
        "repeat": repeat,
        "resolvedModels": sorted({record.get("model") for record in successful if record.get("model")}),
        "questionIds": list(QUESTIONS),
        "caseCount": len(cases),
        "successCount": len(successful),
        "errorCount": len(records) - len(successful),
        "quality": {
            "directDecisionAccuracy": direct_correct / len(successful) if successful else None,
            "derivedDecisionAccuracy": derived_correct / len(successful) if successful else None,
            "directCorrect": direct_correct,
            "derivedCorrect": derived_correct,
            "expectedLabels": {record["recordKey"]: record["expectedDecision"] for record in records},
            "derivedMismatchIds": [
                record["recordKey"] for record in successful if record["derivedDecision"] != record["expectedDecision"]
            ],
            "derivedConfusionMatrix": confusion,
            "byExpectedDecision": by_expected,
            "reasonCodeCounts": reason_code_counts,
            "expectedReasonCodeMatches": reason_code_correct,
            "preflightRejected": sum(
                record.get("preflight", {}).get("ok") is False for record in successful
            ),
        },
        "latencyMs": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "mean": round(statistics.mean(latencies)) if latencies else None,
        },
        "inputChars": input_sizes,
        "stability": stability_summary,
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    cases = _load_cases(args.fixture, args.case_id, args.limit)
    report = run(cases, args.output, repeat=args.repeat)
    print(json.dumps({
        "output": str(args.output),
        "configuredModel": report["configuredModel"],
        "resolvedModels": report["resolvedModels"],
        "successCount": report["successCount"],
        "errorCount": report["errorCount"],
        "quality": report["quality"],
        "latencyMs": report["latencyMs"],
    }, ensure_ascii=False, indent=2))
    if report["errorCount"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
