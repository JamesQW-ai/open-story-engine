#!/usr/bin/env python3
"""Build Jev cases from independent beats in the official longform package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "content/packages/taixu-relics-part1/0.1.3"
DEFAULT_OUTPUT = ROOT / "test_support/fixtures/jev-real-beat-review.json"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _chapter_numbers() -> list[int]:
    numbers = []
    for path in (PACKAGE_ROOT / "modules/indexes/beats").glob("chapter-*.json"):
        try:
            numbers.append(int(path.stem.split("-")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(numbers)


def _first_beat(chapter_number: int) -> tuple[dict[str, Any], Path]:
    index_path = PACKAGE_ROOT / "modules/indexes/beats" / f"chapter-{chapter_number:03d}.json"
    index = _read(index_path)
    entries = index.get("beats") or index.get("items") or index.get("entries") or []
    if not entries:
        raise ValueError(f"no beats in {index_path}")
    relative = entries[0].get("path")
    if not relative:
        raise ValueError(f"beat path missing in {index_path}")
    beat_path = PACKAGE_ROOT / "modules" / relative
    # Some indexes point at indexes/beats/... while the module lives in beats/...
    if not beat_path.exists():
        beat_path = PACKAGE_ROOT / "modules" / relative.replace("indexes/", "")
    return _read(beat_path), beat_path


def _evidence(beat: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    payload = beat.get("beat") or {}
    brief = payload.get("narrativeBrief") or []
    lines: list[str] = []
    evidence_ids: list[str] = []
    line_ranges: list[str] = []
    for item in brief[:4]:
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(text)
        if item.get("evidenceParagraphId"):
            evidence_ids.append(str(item["evidenceParagraphId"]))
        if item.get("lineRange"):
            line_ranges.append(json.dumps(item["lineRange"], ensure_ascii=False, sort_keys=True))
    if not lines and payload.get("summary"):
        lines.append(str(payload["summary"]))
    if not lines:
        raise ValueError(f"beat has no evidence: {payload.get('id')}")
    return "\n".join(lines), evidence_ids, line_ranges


def _base_case(beat: dict[str, Any], beat_path: Path, chapter_number: int) -> dict[str, Any]:
    payload = beat["beat"]
    evidence_text, evidence_ids, line_ranges = _evidence(beat)
    beat_id = str(payload["id"])
    source = {
        "packageId": "taixu-relics-part1",
        "packageVersion": "0.1.3",
        "chapterId": f"chapter-{chapter_number:03d}",
        "chapterTitle": beat.get("chapter", {}).get("title"),
        "beatId": beat_id,
        "path": str(beat_path.relative_to(ROOT)),
        "evidenceParagraphIds": evidence_ids,
        "lineRanges": line_ranges,
    }
    context = (
        f"官方长篇《太虚遗录》当前 beat。章节：{source['chapterTitle']}。"
        f"本 beat 的冻结证据如下，只允许复述或保持疑问：\n{evidence_text}"
    )
    state = (
        "package=taixu-relics-part1@0.1.3; "
        f"chapter={source['chapterId']}; beat={beat_id}; "
        f"authoritative_evidence={','.join(evidence_ids) or 'summary'}; "
        "future_facts=hidden; sibling_branch_facts=hidden; movement_authorized=false"
    )
    candidate = evidence_text + "\n你只记录这些已出现的事实，未替任何角色补充未知原因。"
    return {
        "id": f"realbeat_{chapter_number:03d}_{beat_id}",
        "expectedDecision": "allow",
        "variant": "real_beat_allow",
        "realSource": source,
        "context": context,
        "branchState": state,
        "playerAction": "我复核当前 beat 的已公开内容，暂不移动或改变状态。",
        "resultContract": "只复述冻结证据，保留未知，不移动、不战斗、不新增人物或因果。",
        "allowedEvidence": evidence_text,
        "allowedMechanisms": [],
        "reviewMeta": {"authoritativeReplay": True},
        "candidateNarrative": candidate,
    }


def _variants(base: dict[str, Any], future_text: str) -> list[dict[str, Any]]:
    source_id = base["id"]
    variants: list[dict[str, Any]] = []

    unsupported = dict(base)
    unsupported.update({
        "id": source_id + "__unsupported_claim",
        "expectedDecision": "rewrite",
        "variant": "real_beat_unsupported_claim",
        "candidateNarrative": base["candidateNarrative"] + "\n因此幕后主使已经确定为证据中没有出现的黑塔祭司。",
        "expectedReasonCodes": ["unsupported_claim"],
    })
    variants.append(unsupported)

    future = dict(base)
    future.update({
        "id": source_id + "__future_leak",
        "expectedDecision": "rewrite",
        "variant": "real_beat_future_leak",
        "branchState": base["branchState"] + "; future_reveal=unpublished",
        "candidateNarrative": base["candidateNarrative"] + f"\n后续尚未公开的 beat 已确认：{future_text}",
        "expectedReasonCodes": ["future_fact_leak"],
    })
    variants.append(future)

    missing_contract = dict(base)
    missing_contract.update({
        "id": source_id + "__missing_contract",
        "expectedDecision": "reject",
        "variant": "real_beat_missing_contract",
        "resultContract": "",
        "reviewMeta": {"contractPresent": False},
        "expectedReasonCodes": ["missing_result_contract"],
    })
    variants.append(missing_contract)

    empty = dict(base)
    empty.update({
        "id": source_id + "__empty_candidate",
        "expectedDecision": "reject",
        "variant": "real_beat_empty_candidate",
        "candidateNarrative": "",
        "reviewMeta": {"evidenceSufficient": False},
        "expectedReasonCodes": ["insufficient_evidence"],
    })
    variants.append(empty)
    return variants


def build(limit: int = 40) -> dict[str, Any]:
    chapters = _chapter_numbers()
    if len(chapters) < limit:
        raise ValueError(f"need {limit} chapters, found {len(chapters)}")
    cases: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    for number in chapters[:limit]:
        beat, path = _first_beat(number)
        base = _base_case(beat, path, number)
        selected.append(base)
        cases.append(base)
    future_text = selected[-1]["candidateNarrative"].split("\n", 1)[0]
    for base in selected:
        cases.extend(_variants(base, future_text))
    return {
        "schemaVersion": "jev-real-beat-review-fixture/0.2",
        "package": {"id": "taixu-relics-part1", "version": "0.1.3", "sourceCjk": 102610},
        "description": "从官方长篇不同章节冻结 beat 证据生成的 Jev 审核样本；边界变体不代表原著事实。",
        "baseBeatCount": len(selected),
        "variantCount": len(cases) - len(selected),
        "caseCount": len(cases),
        "coverage": {
            "base": {"real_beat_allow": len(selected)},
            "variants": {
                "real_beat_unsupported_claim": limit,
                "real_beat_future_leak": limit,
                "real_beat_missing_contract": limit,
                "real_beat_empty_candidate": limit,
            },
        },
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = build(args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("output", "baseBeatCount", "variantCount", "caseCount")} if "output" in payload else {
        "output": str(args.output),
        "baseBeatCount": payload["baseBeatCount"],
        "variantCount": payload["variantCount"],
        "caseCount": payload["caseCount"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
