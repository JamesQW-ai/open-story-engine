"""Run a bounded paired prose experiment for full vs chapter context.

This is an experiment harness only.  It does not call the planner's commit
path, does not write a SessionStore, and does not invoke Jev.  Each pair uses
the same official action, state snapshot, model and local validators; only the
writer context representation changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping

from open_story_engine.api_narrative import (
    PlayerNarrativePlanner,
    check_player_voice,
    check_reader_repetition,
    player_package,
)
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import (
    apply_branch_patch,
    create_contract,
    entry_node,
    guard_narrative,
    guard_repeated_paragraphs,
    guard_source_character_names,
    plain_model_narrative,
)
from open_story_engine.llm import OpenAICompatibleGateway, writer_config_from_env
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.prompts import render_prompt
from test_support.longform import longform_cases


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs/evidence/context-management-2026-09-22/context-prose-ab-2026-09-24.json"
DEFAULT_ARTIFACT_DIR = ROOT / "docs/evidence/context-management-2026-09-22/context-prose-ab-2026-09-24"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_env(path: Path | None) -> None:
    """Load simple KEY=value entries without printing or storing secrets."""
    if path is None or not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip("'\"")


def _entry_samples(limit: int) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    for case in longform_cases():
        package = load_runtime_story_package(case["path"], lazy=True)
        resolver = ModuleContextResolver.for_package(case["path"], package)
        if resolver is None:
            raise RuntimeError("官方长篇缺少 ModuleContextResolver: " + str(case["package_id"]))
        for entry_point in list(package["story"]["entryModel"]["entryPoints"]):
            character_ids = list(entry_point.get("sourceCharacterIds") or [])
            if len(character_ids) != 1:
                raise RuntimeError("配对回放要求每个入口绑定一个 source character")
            character_id = character_ids[0]
            contract = create_contract(
                package,
                "context-prose-ab-20260924",
                {"kind": "source_character", "sourceCharacterId": character_id},
            )
            parent = entry_node(package, contract)
            actions = list(entry_point.get("openingActions") or [])
            actions.append({
                "id": f"{entry_point['id']}-hold",
                "title": "留在原地观察",
                "summary": "留在当前位置，只整理眼前已公开证据，暂不转场或替玩家作决定。",
                "statePatch": {},
            })
            for index, action in enumerate(actions):
                state_patch = dict(action.get("statePatch") or {})
                state_patch.setdefault("playerLocationId", parent["branchState"].get("playerLocationId"))
                selected = {
                    "id": f"{entry_point['id']}-ab-{index + 1}",
                    "title": action["title"],
                    "summary": action["summary"],
                    "statePatch": state_patch,
                }
                state = apply_branch_patch(
                    package, parent["branchState"], state_patch, parent["sourceNodeRef"],
                )
                context = {
                    "package": player_package(package, character_id),
                    "contract": contract,
                    "parent": parent,
                    "lineage": [parent],
                    "playerDirection": selected["summary"],
                    "characterDetails": [],
                    "validatedStatePatch": state_patch,
                    "validatedStatePatchSource": "apply_branch_patch",
                    "contextProjection": {"stateVisibilityMode": "formal_required"},
                }
                samples.append({
                    "sampleId": selected["id"],
                    "entryPointId": entry_point["id"],
                    "sourceCharacterId": character_id,
                    "package": package,
                    "resolver": resolver,
                    "context": context,
                    "selected": selected,
                    "state": state,
                })
                if len(samples) >= limit:
                    return samples
    return samples


def _system_message(context: Mapping[str, Any], selected: Mapping[str, Any]) -> str:
    persona = context["contract"]["persona"]
    return render_prompt(
        "reader.turn_system",
        system_instruction=PlayerNarrativePlanner.system_instruction,
        name=persona["name"],
        player_action=selected["summary"],
        interlude_material="",
    )


def _writer_prompt(payload: Mapping[str, Any], context: Mapping[str, Any], selected: Mapping[str, Any],
                   *, writer_controls: Mapping[str, Any] | None = None) -> str:
    """Render one writer contract, changing only the context payload."""
    persona = context["contract"]["persona"]
    control_instruction = (
        "writerControls 是本轮已校验的执行契约：按 resultContract.scenePlan 组织授权动作并在 stop 处停止；"
        "计划不是事实来源，事实仍以公开证据为准。pacing 是软预算，不为凑字数扩写。"
        "repair 非空时必须逐项纠正上一稿问题，不得忽略或把审查文字写入正文。\n"
        if writer_controls is not None else ""
    )
    return (
        "你是互动长篇小说的正文写作者。严格遵循 system 中的视角和输出要求。\n"
        "本回合只完成 selectedDirection 授权的行动，停在 outputContract 要求的停止点；"
        "只能把 confirmed、player_known 或 public_world_fact 的内容写成确定事实。\n"
        "下面是本回合唯一的上下文 payload。请读取其中 hardConstraints、turnIntent、"
        "authoritativeState、allowedEvidence、continuityWindow、dynamicMemory、styleGuide 和 outputContract，"
        "只输出小说正文，不输出 JSON、分析、菜单或字段说明。\n"
        + control_instruction + "\n"
        + _json({
            "persona": persona,
            "selectedDirection": selected,
            "contextPayload": payload,
            **({"writerControls": writer_controls} if writer_controls is not None else {}),
        })
    )


def _forbidden_keys(value: Any) -> List[str]:
    forbidden = {
        "branchLedger", "threadLedger", "characterOutcomeStates",
        "itemOwnerCharacterIds", "itemLocationIds", "provenance",
    }
    found: List[str] = []

    def walk(item: Any, path: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in forbidden:
                    found.append(path + "/" + key)
                walk(child, path + "/" + str(key))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, path + "/" + str(index))

    walk(value)
    return sorted(set(found))


def _validate_body(body: str, context: Mapping[str, Any], state: Mapping[str, Any]) -> List[str]:
    issues: List[str] = []
    checks = [
        ("plain_model_narrative", lambda: plain_model_narrative(body)),
        ("player_voice", lambda: check_player_voice(body, context["contract"]["persona"]["name"])),
        ("narrative_guard", lambda: guard_narrative(
            body,
            dict(state),
            list(context.get("characterDetails") or []),
            context["package"]["world"]["narrativeGuidelines"],
            context["package"],
        )),
        ("source_character_names", lambda: guard_source_character_names(
            body,
            context["package"],
            dict(state),
            session_persona=context["contract"]["persona"],
        )),
        ("repeated_paragraphs", lambda: guard_repeated_paragraphs(body)),
        ("reader_repetition", lambda: check_reader_repetition(body)),
    ]
    for name, check in checks:
        try:
            check()
        except Exception as error:  # local validator errors are evidence
            issues.append(f"{name}: {error}")
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", body))
    if cjk == 0:
        issues.append("empty_or_non_cjk_body")
    return issues


def _run_arm(
    gateway: OpenAICompatibleGateway,
    sample: Mapping[str, Any],
    variant: str,
    user_prompt: str,
    artifact_dir: Path,
) -> Dict[str, Any]:
    messages = [
        {"role": "system", "content": _system_message(sample["context"], sample["selected"])},
        {"role": "user", "content": user_prompt},
    ]
    started = time.monotonic()
    record: Dict[str, Any] = {
        "variant": variant,
        "promptSha256": _sha256(user_prompt),
        "promptChars": len(user_prompt),
        "promptEstimatedTokens": None,
        "status": "failed",
    }
    try:
        completion = gateway.complete_text(messages)
        raw_content = completion.content
        body = plain_model_narrative(raw_content)
        issues = _validate_body(body, sample["context"], sample["state"])
        record.update({
            "status": "passed_local_validation" if not issues else "failed_local_validation",
            "body": body,
            "bodySha256": _sha256(body),
            "bodyChars": len(body),
            "bodyCjk": len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", body)),
            "issues": issues,
            "observations": completion.observations,
            "rawResponse": completion.raw_response,
            "transportFallback": completion.used_transport_fallback,
            "bodyWasStreamed": completion.body_was_streamed,
        })
        try:
            raw = json.loads(completion.raw_response)
            usage = raw.get("usage") if isinstance(raw, dict) else None
        except (TypeError, ValueError):
            usage = None
        if isinstance(usage, Mapping):
            record.update({
                "reportedInputTokens": usage.get("prompt_tokens"),
                "reportedOutputTokens": usage.get("completion_tokens"),
                "reportedTotalTokens": usage.get("total_tokens"),
                "promptCacheHitTokens": usage.get("prompt_cache_hit_tokens"),
                "promptCacheMissTokens": usage.get("prompt_cache_miss_tokens"),
            })
        else:
            record["usageUnavailable"] = True
    except Exception as error:
        record.update({"error": str(error), "errorType": type(error).__name__})
    record["elapsedMs"] = round((time.monotonic() - started) * 1000)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{sample['sampleId']}-{variant}.json"
    artifact_path.write_text(json.dumps({
        "sampleId": sample["sampleId"],
        "variant": variant,
        "messages": messages,
        "result": record,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record["artifact"] = str(artifact_path.relative_to(ROOT))
    record.pop("body", None)
    record.pop("rawResponse", None)
    return record


def run(output: Path, artifact_dir: Path, limit: int) -> Dict[str, Any]:
    config = writer_config_from_env()
    missing = [key for key in ("base_url", "api_key", "model") if not config.get(key)]
    if missing:
        result = {
            "schemaVersion": "context-prose-ab/0.2",
            "recordedAt": "2026-09-24",
            "status": "blocked_missing_model_configuration",
            "missing": missing,
            "sampleDesign": {"requested": limit, "modelCalls": 0},
            "qualityComparison": "not_run",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    try:
        timeout = int(os.environ.get("STORY_LLM_TIMEOUT_SECONDS", "120"))
    except ValueError:
        timeout = 120
    try:
        max_tokens = int(os.environ.get("STORY_LLM_TEXT_MAX_TOKENS", "8192"))
    except ValueError:
        max_tokens = 8192
    gateway = OpenAICompatibleGateway(
        config["base_url"],
        config["api_key"],
        config["model"],
        stream=False,
        timeout_seconds=timeout,
        max_tokens=max_tokens,
        text_max_tokens=max_tokens,
        allow_transport_fallback=False,
        reasoning_effort=os.environ.get("STORY_LLM_REASONING_EFFORT") or None,
    )
    samples = _entry_samples(limit)
    gateway.remaining_calls = len(samples) * 2
    pairs: List[Dict[str, Any]] = []
    for index, sample in enumerate(samples, start=1):
        planner = PlayerNarrativePlanner(
            SimpleNamespace(model=config["model"]),
            context_resolver=sample["resolver"],
        )
        planner._prompt(sample["context"], sample["selected"], sample["state"], None)
        bundle = planner.last_context_bundle
        if bundle is None:
            raise RuntimeError("chapter prompt 未生成 ContextBundle: " + sample["sampleId"])
        full = bundle.as_dict()
        chapter_payload = bundle.project("chapter")
        # Both arms use the same system/user contract.  Only this payload is
        # changed, so quality differences are attributable to projection
        # content rather than a second prompt template.
        canonical_chapter_prompt = _writer_prompt(
            chapter_payload, sample["context"], sample["selected"],
        )
        canonical_full_prompt = _writer_prompt(
            full, sample["context"], sample["selected"],
        )
        chapter = _run_arm(gateway, sample, "chapter", canonical_chapter_prompt, artifact_dir)
        full_arm = _run_arm(gateway, sample, "full", canonical_full_prompt, artifact_dir)
        pairs.append({
            "sampleId": sample["sampleId"],
            "entryPointId": sample["entryPointId"],
            "contextSha256": full["contextSha256"],
            "fullAndChapterSameSourceContext": full["contextSha256"] == chapter_payload["contextSha256"],
            "fullArmPolicy": "negative_control_only",
            "fullArmForbiddenKeys": _forbidden_keys(full),
            "chapter": chapter,
            "full": full_arm,
        })
        print(json.dumps({
            "completed": index,
            "total": len(samples),
            "sampleId": sample["sampleId"],
            "chapter": chapter["status"],
            "full": full_arm["status"],
        }, ensure_ascii=False), flush=True)

    def count(variant: str, status: str) -> int:
        return sum(1 for pair in pairs if pair[variant]["status"] == status)

    result = {
        "schemaVersion": "context-prose-ab/0.2",
        "recordedAt": "2026-09-24",
        "status": "completed",
        "purpose": "同一官方长篇、动作、状态、模型和写作提示下，只替换 full ContextBundle 与 chapter 投影 payload；正文缓冲后执行相同本地校验",
        "model": {"route": config["route"], "model": config["model"], "stream": False, "transportFallback": False},
        "sampleDesign": {
            "requested": limit,
            "actual": len(samples),
            "pairs": len(pairs),
            "modelCalls": len(pairs) * 2,
            "officialLongformOnly": True,
            "formalSessionWrites": 0,
            "jevCalls": 0,
        },
        "qualityComparison": {
            "chapterPassedLocalValidation": count("chapter", "passed_local_validation"),
            "fullPassedLocalValidation": count("full", "passed_local_validation"),
            "chapterFailedLocalValidation": count("chapter", "failed_local_validation"),
            "fullFailedLocalValidation": count("full", "failed_local_validation"),
            "chapterErrors": count("chapter", "failed"),
            "fullErrors": count("full", "failed"),
        },
        "fullArmPolicy": {
            "mode": "negative_control_only",
            "productionEnablement": "forbidden",
            "reason": "完整 bundle 含 chapter 明确排除的权威状态与来源追踪字段，用于暴露隐藏事实泄漏风险",
            "pairsWithForbiddenKeys": sum(1 for pair in pairs if pair["fullArmForbiddenKeys"]),
        },
        "latencyMs": {
            "chapter": sorted(pair["chapter"]["elapsedMs"] for pair in pairs),
            "full": sorted(pair["full"]["elapsedMs"] for pair in pairs),
        },
        "pairs": pairs,
        "acceptance": {
            "qualityGatePassed": False,
            "reason": "配对正文实验只完成本地首稿校验；未执行完整 planner 提交链路，因此不能提升默认路径或启动 Jev",
            "defaultEnablement": "blocked_until_full_planner_commit_replay_and_human_review",
            "jevExperiment": "deferred",
        },
        "manualReviewRequired": True,
        "humanReviewScope": [
            "完整 bundle 与 chapter 的同一动作、同一停止点和叙事视角是否一致",
            "正文是否把未在 allowedEvidence 或 authoritativeState 中确认的细节写成事实",
            "短正文是否遗漏行动结果、停止点或必要因果，而本地结构校验未能发现",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    _load_env(args.env_file)
    if args.limit <= 0:
        raise SystemExit("--limit 必须大于 0")
    result = run(args.output, args.artifact_dir, args.limit)
    print(json.dumps({
        "output": str(args.output),
        "status": result["status"],
        "sampleDesign": result.get("sampleDesign"),
        "qualityComparison": result.get("qualityComparison"),
        "acceptance": result.get("acceptance"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
