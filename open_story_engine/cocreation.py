"""Co-creation tree, immutable source reuse and optional LLM quality review."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .content import by_id
from .llm import LlmError, OpenAICompatibleGateway, parse_json_content
from .storage import SessionStore


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def initial_branch_state(beat: Dict[str, Any]) -> Dict[str, Any]:
    state = copy.deepcopy(beat["branchState"])
    state.setdefault("storyScope", "source")
    state.setdefault("derivativeStage", "inactive")
    state.setdefault("derivedTurn", 0)
    state.setdefault("derivedLocations", [])
    state.setdefault("derivedCharacters", [])
    state.setdefault("derivedItems", [])
    state.setdefault("derivedCharacterReveals", [])
    return state


def empty_branch_additions() -> Dict[str, List[Dict[str, Any]]]:
    return {"locations": [], "characters": [], "items": [], "characterReveals": []}


BRANCH_PRIVATE_STATE_KEYS = frozenset({"derivedLocations", "derivedCharacters", "derivedItems", "derivedCharacterReveals"})


def arc_model(package: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the optional, package-owned macro-plot model."""
    model = package.get("story", {}).get("arcModel")
    return model if isinstance(model, dict) else None


def arc_by_id(package: Dict[str, Any], arc_id: str) -> Dict[str, Any]:
    model = arc_model(package)
    if model is None:
        raise ValueError("StoryPackage 未声明大方向模型")
    arc = next((item for item in model["arcs"] if item["id"] == arc_id), None)
    if arc is None:
        raise ValueError("大方向不存在: " + arc_id)
    return arc


