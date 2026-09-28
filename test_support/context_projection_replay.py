"""Replay ContextBundle full vs chapter projection on official longform entries.

This is a deterministic, model-free P2 measurement.  It measures context
assembly and projection boundaries only; it must not be interpreted as prose
quality or live-model acceptance.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from test_support.longform import longform_cases
from open_story_engine.context_budget import estimate_text_tokens
from open_story_engine.context_bundle import ContextBundleBuilder
from open_story_engine.content import expand_state_visibility, load_runtime_story_package
from open_story_engine.cocreation import apply_branch_patch, create_contract, entry_node
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.storage import SessionStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs/evidence/context-management-2026-09-22/context-projection-replay-2026-09-24.json"


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _percentile(values: Iterable[int], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _metrics(value: Mapping[str, Any]) -> Dict[str, Any]:
    encoded = _encoded(value)
    return {
        "serializedChars": len(encoded),
        "estimatedTokens": estimate_text_tokens(encoded),
    }


def _projection_boundary_violations(projection: Mapping[str, Any], branch_id: str) -> List[str]:
    """Detect forbidden context shapes in a player-stage projection."""
    violations: List[str] = []
    forbidden_keys = {
        "branchLedger", "threadLedger", "characterOutcomeStates",
        "itemOwnerCharacterIds", "itemLocationIds", "provenance",
    }

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in forbidden_keys:
                    violations.append(path + "/" + key)
                if key == "visibility" and child not in {"player_known", "public_world_fact"}:
                    violations.append(path + "/visibility=" + str(child))
                if key == "branchId" and child not in {branch_id, "package", "global"}:
                    violations.append(path + "/branchId=" + str(child))
                walk(child, path + "/" + str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + "/" + str(index))

    walk(projection, "")
    return sorted(set(violations))


def _build_samples() -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    for case in longform_cases():
        package = load_runtime_story_package(case["path"], lazy=True)
        resolver = ModuleContextResolver.for_package(case["path"], package)
        if resolver is None:
            raise RuntimeError("官方长篇缺少 ModuleContextResolver: " + str(case["package_id"]))
        entry_points = list(package["story"]["entryModel"]["entryPoints"])
        for entry_point in entry_points:
            character_ids = list(entry_point.get("sourceCharacterIds") or [])
            if len(character_ids) != 1:
                raise RuntimeError("固定回放要求每个入口绑定一个 source character")
            character_id = character_ids[0]
            session_id = "context-projection-replay-20260924"
            contract = create_contract(
                package,
                session_id,
                {"kind": "source_character", "sourceCharacterId": character_id},
            )
            parent = entry_node(package, contract)
            root_state = parent["branchState"]
            official_actions = list(entry_point.get("openingActions") or [])
            actions = [
                {
                    "source": "official_opening_action",
                    "id": f"{entry_point['id']}-official-{index + 1}",
                    "title": action["title"],
                    "summary": action["summary"],
                }
                for index, action in enumerate(official_actions)
            ]
            actions.append({
                "source": "fixed_hold_action",
                "id": f"{entry_point['id']}-hold",
                "title": "留在原地观察",
                "summary": "留在当前位置，只整理眼前已公开证据，暂不转场或替玩家作决定。",
            })
            def append_variant(
                action: Mapping[str, Any],
                *,
                current_parent: Mapping[str, Any],
                lineage: List[Mapping[str, Any]],
                player_direction: str,
                turn_variant: str,
                state_transition: str,
            ) -> None:
                selected = {
                    "id": action["id"] + ("-turn2" if turn_variant != "entry" else ""),
                    "title": action["title"],
                    "summary": action["summary"],
                    "statePatch": {"playerLocationId": root_state["playerLocationId"]},
                }
                state = apply_branch_patch(
                    package,
                    current_parent["branchState"],
                    selected["statePatch"],
                    current_parent["sourceNodeRef"],
                )
                context = {
                    "package": package,
                    "contract": contract,
                    "parent": current_parent,
                    "lineage": lineage,
                    "playerDirection": player_direction,
                    "characterDetails": [],
                    "validatedStatePatch": selected["statePatch"],
                    "validatedStatePatchSource": "apply_branch_patch",
                }
                branch = {
                    "sessionId": session_id,
                    "parentBranchId": current_parent["id"],
                    "lineageHead": current_parent["id"],
                }
                visibility = expand_state_visibility(
                    package.get("stateVisibility"), state, package,
                )
                bundle = ContextBundleBuilder(resolver).build(
                    context=context,
                    selected=selected,
                    state=state,
                    branch=branch,
                    state_visibility=visibility,
                    module_context=resolver.resolve(context, selected, state),
                    validated_state_patch=selected["statePatch"],
                    state_visibility_source="explicit",
                    state_visibility_mode="formal_required",
                )
                full = bundle.as_dict()
                chapter = bundle.project("chapter")
                boundary_violations = _projection_boundary_violations(
                    chapter, branch["parentBranchId"],
                )
                required = {
                    "hardConstraints.currentBeat": isinstance(
                        chapter.get("hardConstraints", {}).get("currentBeat"), dict,
                    ),
                    "hardConstraints.actionContract": isinstance(
                        chapter.get("hardConstraints", {}).get("actionContract"), dict,
                    ),
                    "turnIntent.rawInput": bool(chapter.get("turnIntent", {}).get("rawInput")),
                    "turnIntent.selectedDirection": isinstance(
                        chapter.get("turnIntent", {}).get("selectedDirection"), dict,
                    ),
                    "outputContract": isinstance(chapter.get("outputContract"), dict),
                    "contextSha256": chapter.get("contextSha256") == full.get("contextSha256"),
                }
                hidden_state_keys = sorted(
                    key for key in chapter.get("authoritativeState", {})
                    if key in {"branchLedger", "threadLedger", "characterOutcomeStates", "itemOwnerCharacterIds", "itemLocationIds"}
                )
                samples.append({
                    "sampleId": selected["id"],
                    "entryPointId": entry_point["id"],
                    "sourceCharacterId": character_id,
                    "actionSource": action["source"],
                    "turnVariant": turn_variant,
                    "lineageKind": (
                        "entry_root" if turn_variant == "entry"
                        else "synthetic_fixed_replay" if turn_variant == "continuation"
                        else "persisted_temp_branch"
                    ),
                    "stateTransition": state_transition,
                    "lineageDepth": len(lineage),
                    "continuityWindowCount": len(chapter.get("continuityWindow", [])),
                    "contextSha256": bundle.context_sha256,
                    "full": _metrics(full),
                    "chapterProjection": _metrics(chapter),
                    "reduction": {
                        "chars": _metrics(full)["serializedChars"] - _metrics(chapter)["serializedChars"],
                        "estimatedTokens": _metrics(full)["estimatedTokens"] - _metrics(chapter)["estimatedTokens"],
                        "charsPercent": round(
                            100 * (1 - _metrics(chapter)["serializedChars"] / _metrics(full)["serializedChars"]), 2,
                        ),
                        "estimatedTokensPercent": round(
                            100 * (1 - _metrics(chapter)["estimatedTokens"] / _metrics(full)["estimatedTokens"]), 2,
                        ),
                    },
                    "requiredFieldsPresent": required,
                    "hiddenAuthoritativeStateKeys": hidden_state_keys,
                    "projectionBoundaryViolations": boundary_violations,
                    "projectionStage": chapter.get("stage"),
                })

            for action in actions:
                append_variant(
                    action,
                    current_parent=parent,
                    lineage=[parent],
                    player_direction=action["summary"],
                    turn_variant="entry",
                    state_transition="apply_branch_patch_validated_no_persist",
                )
                prior_node = {
                    "id": action["id"] + "-prior",
                    "kind": "branch",
                    "sourceNodeRef": parent["sourceNodeRef"],
                    "branchState": root_state,
                    "summary": action["summary"],
                    "narrativeText": action["summary"],
                    "factDeltas": [],
                }
                append_variant(
                    action,
                    current_parent=prior_node,
                    lineage=[parent, prior_node],
                    player_direction="上一回合已确认：" + action["summary"] + "；本回合继续整理当前证据。",
                    turn_variant="continuation",
                    state_transition="apply_branch_patch_validated_no_persist",
                )

            # Exercise one real branch append per entry in a disposable
            # SQLite store.  This validates the storage lineage and branch
            # ledger path without touching the formal project database.
            persisted_session_id = session_id + "-persisted"
            store = SessionStore(":memory:")
            try:
                store.create_session(
                    package, persisted_session_id, initial_state=root_state,
                )
                stored_root = store.create_branch_root(persisted_session_id, parent)
                direction = {
                    **parent["nextDirections"][0],
                    "statePatch": {
                        "derivedAdditions": {
                            "events": [{
                                "id": "event_fixed_replay_" + entry_point["id"].replace("entry_", ""),
                                "name": "固定回放状态转换",
                                "summary": "临时分支中已通过状态层校验的固定事件。",
                            }],
                        },
                    },
                }
                resolved = apply_branch_patch(
                    package,
                    stored_root["branchState"],
                    direction.get("statePatch", {}),
                    stored_root["sourceNodeRef"],
                    {"kind": "fixed_replay", "ref": direction["id"], "nodeRef": stored_root["sourceNodeRef"]},
                )
                child = store.append_branch(persisted_session_id, stored_root["id"], {
                    "sourceNodeRef": stored_root["sourceNodeRef"],
                    "branchState": resolved,
                    "narrativeText": direction["summary"],
                    "summary": direction["summary"],
                    "factDeltas": [],
                    "openThreads": stored_root.get("openThreads", []),
                    "nextDirections": stored_root.get("nextDirections", []),
                    "selectedDirectionId": direction["id"],
                    "selectedDirection": direction,
                    "playerDirection": direction["summary"],
                    "canonicalRelation": "diverged",
                })
                persisted_lineage = store.lineage(persisted_session_id, child["id"])
                persisted_action = {
                    "source": "persisted_official_direction",
                    "id": f"{entry_point['id']}-persisted",
                    "title": direction.get("title", "已发布方向"),
                    "summary": direction["summary"],
                }
                append_variant(
                    persisted_action,
                    current_parent=child,
                    lineage=persisted_lineage,
                    player_direction="上一回合已提交官方方向：" + direction["summary"] + "；本回合继续整理当前证据。",
                    turn_variant="persisted_continuation",
                    state_transition="apply_branch_patch_validated_and_temp_store_append",
                )
            finally:
                store.close()
    return samples


def _aggregate(samples: List[Mapping[str, Any]]) -> Dict[str, Any]:
    full_chars = [int(item["full"]["serializedChars"]) for item in samples]
    chapter_chars = [int(item["chapterProjection"]["serializedChars"]) for item in samples]
    full_tokens = [int(item["full"]["estimatedTokens"]) for item in samples]
    chapter_tokens = [int(item["chapterProjection"]["estimatedTokens"]) for item in samples]

    def summary(values: List[int]) -> Dict[str, Any]:
        return {
            "min": min(values),
            "p50": statistics.median(values),
            "p95": _percentile(values, 0.95),
            "max": max(values),
        }

    return {
        "fullChars": summary(full_chars),
        "chapterProjectionChars": summary(chapter_chars),
        "fullEstimatedTokens": summary(full_tokens),
        "chapterProjectionEstimatedTokens": summary(chapter_tokens),
        "meanCharReductionPercent": round(
            statistics.mean(100 * (1 - chapter / full) for full, chapter in zip(full_chars, chapter_chars)), 2,
        ),
        "meanEstimatedTokenReductionPercent": round(
            statistics.mean(100 * (1 - chapter / full) for full, chapter in zip(full_tokens, chapter_tokens)), 2,
        ),
    }


def run(output: Path) -> Dict[str, Any]:
    samples = _build_samples()
    if len(samples) < 20:
        raise RuntimeError(f"固定长篇回放样本不足 20 条：{len(samples)}")
    all_required = all(all(item["requiredFieldsPresent"].values()) for item in samples)
    no_hidden_state = all(not item["hiddenAuthoritativeStateKeys"] for item in samples)
    no_boundary_violations = all(not item["projectionBoundaryViolations"] for item in samples)
    continuation_samples = [
        item for item in samples
        if item["turnVariant"] in {"continuation", "persisted_continuation"}
    ]
    synthetic_continuation_samples = [
        item for item in samples if item["turnVariant"] == "continuation"
    ]
    persisted_continuation_samples = [
        item for item in samples if item["turnVariant"] == "persisted_continuation"
    ]
    synthetic_lineage_covered = bool(synthetic_continuation_samples) and all(
        item["lineageDepth"] >= 2 and item["continuityWindowCount"] >= 2
        for item in synthetic_continuation_samples
    )
    persisted_branch_covered = bool(persisted_continuation_samples) and all(
        item["lineageDepth"] >= 2
        and item["continuityWindowCount"] >= 2
        and item["lineageKind"] == "persisted_temp_branch"
        for item in persisted_continuation_samples
    )
    result = {
        "schemaVersion": "context-projection-replay/0.1",
        "recordedAt": "2026-09-24",
        "status": "projection_only_no_model",
        "purpose": "P2 固定长篇上下文体积与阶段边界回放；不代表正文质量或真实模型验收",
        "package": {
            "id": "taixu-relics-part1",
            "version": "0.1.3",
            "title": "《太虚遗录》",
            "sourceCjk": 102610,
        },
        "sampleDesign": {
            "sampleCount": len(samples),
            "officialOpeningActionsPerEntry": 2,
            "fixedHoldActionsPerEntry": 1,
            "continuationSamples": len(continuation_samples),
            "syntheticContinuationSamples": len(synthetic_continuation_samples),
            "persistedContinuationSamples": len(persisted_continuation_samples),
            "entryPointCount": len({item["entryPointId"] for item in samples}),
            "modelCalls": 0,
            "qualityComparison": "unavailable_without_model_replay",
            "continuationMode": "synthetic_lineage_shape_plus_disposable_temp_store_commit",
        },
        "comparison": {
            "full": "ContextBundle.as_dict()",
            "compressedEquivalent": "ContextBundle.project('chapter')",
            "sameSourceContextSha256": all(item["requiredFieldsPresent"]["contextSha256"] for item in samples),
            "hashMeaning": "projection traces back to the same source bundle; not semantic equivalence",
            "requiredHardFieldsPresent": all_required,
            "hiddenAuthoritativeStateExcluded": no_hidden_state,
            "recursiveBoundaryScanPassed": no_boundary_violations,
            "syntheticLineageContinuityWindowCovered": synthetic_lineage_covered,
            "persistedBranchCommitCovered": persisted_branch_covered,
            "fullVsCompressedQuality": "not_run",
        },
        "aggregate": _aggregate(samples),
        "samples": samples,
        "acceptance": {
            "projectionMeasurementPassed": bool(
                all_required and no_hidden_state and no_boundary_violations
                and synthetic_lineage_covered and persisted_branch_covered
            ),
            "defaultEnablement": "blocked_until_real_or_fixed_prose_quality_comparison",
            "jevExperiment": "deferred",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run(args.output)
    print(json.dumps({
        "output": str(args.output),
        "sampleCount": result["sampleDesign"]["sampleCount"],
        "aggregate": result["aggregate"],
        "acceptance": result["acceptance"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