def macro_directions(package: Dict[str, Any], arc_ids: List[str], state: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    directions: List[Dict[str, Any]] = []
    for arc_id in arc_ids:
        arc = arc_by_id(package, arc_id)
        if state is not None and arc.get("availableWhen") and not state_matches(arc["availableWhen"], state):
            continue
        directions.append({
            "id": "arc:" + arc_id,
            "title": arc["title"],
            "summary": arc["summary"],
            "directionLevel": "arc",
            "arcId": arc_id,
        })
    return directions


def phase_directions_for_arc(
    package: Dict[str, Any], source_node_ref: str, state: Dict[str, Any], arc_id: str,
) -> List[Dict[str, Any]]:
    """Expose only the current chapter-sized choices declared for one macro plot."""
    phase_ids = set(arc_by_id(package, arc_id)["phaseDirectionIds"])
    directions = controlled_scene_directions(package, source_node_ref, state)
    return [
        {**direction, "directionLevel": "phase", "arcId": arc_id}
        for direction in directions
        if direction["id"] in phase_ids
    ]


def advance_story_arc(
    package: Dict[str, Any], parent_arc: Optional[Dict[str, Any]], selected: Dict[str, Any],
    resolved_state: Dict[str, Any], model_arc: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    """Make chapter and macro-plot transitions authoritative after one phase."""
    if parent_arc is None or not parent_arc.get("arcId"):
        return model_arc, None
    arc = arc_by_id(package, parent_arc["arcId"])
    completed_phase_ids = list(parent_arc.get("completedPhaseIds", []))
    if selected["id"] not in completed_phase_ids:
        completed_phase_ids.append(selected["id"])
    chapter = (model_arc or {}).get("chapter", {})
    chapter_title = chapter.get("title") if isinstance(chapter.get("title"), str) and chapter["title"].strip() else selected["title"]
    previous_title = parent_arc.get("chapter", {}).get("title")
    if chapter_title == previous_title:
        chapter_title = selected["title"]
    arc_completed = state_matches(arc["completionWhen"], resolved_state)
    next_directions = macro_directions(package, arc.get("nextArcIds", []), resolved_state) if arc_completed else None
    return {
        "arcId": arc["id"],
        "activeGoal": arc["title"],
        "currentPhase": selected["title"],
        "completedPhaseIds": completed_phase_ids,
        "goalDisposition": "completed" if arc_completed else "continued",
        "menuLevel": "arc" if arc_completed else "phase",
        "chapter": {"title": chapter_title, "status": "complete"},
    }, next_directions


def state_matches(constraints: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """Evaluate a StoryPackage declarative state constraint."""
    for field, expected in constraints.items():
        actual = state.get(field)
        if isinstance(expected, dict):
            if "equals" in expected and actual != expected["equals"]:
                return False
            if "oneOf" in expected and actual not in expected["oneOf"]:
                return False
            if "sameAs" in expected and actual != state.get(expected["sameAs"]):
                return False
        elif actual != expected:
            return False
    return True


def has_branch_additions(value: Dict[str, List[Dict[str, Any]]]) -> bool:
    return any(value[key] for key in ("locations", "characters", "items", "characterReveals"))


def validate_branch_additions(package: Dict[str, Any], state: Dict[str, Any], value: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Validate branch-private world expansion without mutating StoryPackage."""
    if value is None:
        return empty_branch_additions()
    if not isinstance(value, dict) or set(value) - {"locations", "characters", "items", "characterReveals"}:
        raise ValueError("分支扩展只能包含 locations、characters、items、characterReveals")
    additions = empty_branch_additions()
    known_ids = {item["id"] for item in package["locations"] + package["characters"] + package["items"]}
    derived_locations = state.get("derivedLocations", [])
    derived_characters = state.get("derivedCharacters", [])
    derived_items = state.get("derivedItems", [])
    known_ids.update(item["id"] for item in derived_locations + derived_characters + derived_items)
    known_location_names = {item["name"].strip() for item in package["locations"] + derived_locations}
    known_item_names = {item["name"].strip() for item in package["items"] + derived_items}
    for collection in ("locations", "characters", "items"):
        entries = value.get(collection, [])
        if not isinstance(entries, list):
            raise ValueError(f"分支扩展的 {collection} 必须是数组")
        for entity in entries:
            if not isinstance(entity, dict) or not all(isinstance(entity.get(key), str) and entity[key].strip() for key in ("id", "name", "summary")):
                raise ValueError(f"分支扩展的{collection}必须包含 id、name、summary")
            if not re.fullmatch(r"(?:location|character|item)_[a-z0-9_]{2,80}", entity["id"]):
                raise ValueError("分支扩展实体 id 必须使用 location_、character_ 或 item_ 前缀")
            expected_prefix = {"locations": "location_", "characters": "character_", "items": "item_"}[collection]
            if not entity["id"].startswith(expected_prefix) or entity["id"] in known_ids:
                raise ValueError(f"分支扩展实体 id 无效或已存在: {entity['id']}")
            if collection == "locations" and entity["name"].strip() in known_location_names:
                raise ValueError(f"分支扩展地点与已登记地点重名: {entity['name']}")
            if collection == "items" and entity["name"].strip() in known_item_names:
                raise ValueError(f"分支扩展物品与已登记物品重名: {entity['name']}")
            additions[collection].append({key: entity[key].strip() for key in ("id", "name", "summary")})
            known_ids.add(entity["id"])
            if collection == "locations":
                known_location_names.add(entity["name"].strip())
            if collection == "items":
                known_item_names.add(entity["name"].strip())
    known_characters = {item["id"] for item in derived_characters} | {item["id"] for item in additions["characters"]}
    revealed = {item["characterId"] for item in state.get("derivedCharacterReveals", [])}
    reveals = value.get("characterReveals", [])
    if not isinstance(reveals, list):
        raise ValueError("分支扩展的 characterReveals 必须是数组")
    for reveal in reveals:
        if not isinstance(reveal, dict) or not all(isinstance(reveal.get(key), str) and reveal[key].strip() for key in ("characterId", "name", "summary")):
            raise ValueError("人物身份揭示必须包含 characterId、name、summary")
        if reveal["characterId"] not in known_characters or reveal["characterId"] in revealed:
            raise ValueError("人物身份只能对已出现且未揭示的人物声明一次")
        additions["characterReveals"].append({key: reveal[key].strip() for key in ("characterId", "name", "summary")})
        revealed.add(reveal["characterId"])
    return additions


def normalize_optional_branch_items(package: Dict[str, Any], state: Dict[str, Any], value: Any, observations: List[Dict[str, Any]]) -> Any:
    """Repair optional item metadata without changing authoritative state."""
    if value is None or not isinstance(value, dict):
        return value
    normalized = {key: copy.deepcopy(item) for key, item in value.items() if key in empty_branch_additions()}
    ignored = set(value) - set(normalized)
    if ignored:
        observations.append({"attempt": 1, "outcome": "normalized", "normalization": "discarded_unknown_branch_addition_fields", "fields": sorted(ignored)})
    entries = normalized.get("items", [])
    if isinstance(entries, dict):
        entries = [entries]
    elif isinstance(entries, str):
        entries = [{"name": entries}]
    elif not isinstance(entries, list):
        observations.append({"attempt": 1, "outcome": "normalized", "normalization": "discarded_non_array_branch_items"})
        entries = []
    known_ids = {item["id"] for item in package["locations"] + package["characters"] + package["items"]}
    known_ids.update(item["id"] for item in state.get("derivedLocations", []) + state.get("derivedCharacters", []) + state.get("derivedItems", []))
    known_names = {item["name"].strip() for item in package["items"] + state.get("derivedItems", [])}
    repaired: List[Dict[str, str]] = []
    discarded = 0
    repaired_count = 0
    for entry in entries:
        candidate = entry if isinstance(entry, dict) else {"name": entry} if isinstance(entry, str) else {}
        name = candidate.get("name")
        if not isinstance(name, str) or not name.strip():
            discarded += 1
            continue
        name = name.strip()
        if name in known_names:
            discarded += 1
            continue
        summary = candidate.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = candidate.get("description")
        if not isinstance(summary, str) or not summary.strip():
            summary = "本回合新增的分支私有物品：" + name + "。"
        summary = summary.strip()
        item_id = candidate.get("id")
        if not isinstance(item_id, str) or not re.fullmatch(r"item_[a-z0-9_]{2,80}", item_id) or item_id in known_ids:
            digest = hashlib.sha256((name + "\n" + summary).encode("utf-8")).hexdigest()[:16]
            item_id = "item_generated_" + digest
            while item_id in known_ids:
                digest = hashlib.sha256((item_id + name).encode("utf-8")).hexdigest()[:16]
                item_id = "item_generated_" + digest
            repaired_count += 1
        if not (isinstance(candidate.get("summary"), str) and candidate["summary"].strip()):
            repaired_count += 1
        repaired.append({"id": item_id, "name": name, "summary": summary})
        known_ids.add(item_id)
        known_names.add(name)
    normalized["items"] = repaired
    if repaired_count or discarded:
        observations.append({"attempt": 1, "outcome": "normalized", "normalization": "repaired_optional_branch_items", "repaired": repaired_count, "discarded": discarded})
    return normalized


def parse_narrative_continuation(content: str) -> str:
    """Parse the continuation's prose-only response without relaxing planner JSON."""
    try:
        result = parse_json_content(content)
    except LlmError as error:
        normalized = content.strip()
        match = re.fullmatch(
            r'\{\s*"narrativeContinuation"\s*:\s*"(?P<text>.*)"\s*\}',
            normalized,
            flags=re.DOTALL,
        )
        if not match:
            raise error
        encoded = '"' + match.group("text").replace("\r\n", "\\n").replace("\n", "\\n") + '"'
        try:
            continuation = json.loads(encoded)
        except json.JSONDecodeError:
            raise error
        if not isinstance(continuation, str) or not continuation.strip():
            raise error
        return continuation
    if set(result) != {"narrativeContinuation"}:
        raise LlmError("LLM 剧情续写结果只能包含 narrativeContinuation", "model_output_rejected")
    continuation = result.get("narrativeContinuation")
    if not isinstance(continuation, str) or not continuation.strip():
        raise LlmError("LLM 剧情续写结果缺少 narrativeContinuation", "model_output_rejected")
    return continuation


def apply_branch_patch(package: Dict[str, Any], current: Dict[str, Any], patch: Dict[str, Any], source_node_ref: str) -> Dict[str, Any]:
    if not patch:
        raise ValueError("剧情方向必须声明至少一个状态变化")
    unsupported = set(patch) - set(current) - {"derivedAdditions"}
    if unsupported:
        raise ValueError("剧情方向包含不受支持的状态字段: " + "、".join(sorted(unsupported)))
    private_fields = set(patch) & BRANCH_PRIVATE_STATE_KEYS
    if private_fields:
        raise ValueError("剧情方向不能直接改写分支私有状态字段: " + "、".join(sorted(private_fields)) + "；请使用 derivedAdditions")
    for key, value in patch.items():
        if key == "derivedAdditions" or value is None:
            continue
        if isinstance(value, (dict, list)):
            raise ValueError(f"剧情方向的 {key} 必须使用目标原始值，不能使用对象或数组")
        if type(value) is not type(current[key]):
            raise ValueError(f"剧情方向的 {key} 类型必须与当前状态一致")
    next_state = copy.deepcopy(current)
    additions = validate_branch_additions(package, current, patch.get("derivedAdditions"))
    for key, value in patch.items():
        if key != "derivedAdditions" and value is not None:
            next_state[key] = value
    for collection, label in (("locations", "地点"), ("characters", "人物"), ("items", "物品")):
        known = {entry["id"] for entry in next_state["derived" + collection.title()]}
        for entity in additions.get(collection, []):
            if entity["id"] in known:
                raise ValueError(f"派生故事不能重复引入{label}: {entity['id']}")
            next_state["derived" + collection.title()].append(copy.deepcopy(entity))
            known.add(entity["id"])
    # Python title() is not the data field for characterReveals, handle explicitly.
    known_characters = {entry["id"] for entry in next_state["derivedCharacters"]}
    revealed = {entry["characterId"] for entry in next_state["derivedCharacterReveals"]}
    for reveal in additions.get("characterReveals", []):
        if reveal["characterId"] not in known_characters or reveal["characterId"] in revealed:
            raise ValueError("人物身份只能对已出现且未揭示的人物声明一次")
        next_state["derivedCharacterReveals"].append(copy.deepcopy(reveal))
        revealed.add(reveal["characterId"])
    state_model = package.get("stateModel", {})
    ranks = state_model.get("monotonicEnums", {})
    for key, order in ranks.items():
        if key not in current or key not in next_state:
            raise ValueError(f"状态模型引用了不存在的状态字段: {key}")
        if next_state[key] not in order:
            raise ValueError(f"{key} 不支持状态值: {next_state[key]}")
        if current[key] not in order:
            raise ValueError(f"当前状态包含不支持的 {key} 值: {current[key]}")
        if order.index(next_state[key]) < order.index(current[key]):
            raise ValueError(f"{key} 不能倒退: {current[key]} -> {next_state[key]}")
    for field in state_model.get("immutableFields", []):
        if current.get(field) != next_state.get(field):
            raise ValueError(f"剧情方向不能改变不可变状态字段: {field}")
    for rule in state_model.get("transitionRules", []):
        if not state_matches(rule.get("when", {}), next_state):
            continue
        field = rule["field"]
        if rule["change"] == "increment" and next_state.get(field) != current.get(field) + rule["amount"]:
            raise ValueError(rule["message"])
    known_locations = {location["id"] for location in package["locations"]} | {location["id"] for location in next_state["derivedLocations"]}
    for field in state_model.get("locationReferenceFields", []):
        if next_state.get(field) not in known_locations:
            raise ValueError(f"共创状态的 {field} 引用了不存在的地点")
    for invariant in state_model.get("invariants", []):
        if state_matches(invariant["when"], next_state) and not state_matches(invariant["require"], next_state):
            raise ValueError(invariant["message"])
    return next_state


def branch_direction(direction: Dict[str, Any]) -> Dict[str, Any]:
    return {key: copy.deepcopy(direction[key]) for key in ("id", "title", "summary", "canonicalBeatId", "followupBeatId", "rejoinTargetId", "statePatch", "directionLevel", "arcId") if key in direction}


def create_contract(package: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    player = next(character for character in package["characters"] if character["id"] == package["initialState"]["player"]["characterId"])
    return {
        "id": "contract_" + str(uuid.uuid4()), "sessionId": session_id, "mode": "derivative_co_creation",
        "sourcePackageRef": {"id": package["id"], "version": package["version"]}, "entryNodeId": package["story"]["startNodeId"],
        "canonicalPrefixNodeIds": [package["story"]["startNodeId"]],
        "canonicalTimelineRefs": [item["id"] for item in package["timeline"] if item["knownAtStart"]],
        "continuityScope": "canonical_until_entry", "persona": {"kind": "source_character", "sourceCharacterId": player["id"], "name": player["name"]},
        "immutableFactRefs": [fact["id"] for fact in package["world"]["immutableFacts"]],
        "direction": next(item for item in package["directions"] if item["id"] == package["defaultDirectionId"]),
        "provenance": [{"kind": "source", "ref": package["id"] + "@" + package["version"], "note": "固定原著故事包引用"}], "createdAt": timestamp(),
    }


def entry_node(package: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
    graph = package["story"]["narrativeGraph"]
    beat = next((item for item in graph["beats"] if item["id"] == graph["startBeatId"]), None)
    if beat is None:
        raise ValueError("共创起始节点没有叙事锚点")
    model = arc_model(package)
    directions = macro_directions(package, model["entryArcIds"], initial_branch_state(beat)) if model is not None else [branch_direction(item) for item in beat["nextDirections"]]
    return {
        "id": "branch_" + str(uuid.uuid4()), "kind": "source_entry", "sourceNodeRef": contract["entryNodeId"],
        "branchState": initial_branch_state(beat), "narrativeText": beat.get("sourceExcerpt", {}).get("text", beat["narrativeAnchor"]),
        "summary": beat["summary"], "factDeltas": [{"id": "fact_source_entry", "source": "source", "summary": "原著前史已继承"}],
        "openThreads": beat["openThreads"], "nextDirections": directions,
        "canonicalRelation": "on_line", "planning": {"citations": [{"kind": "canonical_node", "ref": contract["entryNodeId"], "rationale": "共创从原著节点进入。"}], "confidence": "high", "stateChangeProposals": []}, "createdAt": timestamp(),
    }


def resolve_source_node(package: Dict[str, Any], parent: Dict[str, Any], state: Dict[str, Any], selected: Dict[str, Any]) -> str:
    if state["storyScope"] == "derived":
        return parent.get("sourceNodeRef") or package["story"]["startNodeId"]
    source = parent.get("sourceNodeRef") or package["story"]["startNodeId"]
    focal_location_field = focal_character_location_field(package, state)
    if focal_location_field is None:
        return source
    for route in package["story"].get("sceneRoutes", []):
        if (
            route["fromNodeId"] == source
            and route["fromLocationId"] == parent["branchState"].get(focal_location_field)
            and route["toLocationId"] == state.get(focal_location_field)
        ):
            return route["toNodeId"]
    return source


def find_beat(package: Dict[str, Any], beat_id: str) -> Dict[str, Any]:
    beat = next((item for item in package["story"]["narrativeGraph"]["beats"] if item["id"] == beat_id), None)
    if beat is None:
        raise ValueError("叙事锚点不存在: " + beat_id)
    return beat


def resolve_rejoin(
    package: Dict[str, Any],
    parent_source_node_id: str,
    parent_open_threads: List[str],
    parent_state: Dict[str, Any],
    target_node_id: str,
    resolved_state: Dict[str, Any],
    direction: Dict[str, Any],
) -> Optional[Dict[str, str]]:
    target_id = direction.get("rejoinTargetId")
    if not target_id:
        return None
    target = next((item for item in package["story"].get("rejoinTargets", []) if item["id"] == target_id), None)
    if target is None:
        raise ValueError("方向引用的汇合目标不存在: " + str(target_id))
    if target["fromNodeId"] != parent_source_node_id:
        raise ValueError("汇合目标的来源场景不匹配: " + target["fromNodeId"])
    if not all(rejoin_thread_is_open(thread, parent_open_threads) for thread in target["requiredOpenThreads"]):
        raise ValueError("汇合目标缺少必要未解线索: " + target["id"])
    if target.get("requiredState") and not state_matches(target["requiredState"], parent_state):
        raise ValueError("汇合目标不满足必要状态: " + target["id"])
    beat = find_beat(package, target["targetBeatId"])
    if beat["nodeId"] != target_node_id:
        raise ValueError("汇合目标场景与受控路线不一致: " + target["id"])
    if initial_branch_state(beat) != resolved_state:
        raise ValueError("汇合目标状态不兼容: " + target["id"])
    return {"id": target["id"], "targetBeatId": target["targetBeatId"]}


def rejoin_thread_is_open(required_thread: str, open_threads: List[str]) -> bool:
    """Rejoin prerequisites are package-declared thread identifiers, not prose heuristics."""
    return required_thread in open_threads


def assert_published_directions(package: Dict[str, Any], source_node_ref: str, state: Dict[str, Any], directions: List[Dict[str, Any]]) -> None:
    """Validate announced choices before a player can choose them on the next turn."""
    parent = {"sourceNodeRef": source_node_ref, "branchState": state}
    for direction in directions:
        if direction.get("directionLevel") == "arc":
            continue
        provisional = copy.deepcopy(state)
        provisional.update({key: value for key, value in direction["statePatch"].items() if key != "derivedAdditions"})
        target_node_ref = resolve_source_node(package, parent, provisional, direction)
        next_state = apply_branch_patch(package, state, direction["statePatch"], target_node_ref)
        if next_state == state:
            raise ValueError("后续方向必须推进至少一项已确认状态: " + direction["id"])


def matches_storypackage_state(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    """Match package-controlled facts while allowing branch-private world additions.

    Derived entities belong to a runtime branch, rather than the fixed
    StoryPackage. They must not hide a still-valid package progression choice.
    Exact matching remains in place for canonical rejoin decisions elsewhere.
    """
    return (
        {key: value for key, value in expected.items() if key not in BRANCH_PRIVATE_STATE_KEYS}
        == {key: value for key, value in actual.items() if key not in BRANCH_PRIVATE_STATE_KEYS}
    )


def controlled_scene_directions(package: Dict[str, Any], source_node_ref: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the package menu for exactly this source scene and state."""
    if state["storyScope"] != "source":
        return []
    for beat in package["story"]["narrativeGraph"]["beats"]:
        if beat["nodeId"] != source_node_ref or not matches_storypackage_state(initial_branch_state(beat), state):
            continue
        directions = [branch_direction(item) for item in beat["nextDirections"]]
        if directions:
            assert_published_directions(package, source_node_ref, state, directions)
        return directions
    return []


def controlled_followup_directions(
    package: Dict[str, Any],
    selected: Dict[str, Any],
    source_node_ref: str,
    state: Dict[str, Any],
) -> Optional[List[Dict[str, Any]]]:
    """Return the declared follow-up menu for a source turn, when available.

    The StoryPackage menu is a safe fallback when a model menu is unusable.
    A valid model menu, including one that references registered branch-private
    entities, remains available to the player. ``None`` means there is no
    matching template; an empty list is a valid terminal source menu.
    """
    if state["storyScope"] != "source":
        return None
    beat_id = selected.get("followupBeatId") or selected.get("canonicalBeatId")
    if beat_id:
        beat = find_beat(package, beat_id)
        if matches_storypackage_state(initial_branch_state(beat), state):
            directions = [branch_direction(item) for item in beat["nextDirections"]]
            if directions:
                assert_published_directions(package, source_node_ref, state, directions)
            return directions
    for beat in package["story"]["narrativeGraph"]["beats"]:
        if beat["nodeId"] != source_node_ref or not matches_storypackage_state(initial_branch_state(beat), state):
            continue
        directions = [branch_direction(item) for item in beat["nextDirections"]]
        if directions:
            assert_published_directions(package, source_node_ref, state, directions)
        return directions
    return None


def valid_model_directions(
    package: Dict[str, Any],
    source_node_ref: str,
    state: Dict[str, Any],
    value: Any,
    observations: List[Dict[str, Any]],
    attempt: int,
) -> List[Dict[str, Any]]:
    """Keep valid dynamic choices; discard only invalid structured proposals."""
    if not isinstance(value, list):
        observations.append({"attempt": attempt, "outcome": "normalized", "normalization": "discarded_non_array_model_directions"})
        return []
    accepted: List[Dict[str, Any]] = []
    for item in value[:4]:
        try:
            direction = validate_direction(item)
            assert_published_directions(package, source_node_ref, state, [direction])
            accepted.append(direction)
        except (LlmError, ValueError) as error:
            observations.append({
                "attempt": attempt,
                "outcome": "normalized",
                "normalization": "discarded_invalid_model_direction",
                "error": str(error),
            })
    return accepted


def merge_followup_directions(
    model_directions: List[Dict[str, Any]],
    controlled_directions: Optional[List[Dict[str, Any]]],
    observations: List[Dict[str, Any]],
    attempt: int,
) -> List[Dict[str, Any]]:
    """Keep valid model choices without omitting package-critical progress.

    Source-package templates are ordered first and take precedence for an ID
    collision. A declared empty template is a terminal source branch; players
    can explicitly create a derivative package from that endpoint.
    """
    if controlled_directions is None:
        return model_directions
    if not controlled_directions:
        if model_directions:
            observations.append({"attempt": attempt, "outcome": "normalized", "normalization": "preserved_storypackage_terminal"})
        return []
    merged = list(controlled_directions)
    controlled_ids = {direction["id"] for direction in controlled_directions}
    dynamic = [direction for direction in model_directions if direction["id"] not in controlled_ids]
    if dynamic:
        merged.extend(dynamic)
    if model_directions and (len(merged) != len(model_directions) or controlled_ids - {direction["id"] for direction in model_directions}):
        observations.append({"attempt": attempt, "outcome": "normalized", "normalization": "preserved_storypackage_progression_directions"})
    return merged


def fallback_story_arc(context: Dict[str, Any], selected: Dict[str, Any], directions: List[Dict[str, Any]]) -> Dict[str, Any]:
    parent_arc = context["parent"].get("storyArc") or {}
    chapter = parent_arc.get("chapter") or {}
    return {
        "activeGoal": parent_arc.get("activeGoal", selected["title"]),
        "currentPhase": selected["title"],
        "goalDisposition": "continued" if directions else "completed",
        "chapter": {"title": chapter.get("title", selected["title"]), "status": "continuing" if directions else "complete"},
    }


def confirmed_character_details(package: Dict[str, Any], lineage: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    # This is a compact, prompt-only continuity digest. It remains advisory; the
    # deterministic state and narrative guard remain authoritative.
    details: List[Dict[str, str]] = []
    for character in package["characters"]:
        description = character.get("description", "")
        visual = re.findall(r"(?:穿着|戴着|手里|腰间|袖口|雨衣)[^。；，]{2,24}", description)
        if visual:
            details.append({"name": character["name"], "detail": "；".join(visual[:2])})
    for node in lineage:
        text = node.get("narrativeText", "")
        for character in package["characters"]:
            for fragment in re.findall(rf"{re.escape(character['name'])}[^。！？]{{0,35}}(?:穿着|戴着|手里|腰间)[^。！？]{{0,28}}", text):
                details.append({"name": character["name"], "detail": fragment})
    return details[-12:]


def source_continuity_context(package: Dict[str, Any], lineage: List[Dict[str, Any]], state: Dict[str, Any]) -> str:
    """Give the planner a bounded source window without making prose authoritative."""
    facts: List[str] = []
    character_by_id = {character["id"]: character["name"] for character in package["characters"]}
    for item in package.get("items", []):
        holder = item.get("initialHolderId")
        if holder in character_by_id:
            facts.append(f"{character_by_id[holder]}持有{item['name']}")
    for item in state.get("derivedItems", []):
        facts.append("分支登记物品：" + item["name"] + "（" + item["summary"] + "）")
    excerpts: List[str] = []
    for node in lineage[-3:]:
        narrative = node.get("narrativeText", "").strip()
        if narrative:
            excerpts.append(narrative[-3600:])
    protected_history = [item.get("description", "").strip() for item in package.get("timeline", [])]
    protected_history = [item for item in protected_history if item]
    return "\n".join([
        "已确认道具归属：" + "；".join(facts),
        "受保护的世界历史（仅用于保持因果一致；不可把尚未确认的内容直接当作人物已知事实）："
        + ("；".join(protected_history) if protected_history else "无"),
        "来源片段（只可延续，不能改写其中已出现的人物、地点、道具归属或因果）：",
        "\n\n".join(excerpts),
    ])


def state_character_locations(package: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """Bind characters to authoritative locations without story-specific names.

    Packages may declare ``narrativeGuidelines.characterLocationStateFields`` as
    ``{characterId: stateField}``. For existing packages, the conventional
    ``<character-id-prefix>LocationId`` fields are inferred when unambiguous;
    ``playerLocationId`` always belongs to ``initialState.player.characterId``.
    This lets a package opt into explicit bindings without requiring a new
    runtime schema version.
    """
    locations = by_id(package.get("locations", []))
    locations.update(by_id(state.get("derivedLocations", [])))
    character_by_id = by_id(package.get("characters", []))
    guidelines = package.get("world", {}).get("narrativeGuidelines", {})
    declared = guidelines.get("characterLocationStateFields", {})
    bindings: Dict[str, str] = {}
    if isinstance(declared, dict):
        bindings.update({character_id: field for character_id, field in declared.items()
                         if character_id in character_by_id and isinstance(field, str)})
    player_id = package.get("initialState", {}).get("player", {}).get("characterId")
    if isinstance(player_id, str) and player_id in character_by_id and "playerLocationId" in state:
        bindings.setdefault(player_id, "playerLocationId")
    for field in state:
        if not field.endswith("LocationId") or field == "playerLocationId":
            continue
        prefix = field[:-len("LocationId")].lower()
        matches = [character_id for character_id in character_by_id
                   if character_id.removeprefix("character_").split("_")[0].lower() == prefix]
        if len(matches) == 1:
            bindings.setdefault(matches[0], field)
    result: Dict[str, Dict[str, str]] = {}
    for character_id, field in bindings.items():
        location_id = state.get(field)
        if not isinstance(location_id, str) or location_id not in locations:
            continue
        result[character_by_id[character_id]["name"]] = {
            "characterId": character_id,
            "stateField": field,
            "locationId": location_id,
            "locationName": locations[location_id]["name"],
        }
    return result


def focal_character_location_field(package: Dict[str, Any], state: Dict[str, Any]) -> Optional[str]:
    """Return the package-declared state field for the focal character's location."""
    focal_id = package.get("world", {}).get("narrativeGuidelines", {}).get("focalCharacterId")
    if not isinstance(focal_id, str):
        focal_id = package.get("initialState", {}).get("player", {}).get("characterId")
    positions = state_character_locations(package, state)
    for position in positions.values():
        if position["characterId"] == focal_id:
            return position["stateField"]
    return "playerLocationId" if "playerLocationId" in state else None


def state_character_location_context(package: Dict[str, Any], state: Dict[str, Any]) -> str:
    positions = state_character_locations(package, state)
    if not positions:
        return "- StoryPackage 未声明或无法推断可校验的人物位置"
    return "\n".join(
        f"- {name}：{position['locationName']}（由 {position['stateField']} 约束）"
        for name, position in positions.items()
    )


def narration_outside_dialogue(text: str) -> str:
    return re.sub(r"[“\"][^”\"]*[”\"]", "", text)


def guard_character_final_locations(text: str, package: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Reject a final explicit placement that conflicts with authoritative state.

    A scene may include a temporary detour. Only the last explicit movement or
    placement in narration is treated as the character's final location, so a
    later return to the state-backed location remains valid. Dialogue is
    excluded: a question, rumour, or deliberate lie is not an authoritative
    placement.
    """
    locations = state_character_locations(package, state)
    registered_locations = list(by_id(package.get("locations", [])).values()) + state.get("derivedLocations", [])
    narration = narration_outside_dialogue(text)
    sentence_list = [sentence for sentence in re.split(r"[。！？\n]+", narration) if sentence.strip()]
    # Static scene-setting can be an intermediate point before the character
    # follows someone elsewhere.
    # Only an explicit arrival or return names a final location by itself.
    movement = r"(?:位于|回到|返回|回|来到|走进|进入|抵达|赶到)"
    for name, expected in locations.items():
        final_location: Optional[str] = None
        for sentence in sentence_list:
            if name not in sentence:
                continue
            for location in registered_locations:
                location_name = location.get("name")
                if not isinstance(location_name, str) or not location_name:
                    continue
                direct_move = re.search(
                    rf"{re.escape(name)}[^。！？\n]{{0,10}}{movement}[^。！？\n]{{0,4}}{re.escape(location_name)}",
                    sentence,
                )
                named_placement = re.search(
                    rf"{re.escape(location_name)}(?:里|内|中)(?:只|正|还|就)?(?:有|站着|坐着|躲着|是)?{re.escape(name)}",
                    sentence,
                )
                if direct_move or named_placement:
                    final_location = location_name
        if final_location is not None and final_location != expected["locationName"]:
            raise ValueError(
                "剧情正文与已确认状态矛盾："
                f"{name} 的最终位置应为 {expected['locationName']}，不能写为 {final_location}。"
            )


def guard_narrative(
    text: str,
    state: Dict[str, Any],
    character_details: List[Dict[str, str]],
    narrative_guidelines: Optional[Dict[str, Any]] = None,
    package: Optional[Dict[str, Any]] = None,
) -> None:
    normalized = re.sub(r"\s+", "", text)
    narration = re.sub(r"\s+", "", narration_outside_dialogue(text))
    perspective = (narrative_guidelines or {}).get("perspective", "third_person_limited")
    if perspective == "third_person_limited" and re.search(r"(?:^|[。！？；])你(?:[在正又也还将把向从的]|[\u4e00-\u9fff])", narration):
        focal_name = "焦点人物"
        if package is not None:
            focal_id = (narrative_guidelines or {}).get("focalCharacterId")
            focal_name = next((item["name"] for item in package.get("characters", []) if item["id"] == focal_id), focal_name)
        raise ValueError(f"剧情正文视角错误：叙事必须使用第三人称{focal_name}，不能把玩家写成“你”。")
    if package is not None:
        for assertion in package.get("stateModel", {}).get("narrativeAssertions", []):
            if state_matches(assertion["when"], state) and any(
                re.search(pattern, normalized) for pattern in assertion["forbiddenPatterns"]
            ):
                raise ValueError(assertion["message"])
        guard_character_final_locations(text, package, state)


def guard_source_character_names(text: str, package: Dict[str, Any], state: Dict[str, Any], additions: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> None:
    """Keep names tracked by this branch, while allowing declared additions."""
    if state["storyScope"] != "source":
        return
    additions = additions or empty_branch_additions()
    known = {character["name"] for character in package["characters"]}
    known.update(character["name"] for character in state.get("derivedCharacters", []))
    known.update(character["name"] for character in additions["characters"])
    known.update(reveal["name"] for reveal in state.get("derivedCharacterReveals", []))
    known.update(reveal["name"] for reveal in additions["characterReveals"])
    generic_suffixes = ("人", "工", "员", "者", "客", "长", "们")
    # These are pronouns, adverbs, prepositions, or structural particles, not
    # plausible starts of a Chinese personal name. Keeping them out prevents
    # phrases such as "他低头" and "又看了看" from becoming fake characters.
    non_name_prefixes = (
        "他", "她", "它", "这", "那", "其", "谁", "某", "一", "每", "各", "什么",
        "又", "再", "还", "已", "正", "就", "才", "也", "都", "仍", "不", "没",
        "被", "把", "将", "从", "在", "向", "对", "让", "使",
    )
    non_name_words = (
        "平时", "此时", "当时", "同时", "随后", "然后", "突然", "忽然",
        "慢慢", "渐渐", "依旧", "一直", "已经", "立刻", "终于", "刚刚",
    )
    # This guard is deliberately conservative. The prompt already prohibits
    # source-scope names; local validation should catch a likely new name such
    # as "秦戈", not reinterpret ordinary prose such as "但足够让" as one.
    common_surname_prefixes = frozenset(
        "王李张刘陈杨黄赵吴周徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤尹黎易常武乔贺赖龚文"
    )
    grammatical_suffixes = ("的", "地", "得")
    pattern = re.compile(r"(?:^|[。！？\n，、和与])([\u4e00-\u9fff]{2,3})(?=(?:说|问|答|喊|看|点|走|站|蹲|跟|回|抬|低|握|把|让|扶|托|转|停|沉默|开口|脸色|目光|声音|抱紧|快步))")
    for match in pattern.finditer(text):
        candidate = match.group(1)
        if candidate in known or any(candidate.startswith(name) for name in known):
            continue
        if (
            len(candidate) == 2
            and candidate[0] in common_surname_prefixes
            and candidate not in non_name_words
            and not candidate.startswith(non_name_prefixes)
            and not candidate.endswith(generic_suffixes + grammatical_suffixes)
        ):
            raise ValueError("剧情正文引入了未登记的新人物姓名: " + candidate)


def narrative_state_guardrails(package: Dict[str, Any], state: Dict[str, Any]) -> str:
    """Render the state facts a planner must preserve in its single draft."""
    rules = [
        "- 正文必须从本回合的已确认状态开始；只能描写本回合选择已经导致的变化，不能预支下一次方向的结果。",
        "- 状态中的地点、人物状态、道具归属和已发生事件均为事实；可补充合理过程、细节、新角色或新地点，但不得改变这些事实。",
    ]
    for assertion in package.get("stateModel", {}).get("narrativeAssertions", []):
        if state_matches(assertion["when"], state):
            rules.append("- " + assertion["instruction"])
    return "\n".join(rules)


def normalize_narrative_text(text: str) -> str:
    """Recover paragraph breaks double-escaped by incompatible model gateways."""
    return text.replace("\\\\r\\\\n", "\n").replace("\\\\n", "\n").replace("\\\\r", "\n")


def plain_model_narrative(content: str) -> str:
    """Reject a structured response instead of persisting it as chapter prose."""
    narrative = normalize_narrative_text(content).strip()
    if narrative.startswith(("{", "[")):
        try:
            json.JSONDecoder().raw_decode(narrative)
        except json.JSONDecodeError:
            pass
        else:
            raise LlmError("LLM 正文接口只接受小说文本，不能返回 JSON", "model_output_rejected")
    return narrative


def narrative_character_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def validate_story_arc(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LlmError("LLM 剧情结果缺少章节信息", "model_output_rejected")
    chapter = value.get("chapter")
    if not all(isinstance(value.get(key), str) and value[key].strip() for key in ("activeGoal", "currentPhase", "goalDisposition")):
        raise LlmError("LLM 剧情结果缺少主线目标或当前阶段", "model_output_rejected")
    if not isinstance(chapter, dict) or not isinstance(chapter.get("title"), str) or not chapter["title"].strip() or chapter.get("status") not in ("continuing", "complete"):
        raise LlmError("LLM 剧情结果缺少有效章节名称或章节状态", "model_output_rejected")
    return value


class NarrativeFieldStream:
    """Extract incremental narrativeText characters from a JSON object SSE stream."""
    def __init__(self, observer: Optional[Callable[[str], None]]) -> None:
        self.observer = observer
        self.buffer = ""
        self.started = False
        self.escaped = False
        self.double_escaped = False
        self.finished = False

    def feed(self, chunk: str) -> None:
        if self.finished:
            return
        self.buffer += chunk
        if not self.started:
            marker = '"narrativeText"'
            index = self.buffer.find(marker)
            if index < 0:
                self.buffer = self.buffer[-len(marker):]
                return
            colon = self.buffer.find(":", index + len(marker))
            quote = self.buffer.find('"', colon + 1) if colon >= 0 else -1
            if quote < 0:
                return
            self.started = True
            self.buffer = self.buffer[quote + 1:]
        produced: List[str] = []
        index = 0
        while index < len(self.buffer):
            character = self.buffer[index]
            if self.double_escaped:
                mapping = {"n": "\n", "r": "\r", "t": "\t"}
                if character in mapping:
                    produced.append(mapping[character])
                else:
                    produced.extend(("\\", character))
                self.double_escaped = False
            elif self.escaped:
                mapping = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/"}
                if character == "\\":
                    self.double_escaped = True
                else:
                    produced.append(mapping.get(character, character))
                self.escaped = False
            elif character == "\\":
                self.escaped = True
            elif character == '"':
                self.finished = True
                index += 1
                break
            else:
                produced.append(character)
            index += 1
        self.buffer = ""
        if produced and self.observer:
            self.observer("".join(produced))


class Planner:
    def plan(self, context: Dict[str, Any], selected: Dict[str, Any], resolved_state: Dict[str, Any], stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None, repair: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        raise NotImplementedError


class MockPlanner(Planner):
    def plan(self, context: Dict[str, Any], selected: Dict[str, Any], resolved_state: Dict[str, Any], stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None, repair: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        title = selected["title"]
        narrative = self._narrative(context["package"], selected, resolved_state)
        if narrative_character_count(narrative) < 2000:
            narrative += self._chapter_extension(context["package"], selected, resolved_state)
        next_directions = self._next(context, selected, resolved_state)
        result = plan_result(context, selected, narrative, title, next_directions, "medium")
        result["planning"]["narrativeOrigin"] = "mock_structural_fixture"
        if stream:
            stream(narrative)
        return result, None

    def _narrative(self, package: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> str:
        location = location_name(package, state)
        focal_id = package.get("world", {}).get("narrativeGuidelines", {}).get("focalCharacterId")
        focal = next((item["name"] for item in package.get("characters", []) if item["id"] == focal_id), "焦点人物")
        companions = [name for name, position in state_character_locations(package, state).items()
                      if name != focal and position["locationName"] == location]
        companion_text = "、".join(companions) if companions else "身边的人"
        node_id = next((item.get("nodeId") for item in package["story"]["narrativeGraph"]["beats"]
                        if item.get("branchState") == {key: value for key, value in state.items() if key not in BRANCH_PRIVATE_STATE_KEYS}), None)
        node = next((item for item in package["story"].get("nodes", []) if item["id"] == node_id), {})
        objective = node.get("objective", package["story"].get("longTermGoal", "当前目标"))
        pressure = node.get("pressure", {}).get("effect", "仍在累积的压力")
        return (
            f"{location}没有因为“{selected['title']}”这个决定而立刻安静下来。{focal}先停住脚步，"
            f"和{companion_text}确认眼前能够看见的路径、工具与风险；他们知道这一回合只推进已选择的事情，不能替下一步预支结果。\n\n"
            f"当前要解决的是“{objective}”。这个方向界定了本回合推进的范围，{focal}没有把它当作保证，"
            "而是把它拆成能核验的判断：谁在场，地点是否可达，已确认的线索还缺少什么，以及一旦受阻应当如何留下退路。\n\n"
            "周围的细节仍在提醒他们，叙事不能替代事实。已经发生的变化可以被看见、被讨论，也可以带来新的情绪和阻力；"
            "尚未确认的人、物、地点和结果则必须继续保持悬而未决。任何看似省事的跳跃，都会让之后的选择失去可追溯的依据。\n\n"
            f"{focal}把注意力从笼统的愿望收回到当前动作上。{companion_text}各自保留了不同的顾虑，"
            "却都同意先把能确认的部分做扎实：检查环境、说明限制、记录变化，并让新的方向真正对应一个尚未完成的问题。\n\n"
            f"场景之外的{pressure}没有消失。{focal}因此没有宣布胜利，也没有替任何人作出未经选择的承诺；"
            "他只确认这一步已经改变了什么，并把其余问题留给下一次明确的决定。"
        )

    def _chapter_extension(self, package: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> str:
        location = location_name(package, state)
        focal_id = package.get("world", {}).get("narrativeGuidelines", {}).get("focalCharacterId")
        focal = next((item["name"] for item in package.get("characters", []) if item["id"] == focal_id), "焦点人物")
        return (
            f"\n\n{location}中的光线、声音和气味都在不断提醒{focal}：环境并不会为叙事让路。"
            "他先检查能够使用的物件、可撤回的路径和身边人的反应，再决定是否继续把风险交给下一步。"
            "这种确认并不华丽，却让每一项变化都有来源，也让角色的犹豫、分歧和承担能够落在具体事实之上。\n\n"
            f"内容包为此刻声明的状态边界仍然有效。{focal}没有把这些限制当作背景说明，"
            "而是把它们带进每一次观察和对话：什么已经得到证实，什么还只是推测，什么必须等到玩家明确选择后才能发生。\n\n"
            "短暂的安静没有消除压力，反而让未解的问题显出轮廓。有人提出可能的办法，也有人指出其代价；"
            "他们因此把计划拆开，先核实最接近当前场景的细节，再保留对后续走向的判断。这样，故事可以继续推进，"
            "却不会用一句方便的结论覆盖仍未解决的因果。\n\n"
            f"{focal}听完不同意见，没有立刻选择最省力的一条路。他先让每个人说明自己真正看见了什么，"
            "哪些判断来自经验，哪些只是为了安慰彼此而作出的推断。说清这些差异并不会削弱场景的紧张，"
            "反而让每个人承担的风险变得可以辨认：有人负责留意环境变化，有人负责保管已有线索，"
            "有人则必须在条件不足时承认暂时无法继续。\n\n"
            f"在{location}里，细节从来不是装饰。脚步停在什么位置、工具是否还能使用、"
            "一句迟疑是否意味着有人隐瞒了信息，都会改变下一步的代价。"
            f"{focal}把这些变化逐一记下，不把它们夸大成答案，也不因为暂时没有答案就把它们忽略。"
            "故事由此保留了继续追问的空间，而不是把读者和角色一同推向没有依据的结论。\n\n"
            "身边的人也在这种克制中显出各自的位置。有人愿意提供帮助，却不一定能替别人承担后果；"
            "有人掌握片段信息，却仍需要被核验；有人看似沉默，也可能正在衡量自己是否该承担此前回避的责任。"
            "这些关系不会因为一次选择自动变得可靠，只能在可被看见的行动和回应中慢慢改变。\n\n"
            f"{focal}没有催促谁立刻给出承诺。他让场景保持足够的停顿，先确认眼前的变化已经被共同看见，"
            "再说明这一回合到此为止。这样做不是拖延，而是避免把下一次行动的门槛偷偷藏进已经完成的叙述里。"
            "只有当玩家明确选择新的方向，人物才会跨过那条边界，承担随之而来的新事实。\n\n"
            "远处的动静仍在持续，时间和外部压力也没有停止计算。可角色已经不再只是在等待一个偶然的转机；"
            "他们知道应当带着什么问题进入下一段场景，也知道哪些事实必须在转身前被守住。"
            "这种明确并不保证结果，却让每一次失败、让步或收获都有能够回看的原因。\n\n"
            f"{focal}又回头看了一眼{location}。那些看似无关紧要的痕迹仍然留在原处："
            "被反复使用过的边角、被匆忙挪开的物件、说到一半便停住的话。它们未必立刻构成线索，"
            "却提醒每个人，真正可靠的判断需要允许自己暂时不知道。角色可以提出怀疑，可以调整计划，"
            "也可以承认此前的选择并不充分；唯一不能做的，是为了尽快结束场景而把不确定性伪装成确定事实。\n\n"
            "于是他们把刚才发生的事重新说了一遍，确认谁看见了什么、谁承担了什么、哪些变化已经留在现场。"
            "这段复盘没有让气氛松弛，反而让原先模糊的风险有了轮廓。每个人都明白，下一次选择不仅会推动目标，"
            "也会决定关系是否值得信任、资源是否还能使用，以及后来的人将如何理解这一刻的因果。\n\n"
            f"{focal}没有要求所有人达成同一种解释。他只要求下一步能够被清楚地说出来："
            "目的是什么，边界在哪里，行动失败后谁需要负责收拾后果。这样的约定让人物仍有分歧，"
            "却不必靠误解或突然出现的万能答案维持冲突。它也给读者留下了判断的余地，"
            "让接下来的方向成为真正的选择，而不是早已被正文替代完成的程序。\n\n"
            "在重新出发之前，他们没有再增加新的事实，只把已知信息按轻重放回心里："
            "眼前能够处理的障碍、尚待核验的说法，以及一旦局势变化就必须优先回应的人。"
            "这份排序并不替代行动，却让行动拥有了清晰的起点。\n\n"
            f"{focal}最后重新确认“{selected['title']}”只完成了本回合应完成的部分。"
            "他把余下的问题留在可选择的方向里，让下一次行动决定谁去做、在哪里做，以及它究竟会改变什么。"
        )

    def _next(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
        return scripted_followup_directions(context, selected, state)


def scripted_followup_directions(
    context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Publish only StoryPackage-defined choices after a completed chapter."""
    package = context["package"]
    source_node_ref = resolve_source_node(package, context["parent"], state, selected)
    controlled = controlled_followup_directions(package, selected, source_node_ref, state)
    if controlled is not None:
        return controlled
    for template in package.get("stateModel", {}).get("mockFollowups", []):
        if state_matches(template.get("when", {}), state):
            return [branch_direction(item) for item in template["nextDirections"]]
    return []


def derived_direction(identifier: str, title: str, summary: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": identifier, "title": title, "summary": summary, "statePatch": patch}


class LlmPlanner(Planner):
    def __init__(self, gateway: OpenAICompatibleGateway, minimum_narrative_characters: int = 0) -> None:
        self.gateway = gateway
        self.minimum_narrative_characters = minimum_narrative_characters

    def plan(self, context: Dict[str, Any], selected: Dict[str, Any], resolved_state: Dict[str, Any], stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None, repair: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        raw_responses: List[str] = []
        observations: List[Dict[str, Any]] = []
        rejected_narrative_characters: Optional[int] = None
        source_node_ref = resolve_source_node(context["package"], context["parent"], resolved_state, selected)
        prompt = self._prompt(
            context, selected, resolved_state, repair,
            terminal_source_branch=controlled_followup_directions(
                context["package"], selected, source_node_ref, resolved_state,
            ) == [],
        )
        try:
            completion = self.gateway.complete_text(
                [{"role": "system", "content": "你是中文互动小说叙事者。只输出小说正文。"}, {"role": "user", "content": prompt}],
                stream,
                stream_reset,
            )
            raw_responses.append(completion.raw_response)
            observations.extend({**item, "generationStage": "initial"} for item in completion.observations)
            narrative = plain_model_narrative(completion.content)
            if not narrative:
                raise LlmError("LLM 剧情正文为空", "model_output_rejected")
            rejected_narrative_characters = narrative_character_count(narrative)
            if rejected_narrative_characters < self.minimum_narrative_characters:
                continuation_completion = self.gateway.complete_text(
                    [
                        {"role": "system", "content": "你是中文互动小说续写器。只输出小说正文。"},
                        {"role": "user", "content": self._continuation_prompt(
                            context, selected, resolved_state, narrative,
                        )},
                    ],
                    None,
                    stream_reset,
                )
                raw_responses.append(continuation_completion.raw_response)
                observations.extend({**item, "generationStage": "continuation"} for item in continuation_completion.observations)
                continuation = plain_model_narrative(continuation_completion.content)
                if not continuation:
                    raise LlmError("LLM 续写正文为空", "model_output_rejected")
                narrative += "\n\n" + continuation
                rejected_narrative_characters = narrative_character_count(narrative)
                initial_was_streamed = stream and (
                    not completion.used_transport_fallback or completion.body_was_streamed
                )
                if initial_was_streamed:
                    stream("\n\n" + continuation)
                if rejected_narrative_characters < self.minimum_narrative_characters:
                    raise LlmError(
                        f"LLM 剧情正文少于 {self.minimum_narrative_characters} 个非空白字符",
                        "model_output_rejected",
                    )
            guard_narrative(
                narrative, resolved_state, context["characterDetails"],
                context["package"]["world"].get("narrativeGuidelines"), context["package"],
            )
            guard_source_character_names(narrative, context["package"], resolved_state, empty_branch_additions())
            next_directions = scripted_followup_directions(context, selected, resolved_state)
            result = plan_result(context, selected, narrative, selected["title"], next_directions, "medium")
            result["branchAdditions"] = empty_branch_additions()
            observations.append({"attempt": 1, "outcome": "normalized", "normalization": "generated_scripted_turn_metadata"})
            if completion.used_transport_fallback and stream and not completion.body_was_streamed:
                stream(narrative)
            return result, {"operation": "branch_planner", "model": self.gateway.model, "promptVersion": "python-v0.7", "requestSummary": selected["title"], "rawResponse": "\n\n".join(raw_responses), "callObservations": observations}
        except LlmError as error:
            if error.raw_response:
                raw_responses.append(error.raw_response)
            observations.extend(error.observations)
            if rejected_narrative_characters is not None:
                observations.append({
                    "attempt": 1,
                    "outcome": "rejected",
                    "rejectedNarrativeCharacters": rejected_narrative_characters,
                })
            observations.append({"attempt": 1, "outcome": "failed", "failureKind": error.code, "error": str(error)})
        except ValueError as error:
            if rejected_narrative_characters is not None:
                observations.append({
                    "attempt": 1,
                    "outcome": "rejected",
                    "rejectedNarrativeCharacters": rejected_narrative_characters,
                })
            observations.append({"attempt": 1, "outcome": "failed", "failureKind": "model_output_rejected", "error": str(error)})
        error = LlmError("LLM Planner 未生成可用剧情：" + observations[-1]["error"], observations[-1]["failureKind"])
        error.audit = {"operation": "branch_planner", "model": self.gateway.model, "promptVersion": "python-v0.7", "requestSummary": selected["title"], "rawResponse": "\n\n".join(raw_responses), "error": observations[-1]["error"], "callObservations": observations, "rejectedNarrativeCharacters": rejected_narrative_characters}
        raise error

    def _continuation_prompt(
        self,
        context: Dict[str, Any],
        selected: Dict[str, Any],
        state: Dict[str, Any],
        narrative: str,
    ) -> str:
        current_characters = narrative_character_count(narrative)
        target_characters = max(self.minimum_narrative_characters + 200, 2200)
        state_guardrail_text = narrative_state_guardrails(context["package"], state)
        return f"""续写已经生成但篇幅不足的同一章互动小说。不得改写、概述、重复或否定既有正文；只从最后一句之后自然续写。

本回合选择：{selected['title']}。{selected['summary']}
本回合结束后的已确认状态：{json.dumps(state, ensure_ascii=False)}
当前正文已有 {current_characters} 个非空白字符。请追加约 {max(700, target_characters - current_characters)} 至 {max(1100, target_characters - current_characters + 300)} 个中文字符，使合并后的正文达到至少 {self.minimum_narrative_characters} 个非空白字符。续写必须通过新的场景、动作、对话、人物反应和下一项尚未完成的具体问题推进，不得重复已有段落或提前完成下一方向。

当前状态门槛（不可违反）：
{state_guardrail_text}

特别注意：不得新增会跨回合影响因果的命名人物、地点或物品。必须遵守 StoryPackage 声明的叙事视角，不能把焦点人物写成“你”。

已有正文：
{narrative}

提交前最后核对（优先级最高；若已有正文的任一句与此冲突，以此为准，绝不可顺着冲突继续写）：
{state_guardrail_text}

只输出续写正文，不要输出标题、JSON、说明或代码块。"""

    def _prompt(
        self,
        context: Dict[str, Any],
        selected: Dict[str, Any],
        state: Dict[str, Any],
        repair: Optional[str],
        terminal_source_branch: bool = False,
    ) -> str:
        fact_text = "\n".join("- " + fact["text"] for fact in context["package"]["world"]["immutableFacts"])
        constraint_text = "\n".join("- " + item for item in context["package"]["world"].get("globalConstraints", [])) or "- 未声明额外全局约束"
        guideline_text = "\n".join(
            "- " + item for item in context["package"]["world"].get("narrativeGuidelines", {}).get("prohibitions", [])
        ) or "- 未声明额外叙事禁则"
        guidelines = context["package"]["world"].get("narrativeGuidelines", {})
        registered_locations = context["package"].get("locations", []) + state.get("derivedLocations", [])
        location_text = "\n".join(
            "- " + location["name"] + "：" + location.get("description", location.get("summary", ""))
            for location in registered_locations
        ) or "- 暂无已登记地点"
        focal_id = guidelines.get("focalCharacterId")
        focal_name = next((item["name"] for item in context["package"].get("characters", []) if item["id"] == focal_id), "主角")
        perspective = guidelines.get("perspective", "third_person_limited")
        perspective_instruction = (
            f"以第三人称限知、连贯的场景推进来写，焦点人物一律称“{focal_name}”或“他/她”，绝不可使用“你”指代焦点人物；"
            if perspective == "third_person_limited"
            else f"严格遵循 StoryPackage 声明的 {perspective} 视角与焦点人物“{focal_name}”；"
        )
        character_text = "\n".join("- " + item["name"] + "：" + item["detail"] for item in context["characterDetails"]) or "- 暂无额外已确认外观细节"
        continuity_text = source_continuity_context(context["package"], context.get("lineage", [context["parent"]]), state)
        state_guardrail_text = narrative_state_guardrails(context["package"], state)
        character_location_text = state_character_location_context(context["package"], state)
        selected_patch = {key: value for key, value in selected["statePatch"].items() if key != "derivedAdditions"}
        state_transition = {
            key: {"from": context["parent"]["branchState"].get(key), "to": state.get(key)}
            for key in selected_patch
        }
        state_patch_targets = {key: state.get(key) for key in selected_patch}
        parent_arc = context["parent"].get("storyArc") or {}
        arc_instruction = ""
        if parent_arc.get("arcId"):
            arc_instruction = (
                f"当前大方向：{parent_arc['activeGoal']}。当前小方向：{selected['title']}。"
                "本回合只完成这个小方向对应的一章；运行时会依据 StoryPackage 的完成条件决定"
                "接下来继续给出小方向，还是切换到新的大方向；章节标题和方向均由运行时生成。"
            )
        terminal_instruction = (
            "本回合抵达已声明的源分支终点。正文应收束当前即时危机，不能安排还需要本分支立刻执行的新行动，"
            "也不能用未兑现的道具、约定或命令制造假菜单。"
            "如需继续，只能在本分支结束后另行创建独立衍生故事；正文不得提及命令或系统。"
            if terminal_source_branch
            else "本回合尚未到达声明的源分支终点；只给出与当前状态一致、下一回合可执行的方向。"
        )
        return f"""根据已确认状态继续中文互动小说。不能改写 StoryPackage，不能凭空让未获得的证据、救援或列车状态发生。

玩家本回合选择：{selected['title']}。{selected['summary']}
本回合必须在正文中实际完成的状态变化：{json.dumps(state_transition, ensure_ascii=False)}
本回合结束后的已确认状态：{json.dumps(state, ensure_ascii=False)}
正文只可完成上列 `from` 到 `to` 的状态变化；未列出的状态字段必须保持父节点状态，不能把下一方向的开门、救出人物、取得证据、降低水位或阻止放行提前写为已发生。状态、物品、人物、地点、章节名、摘要和后续方向均由运行时处理，你不能输出或声明它们。
声明式状态断言：满足条件的 StoryPackage 叙事断言均已列入下方状态门槛。
{arc_instruction}
原著不可变事实：\n{fact_text}
世界全局约束：\n{constraint_text}
叙事禁则：\n{guideline_text}
已确认人物细节（新增人物可以暂不透露身份）：\n{character_text}
已确认人物最终位置（正文若明确写到这些人物移动或出现，最后一次明确定位必须与此表一致；中途绕路可以写，但必须在正文结束前回到表中位置）：\n{character_location_text}
已登记地点（涉及这些地点时直接使用，不得重复登记）：\n{location_text}
上一节点摘要：{context['parent']['summary']}
受控连续性上下文：\n{continuity_text}

写作要求：{perspective_instruction}正文必须实际写出玩家所选行动，以及上列状态变化如何发生；不能只写准备、讨论、寻找或尝试，却把结果留给下一回合。再写出选择造成的阻碍、人物反应和新的具体问题。请规划 2,200 至 2,800 个中文字符，以留出高于 2,000 字硬下限的余量；按非空白字符自行计数，少于 2,200 时必须继续补充新的场景、动作、对话或后果，不能重复句子或概述。不要替玩家完成后续选择。

分支扩展规则：StoryPackage 固定不改写。正文不得首次引入会跨回合影响行动、取证或因果的命名人物、地点、物品、工具、文件、标记或线索；只可使用上文已经登记的实体，或以不命名、不可复用的场景细节表达。新实体必须等待用户通过后续的结构化确认流程登记。受保护的世界历史中的时间、保管、隐藏、交接和已发生因果均不能被替换；除非本回合的已确认状态变化明确表示该对象发生转移，否则不得为了铺垫而补写一次未确认的转移。正文使用真实段落换行，不能输出字面量 \\n 或 \\r\\n。

必须遵守的当前状态门槛（优先级最高）：
{state_guardrail_text}

分支终点规则（优先级最高）：{terminal_instruction}

提交前自行核对正文不得违反上述门槛。只输出一次完整小说正文，不要标题、JSON、解释、自评、草稿版本或重写过程。
{('上次草稿的问题：' + repair + '。请只修复这些问题并保留合理剧情。') if repair else ''}
"""


class NarrativeReviewer:
    """Advisory review inspired by multi-stage novel pipelines, never state authority."""
    def review(self, context: Dict[str, Any], result: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        return {"decision": "accept", "issues": []}


class LlmNarrativeReviewer(NarrativeReviewer):
    def __init__(self, gateway: OpenAICompatibleGateway) -> None:
        self.gateway = gateway

    def review(self, context: Dict[str, Any], result: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        prompt = f"""审阅下列互动小说草稿。你不是状态权威，不能提议新增事实；只判断是否存在应记录的可读性问题，不要要求系统自动重写。检查：因果连贯、人物动机、细节延续、场景感、是否把未完成的玩家选择写成已完成、是否存在有意义的下一步。允许新人物暂不揭示身份。\n已确认状态：{json.dumps(state, ensure_ascii=False)}\n上文摘要：{context['parent']['summary']}\n草稿：{result['narrativeText']}\n只输出 {{\"decision\":\"accept\"或\"revise\",\"issues\":[\"不超过三条具体问题\"]}}。"""
        completion = self.gateway.complete_json([{"role": "system", "content": "你是中文小说一致性与可读性审阅器。只输出 JSON。"}, {"role": "user", "content": prompt}])
        review = parse_json_content(completion.content)
        if review.get("decision") not in ("accept", "revise") or not isinstance(review.get("issues", []), list):
            raise LlmError("LLM 质量审阅响应无效")
        return review


class DirectionEvaluator:
    def __init__(self, package: Optional[Dict[str, Any]] = None) -> None:
        self.package = package

    def _forbidden_world_fact(self, player_direction: str) -> Optional[Dict[str, str]]:
        if self.package is None:
            return None
        normalized = normalize(player_direction)
        supernatural_terms = ("魔法", "法术", "超能力", "瞬移", "复活")
        if not any(term in normalized for term in supernatural_terms):
            return None
        for fact in self.package.get("world", {}).get("immutableFacts", []):
            text = normalize(str(fact.get("text", "")))
            if "超自然" in text or "魔法" in text or "法术" in text:
                return fact
        return None

    @staticmethod
    def _direction_terms(direction: Dict[str, Any]) -> List[str]:
        source = "".join(str(direction.get(key, "")) for key in ("title", "summary", "suggestedInput"))
        terms = [direction["title"]]
        for segment in re.findall(r"[\u4e00-\u9fff]{2,}", source):
            for width in range(2, min(5, len(segment) + 1)):
                terms.extend(segment[index:index + width] for index in range(len(segment) - width + 1))
        return list(dict.fromkeys(term for term in terms if len(normalize(term)) >= 2))

    def evaluate(self, parent: Dict[str, Any], player_direction: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        normalized = normalize(player_direction)
        if not normalized:
            return {"kind": "clarification_needed", "message": "请用一句话说明希望优先推动哪条剧情方向。"}, None
        forbidden_fact = self._forbidden_world_fact(player_direction)
        if forbidden_fact is not None:
            return {"kind": "rejected", "message": "当前故事不允许以超自然能力直接解决障碍。请在既有世界规则内说明行动。", "citations": [{"kind": "immutable_fact", "ref": forbidden_fact["id"]}]}, None
        matches = []
        for direction in parent["nextDirections"]:
            words = self._direction_terms(direction)
            positions = [normalized.find(normalize(word)) for word in words if normalize(word) in normalized]
            if positions:
                matches.append((min(positions), direction))
        if len(matches) == 1 or (len(matches) > 1 and sorted(matches, key=lambda item: item[0])[0][0] != sorted(matches, key=lambda item: item[0])[1][0]):
            direction = sorted(matches, key=lambda item: item[0])[0][1]
            return {"kind": "accepted", "directionId": direction["id"], "rationale": f"输入优先推进“{direction['title']}”。"}, None
        if matches:
            return {"kind": "clarification_needed", "message": "这段描述同时涉及多条方向，请明确本回合优先哪一条。"}, None
        return {"kind": "clarification_needed", "message": "当前无法将这段描述映射到已公布的剧情方向。可优先考虑：" + "、".join(item["title"] for item in parent["nextDirections"]) + "。"}, None


class LlmDirectionEvaluator(DirectionEvaluator):
    """Maps free text to only already-published directions."""
    def __init__(self, gateway: OpenAICompatibleGateway) -> None:
        super().__init__()
        self.gateway = gateway

    def _ordered_direction_fallback(self, parent: Dict[str, Any], player_direction: str) -> Optional[Dict[str, Any]]:
        """Resolve a clear first action when the model unnecessarily asks again."""
        normalized = normalize(player_direction)
        if not normalized or any(marker in normalized for marker in ("还是", "或者", "抑或", "二选一", "任选", "哪个")):
            return None
        evaluation, _ = super().evaluate(parent, player_direction)
        return evaluation if evaluation["kind"] == "accepted" else None

    def evaluate(self, parent: Dict[str, Any], player_direction: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        directions = [{"id": item["id"], "title": item["title"], "summary": item["summary"]} for item in parent["nextDirections"]]
        prompt = f"""将玩家的中文自由文本判定为当前一个已公布方向，或要求澄清/拒绝。不可发明方向，不可把多个目标合为一步；若多个目标并列，选择文本中最先明确的一个。拒绝超自然、瞬移、复活等违背世界设定的内容。\n当前方向：{json.dumps(directions, ensure_ascii=False)}\n玩家输入：{player_direction}\n只输出 {{\"kind\":\"accepted\",\"directionId\":\"已公布ID\",\"rationale\":\"\"}}，或 {{\"kind\":\"clarification_needed\",\"message\":\"\"}}，或 {{\"kind\":\"rejected\",\"message\":\"\",\"citations\":[{{\"kind\":\"immutable_fact\",\"ref\":\"fact_no_supernatural\"}}]}}。"""
        try:
            completion = self.gateway.complete_json([{"role": "system", "content": "你是互动小说方向判定器。只输出 JSON。"}, {"role": "user", "content": prompt}])
        except LlmError as error:
            error.audit = {"operation": "direction_evaluator", "model": self.gateway.model, "promptVersion": "python-v0.1", "requestSummary": player_direction[:300], "rawResponse": None, "error": str(error), "callObservations": error.observations}
            raise
        evaluation = parse_json_content(completion.content)
        kind = evaluation.get("kind")
        if kind == "accepted":
            if evaluation.get("directionId") not in {item["id"] for item in directions} or not isinstance(evaluation.get("rationale"), str):
                raise LlmError("LLM 方向判定引用了未公布方向或缺少理由")
        elif kind in ("clarification_needed", "rejected"):
            if not isinstance(evaluation.get("message"), str) or not evaluation["message"].strip():
                raise LlmError("LLM 方向判定缺少说明")
        else:
            raise LlmError("LLM 方向判定 kind 无效")
        fallback = self._ordered_direction_fallback(parent, player_direction) if kind == "clarification_needed" else None
        observations = list(completion.observations)
        if fallback is not None:
            evaluation = {
                "kind": "accepted",
                "directionId": fallback["directionId"],
                "rationale": "输入包含后续目标，已按最先明确的已公布方向推进。",
            }
            observations.append({
                "attempt": 1,
                "outcome": "normalized",
                "normalization": "resolved_ordered_multi_goal_to_first_published_direction",
                "directionId": fallback["directionId"],
            })
        audit = {"operation": "direction_evaluator", "model": self.gateway.model, "promptVersion": "python-v0.1", "requestSummary": player_direction[:300], "rawResponse": completion.raw_response, "callObservations": observations}
        return evaluation, audit


def normalize(value: str) -> str:
    return re.sub(r"[\s，。！？、；：“”‘’（）()【】]", "", value).lower()


def validate_direction(direction: Dict[str, Any]) -> Dict[str, Any]:
    if not all(isinstance(direction.get(key), str) and direction[key].strip() for key in ("id", "title", "summary")) or not isinstance(direction.get("statePatch"), dict) or not direction["statePatch"]:
        raise LlmError("LLM 后续方向缺少 id、title、summary 或 statePatch")
    return {key: copy.deepcopy(direction[key]) for key in ("id", "title", "summary", "canonicalBeatId", "followupBeatId", "rejoinTargetId", "statePatch") if key in direction}


def location_name(package: Dict[str, Any], state: Dict[str, Any]) -> str:
    locations = by_id(package["locations"])
    locations.update(by_id(state.get("derivedLocations", [])))
    field = focal_character_location_field(package, state)
    location_id = state.get(field) if field else None
    return locations.get(location_id, {"name": location_id or "当前地点"})["name"]


def plan_result(context: Dict[str, Any], selected: Dict[str, Any], narrative: str, phase: str, directions: List[Dict[str, Any]], confidence: str) -> Dict[str, Any]:
    parent_arc = context["parent"].get("storyArc") or {}
    return {
        "narrativeText": narrative, "summary": phase + "已推进，新的风险和选择仍然存在。", "factDeltas": [],
        "openThreads": list(dict.fromkeys(context["parent"].get("openThreads", []) + ["尚未完成的当前目标"]))[-6:], "nextDirections": directions,
        "storyArc": {"activeGoal": parent_arc.get("activeGoal", selected["title"]), "currentPhase": phase, "goalDisposition": "completed" if not directions else "continued", "chapter": {"title": parent_arc.get("chapter", {}).get("title", context["package"]["metadata"]["title"] + "·共创篇"), "status": "complete" if not directions else "continuing"}},
        "planning": {"citations": [{"kind": "branch_node", "ref": context["parent"]["id"], "rationale": "承接已确认的上一节点。"}], "confidence": confidence, "stateChangeProposals": []},
    }


class CoCreationService:
    def __init__(self, package: Dict[str, Any], store: SessionStore, planner: Planner, evaluator: Optional[DirectionEvaluator] = None, reviewer: Optional[NarrativeReviewer] = None) -> None:
        self.package, self.store, self.planner, self.evaluator = package, store, planner, evaluator or DirectionEvaluator(package)
        if self.evaluator.package is None:
            self.evaluator.package = package
        self.reviewer = reviewer or NarrativeReviewer()

    def start(self, session_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        contract = create_contract(self.package, session_id)
        root = entry_node(self.package, contract)
        self.store.save_contract(contract)
        return contract, self.store.create_branch_root(session_id, root)

    def current(self, session_id: str) -> Dict[str, Any]:
        nodes = self.store.branches(session_id)
        if not nodes:
            raise ValueError("共创会话没有分支节点")
        return nodes[-1]

    def _select_arc(
        self, session_id: str, parent_id: str, parent: Dict[str, Any], selected: Dict[str, Any],
        player_direction: Optional[str], request_id: Optional[str], contract: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Record a macro-plot choice, then expose its first chapter choices."""
        arc = arc_by_id(self.package, selected["arcId"])
        source_node_ref = parent.get("sourceNodeRef") or contract["entryNodeId"]
        phases = phase_directions_for_arc(self.package, source_node_ref, parent["branchState"], arc["id"])
        if not phases:
            raise ValueError("当前状态下该大方向没有可执行的小方向: " + arc["title"])
        result = {
            "kind": "arc_selection",
            "sourceNodeRef": source_node_ref,
            "branchState": copy.deepcopy(parent["branchState"]),
            "narrativeText": "大方向已确立：" + arc["title"] + "。接下来请选择本章要推进的小方向。",
            "summary": arc["summary"],
            "factDeltas": [],
            "openThreads": copy.deepcopy(parent.get("openThreads", [])),
            "nextDirections": phases,
            "canonicalRelation": "on_line",
            "selectedDirectionId": selected["id"],
            "playerDirection": player_direction,
            "requestId": request_id,
            "storyArc": {
                "arcId": arc["id"],
                "activeGoal": arc["title"],
                "currentPhase": "等待选择本章推进",
                "completedPhaseIds": [],
                "goalDisposition": "started",
                "menuLevel": "phase",
                "chapter": {"title": "", "status": "pending"},
            },
            "planning": {"citations": [{"kind": "story_arc", "ref": arc["id"], "rationale": "玩家已选择本阶段大方向。"}], "confidence": "high", "stateChangeProposals": []},
            "createdAt": timestamp(),
        }
        return self.store.append_branch(session_id, parent_id, result)

    def continue_direction(self, session_id: str, parent_id: str, direction_id: str, player_direction: Optional[str] = None, stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None, on_generation_start: Optional[Callable[[], None]] = None, request_id: Optional[str] = None) -> Dict[str, Any]:
        if request_id:
            existing = self.store.find_branch_request(session_id, request_id)
            if existing is not None:
                if (
                    existing["parentId"] != parent_id
                    or existing["selectedDirectionId"] != direction_id
                    or existing.get("playerDirection") != player_direction
                ):
                    raise ValueError("同一 requestId 不能用于不同的父分支、方向或玩家输入")
                return existing
        contract = self.store.contract(session_id)
        parent = self.store.branch(session_id, parent_id)
        selected = next((item for item in parent["nextDirections"] if item["id"] == direction_id), None)
        if selected is None:
            raise ValueError("当前分支不存在可选方向: " + direction_id)
        if selected.get("directionLevel") == "arc":
            return self._select_arc(session_id, parent_id, parent, selected, player_direction, request_id, contract)
        lineage = self.store.lineage(session_id, parent_id)
        provisional = copy.deepcopy(parent["branchState"])
        # Route resolution is based on a provisional patch. The actual patch is
        # then checked against source-state invariants.
        provisional.update({key: value for key, value in selected["statePatch"].items() if key != "derivedAdditions"})
        source_node_ref = resolve_source_node(self.package, parent, provisional, selected)
        resolved = apply_branch_patch(self.package, parent["branchState"], selected["statePatch"], source_node_ref)
        rejoin = resolve_rejoin(self.package, parent.get("sourceNodeRef") or contract["entryNodeId"], parent["openThreads"], parent["branchState"], source_node_ref, resolved, selected) if resolved["storyScope"] == "source" else None
        if rejoin and not any(item["canonicalRelation"] == "diverged" for item in lineage):
            raise ValueError("只有已偏离原著的分支可以汇合: " + rejoin["id"])
        canonical = None
        if player_direction and player_direction.startswith("选择方向：") and all(item["canonicalRelation"] == "on_line" for item in lineage) and selected.get("canonicalBeatId"):
            beat = find_beat(self.package, selected["canonicalBeatId"])
            if beat:
                if beat["nodeId"] != source_node_ref:
                    raise ValueError("规范方向场景路线与目标锚点不一致: " + selected["id"])
                if initial_branch_state(beat) != resolved:
                    raise ValueError("规范方向状态快照与补丁不一致: " + selected["id"])
                canonical = {"narrativeText": beat.get("sourceExcerpt", {}).get("text", beat["narrativeAnchor"]), "summary": beat["summary"], "factDeltas": [{"id": "fact_" + beat["id"], "source": "source", "summary": beat["summary"]}], "openThreads": beat["openThreads"], "nextDirections": [branch_direction(item) for item in beat["nextDirections"]], "storyArc": parent.get("storyArc"), "planning": {"citations": [{"kind": "canonical_node", "ref": beat["nodeId"], "rationale": "规范分支复用原著。"}], "confidence": "high", "stateChangeProposals": []}}
        context = {"package": self.package, "contract": contract, "parent": parent, "lineage": lineage, "characterDetails": confirmed_character_details(self.package, lineage)}
        audit = None
        if canonical is not None:
            result, relation = canonical, "on_line"
        else:
            try:
                if on_generation_start:
                    on_generation_start()
                result, audit = self.planner.plan(context, selected, resolved, stream, stream_reset)
            except LlmError as error:
                failure_audit = getattr(error, "audit", None)
                if failure_audit:
                    self.store.save_audit(session_id, failure_audit)
                raise
            relation = "rejoined" if rejoin else "diverged"
            branch_additions = result.get("branchAdditions", empty_branch_additions())
            if has_branch_additions(branch_additions):
                resolved = apply_branch_patch(
                    self.package, resolved, {"derivedAdditions": branch_additions}, source_node_ref,
                )
            guard_narrative(
                result["narrativeText"], resolved, context["characterDetails"],
                self.package["world"].get("narrativeGuidelines"), self.package,
            )
            review = self.reviewer.review(context, result, resolved)
            if review.get("decision") == "revise" and audit is not None:
                # Review is advisory. A second full generation makes the player
                # wait and replaces already displayed prose, so record it
                # without silently rewriting the selected turn.
                audit["review"] = review
        if audit:
            self.store.save_audit(session_id, audit)
        if rejoin:
            target_beat = find_beat(self.package, rejoin["targetBeatId"])
            result = {**result, "nextDirections": [branch_direction(item) for item in target_beat["nextDirections"]]}
        story_arc, next_arc_directions = advance_story_arc(
            self.package, parent.get("storyArc"), selected, resolved, result.get("storyArc"),
        )
        if story_arc is not None:
            result["storyArc"] = story_arc
            if next_arc_directions is not None:
                result["nextDirections"] = next_arc_directions
            elif story_arc.get("arcId"):
                result["nextDirections"] = [
                    {**direction, "directionLevel": "phase", "arcId": story_arc["arcId"]}
                    for direction in result["nextDirections"]
                ]
        assert_published_directions(self.package, source_node_ref, resolved, result["nextDirections"])
        node = {**result, "sourceNodeRef": source_node_ref, "branchState": resolved, "canonicalRelation": relation, "selectedDirectionId": direction_id, "playerDirection": player_direction, "requestId": request_id, "createdAt": timestamp()}
        stored = self.store.append_branch(session_id, parent_id, node)
        if resolved["storyScope"] == "derived":
            derived = self.store.derived(session_id)
            if derived:
                derived = copy.deepcopy(derived)
                derived["revisions"].append({"branchId": stored["id"], "summary": stored["summary"], "addedAt": timestamp()})
                self.store.update_derived(derived)
        return stored

    def continue_free_text(
        self,
        session_id: str,
        parent_id: str,
        player_direction: str,
        stream: Optional[Callable[[str], None]] = None,
        stream_reset: Optional[Callable[[str], None]] = None,
        on_direction_accepted: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_generation_start: Optional[Callable[[], None]] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        parent = self.store.branch(session_id, parent_id)
        if request_id:
            existing = self.store.find_direction_request(session_id, request_id)
            if existing is not None:
                if existing["parentBranchId"] != parent_id or existing["playerDirection"] != player_direction:
                    raise ValueError("同一 requestId 不能用于不同的父分支或玩家输入")
                if existing["kind"] != "accepted":
                    return {key: value for key, value in existing.items() if key not in ("id", "sessionId", "parentBranchId", "playerDirection", "requestId", "createdAt")}
                node = self.store.find_branch_request(session_id, request_id)
                if node is None:
                    raise ValueError("同一 requestId 的方向已判定，但正文未生成；请使用新的 requestId 重试")
                return {"kind": "accepted", "node": node, "rationale": existing["rationale"]}
        try:
            evaluation, audit = self.evaluator.evaluate(parent, player_direction)
        except LlmError as error:
            failure_audit = getattr(error, "audit", None)
            if failure_audit:
                self.store.save_audit(session_id, failure_audit, parent_id)
            raise
        if audit:
            self.store.save_audit(session_id, audit, parent_id)
        self.store.save_direction_evaluation(session_id, parent_id, player_direction, evaluation, request_id)
        if evaluation["kind"] != "accepted":
            return evaluation
        if on_direction_accepted:
            on_direction_accepted(evaluation)
        node = self.continue_direction(
            session_id, parent_id, evaluation["directionId"], player_direction, stream, stream_reset,
            on_generation_start, request_id,
        )
        return {"kind": "accepted", "node": node, "rationale": evaluation["rationale"]}

    def begin_derivative(self, session_id: str, parent_id: str, goal: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        parent = self.store.branch(session_id, parent_id)
        if parent["nextDirections"]:
            raise ValueError("请先完成当前分支；只有没有后续方向的节点可以创建衍生故事包")
        policy = self.package.get("stateModel", {}).get("derivativeEntry")
        if not isinstance(policy, dict):
            raise ValueError("StoryPackage 未声明衍生故事入口规则")
        if not state_matches(policy["requiredState"], parent["branchState"]):
            raise ValueError(policy["message"])
        if self.store.derived(session_id):
            raise ValueError("当前会话已经创建衍生故事包")
        state = copy.deepcopy(parent["branchState"])
        state.update(copy.deepcopy(policy["statePatch"]))
        next_direction = copy.deepcopy(policy["nextDirection"])
        entry = {
            "id": "branch_" + str(uuid.uuid4()), "kind": "derived_entry", "parentId": parent_id,
            "sourceNodeRef": parent.get("sourceNodeRef"), "branchState": state,
            "selectedDirectionId": "direction_begin_derivative", "playerDirection": goal.strip(),
            "narrativeText": policy["narrativeText"], "summary": policy["summary"],
            "factDeltas": [{"id": "fact_derivative_entry", "source": "user", "summary": "衍生目标：" + goal.strip()}],
            "openThreads": copy.deepcopy(policy["openThreads"]), "nextDirections": [next_direction],
            "canonicalRelation": "diverged",
            "storyArc": {"activeGoal": goal.strip(), "currentPhase": policy["currentPhase"], "goalDisposition": "started", "chapter": {"title": policy["chapterTitle"], "status": "continuing"}},
            "planning": {"citations": [{"kind": "branch_node", "ref": parent_id, "rationale": "衍生故事继承分叉结果。"}], "confidence": "high", "stateChangeProposals": []}, "createdAt": timestamp(),
        }
        package = {"id": "derived_" + str(uuid.uuid4()), "sessionId": session_id, "sourcePackageRef": {"id": self.package["id"], "version": self.package["version"]}, "forkBranchId": parent_id, "title": self.package["metadata"]["title"] + "·衍生篇", "goal": goal.strip(), "createdAt": timestamp(), "revisions": []}
        self.store.save_derived(package)
        return package, self.store.create_derived_entry(session_id, parent_id, entry)
