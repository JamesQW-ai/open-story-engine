"""Co-creation tree, immutable source reuse and optional LLM quality review."""

from __future__ import annotations

import copy
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
    ranks = {
        "tangStatus": ["missing", "located", "rescued"],
        "evidenceStatus": ["unsecured", "secured"],
        "waterLevel": ["rising", "lowered"],
        "signalRoomStatus": ["locked", "opened"],
        "trainStatus": ["pending_release", "held", "departed"],
        "derivativeStage": ["inactive", "setup", "active"],
    }
    for key, order in ranks.items():
        if next_state[key] not in order:
            raise ValueError(f"{key} 不支持状态值: {next_state[key]}")
        if current[key] not in order:
            raise ValueError(f"当前状态包含不支持的 {key} 值: {current[key]}")
        if order.index(next_state[key]) < order.index(current[key]):
            raise ValueError(f"{key} 不能倒退: {current[key]} -> {next_state[key]}")
    if current["storyScope"] != next_state["storyScope"]:
        raise ValueError("剧情方向不能改变故事包范围")
    if current["storyScope"] == "derived" and next_state["derivedTurn"] != current["derivedTurn"] + 1:
        raise ValueError("衍生剧情回合必须按顺序推进")
    if current["trainStatus"] == "held" and next_state["trainStatus"] == "departed":
        raise ValueError("已阻止放行的列车不能重新离站")
    known_locations = {location["id"] for location in package["locations"]} | {location["id"] for location in next_state["derivedLocations"]}
    if any(next_state[key] not in known_locations for key in ("playerLocationId", "tangLocationId", "jiangLocationId")):
        raise ValueError("共创状态引用了不存在的地点")
    if next_state["storyScope"] == "source":
        if next_state["tangStatus"] == "rescued" and next_state["tangLocationId"] != "location_waiting_hall":
            raise ValueError("唐栖获救后必须回到候车厅")
        if next_state["tangStatus"] != "rescued" and next_state["tangLocationId"] != "location_signal_tunnel":
            raise ValueError("唐栖未获救时必须仍在信号维修隧道区域")
    elif next_state["tangStatus"] == "rescued" and next_state["tangLocationId"] != next_state["playerLocationId"]:
        raise ValueError("衍生故事中唐栖获救后必须与许川处于同一地点")
    return next_state


def branch_direction(direction: Dict[str, Any]) -> Dict[str, Any]:
    return {key: copy.deepcopy(direction[key]) for key in ("id", "title", "summary", "canonicalBeatId", "rejoinTargetId", "statePatch") if key in direction}


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
    return {
        "id": "branch_" + str(uuid.uuid4()), "kind": "source_entry", "sourceNodeRef": contract["entryNodeId"],
        "branchState": initial_branch_state(beat), "narrativeText": beat.get("sourceExcerpt", {}).get("text", beat["narrativeAnchor"]),
        "summary": beat["summary"], "factDeltas": [{"id": "fact_source_entry", "source": "source", "summary": "原著前史已继承"}],
        "openThreads": beat["openThreads"], "nextDirections": [branch_direction(item) for item in beat["nextDirections"]],
        "canonicalRelation": "on_line", "planning": {"citations": [{"kind": "canonical_node", "ref": contract["entryNodeId"], "rationale": "共创从原著节点进入。"}], "confidence": "high", "stateChangeProposals": []}, "createdAt": timestamp(),
    }


def resolve_source_node(package: Dict[str, Any], parent: Dict[str, Any], state: Dict[str, Any], selected: Dict[str, Any]) -> str:
    if state["storyScope"] == "derived":
        return parent.get("sourceNodeRef") or package["story"]["startNodeId"]
    source = parent.get("sourceNodeRef") or package["story"]["startNodeId"]
    for route in package["story"].get("sceneRoutes", []):
        if route["fromNodeId"] == source and route["fromLocationId"] == parent["branchState"]["playerLocationId"] and route["toLocationId"] == state["playerLocationId"]:
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
    if not all(rejoin_thread_is_open(thread, parent_open_threads, parent_state) for thread in target["requiredOpenThreads"]):
        raise ValueError("汇合目标缺少必要未解线索: " + target["id"])
    beat = find_beat(package, target["targetBeatId"])
    if beat["nodeId"] != target_node_id:
        raise ValueError("汇合目标场景与受控路线不一致: " + target["id"])
    if initial_branch_state(beat) != resolved_state:
        raise ValueError("汇合目标状态不兼容: " + target["id"])
    return {"id": target["id"], "targetBeatId": target["targetBeatId"]}


def rejoin_thread_is_open(required_thread: str, open_threads: List[str], state: Dict[str, Any]) -> bool:
    """Match declared rejoin prerequisites without treating reader-facing prose as IDs."""
    if required_thread in open_threads:
        return True
    normalized = normalize(required_thread)
    if "证据" in normalized:
        return state["evidenceStatus"] == "unsecured"
    if "唐栖" in normalized or "救援" in normalized:
        return state["tangStatus"] != "rescued"
    if "水位" in normalized or "积水" in normalized:
        return state["waterLevel"] == "rising"
    if "信号室" in normalized:
        return state["signalRoomStatus"] == "locked"
    if "列车" in normalized or "放行" in normalized:
        return state["trainStatus"] == "pending_release"
    return False


def assert_published_directions(package: Dict[str, Any], source_node_ref: str, state: Dict[str, Any], directions: List[Dict[str, Any]]) -> None:
    """Validate announced choices before a player can choose them on the next turn."""
    parent = {"sourceNodeRef": source_node_ref, "branchState": state}
    for direction in directions:
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


SOURCE_FOLLOWUP_BEATS = {
    # Divergent source choices have no canonical beat themselves. Their
    # subsequent menus are nevertheless declared by a StoryPackage beat.
    "direction_rescue_first": "beat_tunnel_without_proof",
    "direction_lower_water_without_proof": "beat_valve_without_proof",
    "direction_open_signal_room_without_proof": "beat_ending_rescue_without_proof",
    "direction_return_for_records": "beat_evidence_secured",
}


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
    beat_id = selected.get("canonicalBeatId") or SOURCE_FOLLOWUP_BEATS.get(selected["id"])
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
    if state.get("hasLockerToken"):
        facts.append("许川已取得十七号柜铜牌")
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
    # Static scene-setting (for example, "姜序在候车厅尽头停下") can be
    # an intermediate point before the character follows someone elsewhere.
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
                    rf"{re.escape(location_name)}(?:里|内|中|外|门前)(?:[，、：: ]{{0,3}}){re.escape(name)}",
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
        raise ValueError("剧情正文视角错误：叙事必须使用第三人称许川，不能把玩家写成“你”。")
    if state["trainStatus"] == "pending_release" and any(word in normalized for word in ("列车已经离站", "末班列车离开", "列车驶离", "列车发车")):
        raise ValueError("剧情正文与已确认状态矛盾：列车仍在等待放行，正文不能描写列车已经进站、离站或发车。")
    if state["signalRoomStatus"] == "locked" and (
        "唐栖走出信号室" in normalized
        or re.search(r"唐栖.{0,24}(?:从|经由).{0,16}(?:信号室|通风口).{0,12}(?:爬出|钻出|走出|逃出|离开)", normalized)
        or re.search(r"唐栖.{0,40}(?:跨出|走出|逃出|离开).{0,8}(?:门|门口|门缝)", normalized)
        or re.search(r"唐栖.{0,20}(?:在|位于|待在|站在|蹲在|出现在|进入|来到|走进|走到).{0,12}设备间", normalized)
        or re.search(r"设备间(?:里|内|中).{0,20}唐栖", normalized)
    ):
        raise ValueError("剧情正文与已确认状态矛盾：信号室仍锁闭，唐栖不能自行离开或出现在设备间。")
    if state["tangStatus"] != "rescued" and any(word in normalized for word in ("唐栖已经获救", "唐栖回到候车厅")):
        raise ValueError("剧情正文与已确认状态矛盾：唐栖尚未获救。")
    if state["evidenceStatus"] == "unsecured":
        secured_evidence_terms = ("证据已经保全", "证据已保全", "录音已经取得", "录音已取得", "证据有了", "拿到证据", "录下来了")
        recorder_is_held = re.search(
            r"(?:拿到|取得|取出|持有|携带|带走|收好|贴身收好|保全|攥着|拿着)[^。！？；，、]{0,12}录音笔"
            r"|录音笔[^。！？；，、]{0,12}(?:拿到|取得|取出|持有|携带|带走|收好|贴身收好|保全)",
            normalized,
        )
        if any(term in normalized for term in secured_evidence_terms) or recorder_is_held:
            raise ValueError("剧情正文与已确认状态矛盾：证据尚未取得。")
    if package is not None:
        guard_character_final_locations(text, package, state)
    # Reject only direct known-detail inversions. New appearances stay allowed;
    # identity may be intentionally withheld until later chapters.
    for detail in character_details:
        if "深蓝" in detail["detail"] and (detail["name"] + "穿着浅色") in normalized:
            raise ValueError(f"剧情正文与已确认人物细节矛盾：{detail['name']} 的衣着描述不一致。")


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


def narrative_state_guardrails(state: Dict[str, Any]) -> str:
    """Render the state facts a planner must preserve in its single draft."""
    rules = [
        "- 正文必须从本回合的已确认状态开始；只能描写本回合选择已经导致的变化，不能预支下一次方向的结果。",
        "- 状态中的地点、人物状态、道具归属和已发生事件均为事实；可补充合理过程、细节、新角色或新地点，但不得改变这些事实。",
    ]
    if state.get("trainStatus") == "pending_release":
        rules.append("- 列车仍在等待放行；不能写列车已经进站、离站、发车或驶离。")
    if state.get("signalRoomStatus") == "locked" and state.get("tangStatus") != "rescued":
        rules.append(
            "- 唐栖仍被困在锁闭的信号室内。她可以隔门回应、敲击，或通过既有门缝与外部交流；"
            "不能自行走出、爬出、钻出、逃出或离开信号室，也不能出现在设备间、通风口外或其他地点。"
        )
    if state.get("tangStatus") != "rescued":
        rules.append("- 唐栖尚未获救；不能写她已获救、回到候车厅或与许川一同行动。")
    if state.get("evidenceStatus") == "unsecured":
        rules.append(
            "- 证据尚未取得。可写人物寻找、讨论或怀疑录音和记录，"
            "但不能写任何人已拿到、取出、持有、带走、收好、保全录音笔、录音或可用证据。"
        )
    return "\n".join(rules)


def normalize_narrative_text(text: str) -> str:
    """Recover paragraph breaks double-escaped by incompatible model gateways."""
    return text.replace("\\\\r\\\\n", "\n").replace("\\\\n", "\n").replace("\\\\r", "\n")


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
        if stream:
            stream(narrative)
        return result, None

    def _narrative(self, package: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> str:
        direction_id = selected["id"]
        if direction_id == "direction_rescue_first":
            return (
                "姜序把通行证攥在掌心，先穿过候车厅最暗的一段走廊。许川跟在他身后，没有再回头看陈砚。"
                "消防通道的锁舌果然没有扣紧，门缝里涌出的冷气带着铁锈和海水的味道，像把整座车站的雨声都压进了地下。\n\n"
                "维修隧道的坡道向下延伸，积水已经没过鞋面。姜序用应急灯扫过墙上的旧编号，提醒许川避开右侧的电缆槽；"
                "许川则把铜牌按进内袋，知道自己现在没有录音、没有图纸，只有赶在水位继续上涨前确认唐栖仍能回应。\n\n"
                "拐过弯后，绿漆铁门出现在隧道尽头。门没有打开，门缝里却透出一线微弱的灯光。许川敲了三下，里面很快传来同样的回应。"
                "唐栖仍被困在锁闭的信号室内，但她还醒着；这让救援从猜测变成了眼前必须解决的障碍。\n\n"
                "姜序站在门侧，望着被水汽锈住的滑栓，声音低得几乎被水声盖过：先让水退下去，才有余地处理这扇门。"
                "许川看向隧道入口，远处的广播仍在催促列车放行。他们已经进入救援路线，却还没有取得足以对抗陈砚的证据。"
            )
        if direction_id in ("direction_lower_water_with_proof", "direction_lower_water_without_proof"):
            evidence_note = "录音与维修图纸被许川贴身收好，" if state["evidenceStatus"] == "secured" else "站务室里的录音和维修图纸仍未取得，"
            return (
                "姜序跪在锈死的手动阀旁，用肩膀顶住湿滑的管壁。许川扶住阀杆，两人合力把卡滞的金属一点点拧开。"
                "起初只有沉闷的摩擦声，随后排水沟深处传来一阵持续的轰响，积水终于开始向更低处退去。\n\n"
                "水面从膝侧退到脚背，露出被淹没的检修编号和散落的碎石。许川没有把这当成救援已经完成；"
                "信号室的门仍锁着，唐栖隔着门板的回应提醒他们，真正的阻碍还在前面。\n\n"
                + evidence_note
                + "许川因此不能把下一步只当成一次破拆。他要决定是马上处理滑栓，还是冒险让其中一人折返，"
                "在列车放行压力再次逼近前补上这条证据缺口。\n\n"
                "姜序擦去手背上的污水，把应急灯照向那扇绿漆铁门。门前的水流已经缓下来，隧道却没有变得安全。"
                "许川听见门后一次短促的敲击，便知道这一回合争取到的是时间，而不是答案。"
            )
        if direction_id == "direction_return_for_records":
            return (
                "许川把隧道里的应急灯交给姜序，沿原路折返站务室。积水在身后发出持续的回响，提醒他这趟折返并不是放弃救援，"
                "而是要带回足以让陈砚无法再把危险说成误会的东西。\n\n"
                "十七号柜仍开着。许川取出录音笔和维修图纸，把调度记录上的异常时间逐项拍下；每确认一项，"
                "他都更清楚唐栖为何会被困在信号室。证据终于有了可保全的形状，也让下一次回到隧道不再只是凭猜测行事。\n\n"
                "候车厅方向传来模糊的广播，列车依旧等待放行。姜序已经先回到候车厅，"
                "替唐栖争取的那点余地仍需要许川尽快带着证据去兑现。\n\n"
                "许川把录音笔贴进内袋，关上柜门。现在他既要把证据带回人前，也必须尽快回到信号室门前；"
                "这两件事都不能再交给下一场雨来决定。"
            )
        if direction_id == "direction_derivative_start":
            return (
                "站务室的门在身后合上，雨声被隔成一层遥远的底噪。许川把湿透的记录摊在桌上，唐栖坐到调度终端旁，"
                "先把还能辨认的时间和编号抄进新的笔记本。两人都没有把这一夜当成已经结束的故事。\n\n"
                "唐栖的手背仍留着擦伤，却坚持逐页核对采购单。许川替她换掉被水浸软的纸垫，发现那些看似零散的数字"
                "开始指向同一条未核实的责任链：有人知道设备会坏，也有人一直从记录里抹去警告。\n\n"
                "站外的列车还停在雨里，候车厅偶尔传来乘客的说话声。这里暂时安全，但安全并不等于可以遗忘；"
                "他们需要先把手里已有的事实整理清楚，才能决定是否追查匿名线索。\n\n"
                "许川合上最上面那份记录，和唐栖确认下一步只做一件事：从可核验的细节开始，"
                "不让新的目标反过来改写已经留下的证据。"
            )
        location = location_name(package, state)
        return (
            f"{location}里的声响没有因为许川作出“{selected['title']}”的选择而停下来。"
            "他先重新确认脚下的路线和仍未完成的目标，再把决定告诉身边的人，避免把下一步写成已经解决的结局。\n\n"
            "潮湿的空气里混着机油、雨水和旧纸张的味道。有人守着入口，有人留意远处的动静；"
            "许川从这些细节里判断出，眼前能推进的并不是一句笼统的承诺，而是一项必须承担后果的行动。\n\n"
            "行动推进后，已经确认的状态没有被叙述替换：尚未拿到的证据仍需取得，尚未打开的门仍需处理，"
            "还在等待的列车也没有突然离开。新的压力因此变得更清晰，而不是被一段漂亮的收束掩盖。\n\n"
            "许川把注意力放回下一项具体选择上。他知道下一回合应该决定谁去做、去哪里做，以及完成后会改变什么，"
            "而不是让故事跳过那些仍在眼前的障碍。"
        )

    def _chapter_extension(self, package: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> str:
        location = location_name(package, state)
        water = {"rising": "仍在上涨", "lowered": "已经退到较低处"}.get(state.get("waterLevel"), "仍需确认")
        evidence = "仍未取得" if state.get("evidenceStatus") == "unsecured" else "已经保全"
        train = {"pending_release": "仍在等待放行", "held": "已经被暂缓放行", "departed": "已经离站"}.get(state.get("trainStatus"), "状态未明")
        tang = (
            "唐栖已经脱离即时危险，但她留下的记录仍需要被完整核验。"
            if state.get("tangStatus") == "rescued"
            else "唐栖仍未获救；每一次隔门的回应都只能证明她还在，不能替代真正的救援。"
        )
        signal = "信号室仍是锁闭状态，滑栓和门框的处理不能被跳过。" if state.get("signalRoomStatus") == "locked" else "信号室已经打开，眼前的重点转为带人离开、保存记录与应对站内反应。"
        return (
            f"\n\n{location}里的光线并不稳定。许川停下脚步，让眼睛适应应急灯忽明忽暗的节奏；"
            "墙面上的盐霜、管道上的水珠和地面被冲散的纸屑，都让这里看上去像一处被人刻意遗忘的角落。"
            "他没有急着替任何人下结论，只先辨认脚下的水流、能够借力的墙面，以及一旦发生意外可以退回的方向。\n\n"
            f"“{selected['title']}”并没有让此前的压力自动消失。积水{water}，证据{evidence}，"
            f"列车{train}。这些状态并不是背景说明，而是每个人说话、停顿和犹豫时都绕不开的限制。"
            "许川把它们逐一记在心里，避免因为眼前出现一点转机，就把尚未兑现的结果写成既成事实。\n\n"
            "姜序的手始终没有离开通行证和应急灯。他不再像候车厅里那样只用沉默回避问题，"
            "却也没有突然变成能解决一切的人。每当远处传来金属受力的轻响，他都会先听一会儿，"
            "再说明哪一段管线可能仍在通电、哪一处积水不能贸然踩过去。许川听得出，这些提醒既是经验，也是迟来的愧疚。\n\n"
            f"{tang}许川没有把这句话说出口，只把注意力放到能够确认的细节上："
            "回应从哪里传来，声音是否还清醒，门、阀门或通道究竟需要怎样的处理。这样做不会让局面立刻变好，"
            "却能让下一步不至于建立在误听、猜测或陈砚留下的说辞上。\n\n"
            "隧道深处偶尔传来水泵吃力运转的闷响，像一台随时会停下的旧机器。许川想起唐栖最早那段语音，"
            "也想起陈砚在候车厅里过分平稳的语气。两件事之间仍隔着许多没有核实的记录，"
            "但他已经能看见其中共同的轮廓：有人知道风险存在，却希望所有人继续把它当成小事。\n\n"
            f"{signal}许川没有伸手去做尚未选定的下一步，也没有让姜序替他作出承诺。"
            "他们先检查现有条件，确认眼前动作真正会改变什么，随后才把新的问题摆到台面上。"
            "这让场景里的紧张感没有消退，反而变得具体：时间在流失，水声在靠近，而每一次选择都会留下可以追溯的后果。\n\n"
            "远处的广播被厚墙削得模糊，只能听见断续的电流杂音。站内其他人未必了解这里发生了什么，"
            "也知道陈砚可能正在利用这种信息差拖延、施压或掩盖。可他不能为了追上对方的节奏而牺牲已经确认的事实；"
            "无论是救人还是取证，最后都必须经得起回看。\n\n"
            "他看向姜序。两人之间没有再多说漂亮的话，只把下一项能够完成的行动说清楚：谁留意危险，"
            "谁承担工具和路线，完成以后又该怎样确认结果。这样的分工并不保证安全，却让他们不必靠夸张的勇敢掩盖准备不足。\n\n"
            "姜序把应急灯抬高了一点，光圈沿着墙根缓慢扫过去，照出几道新旧不一的划痕。"
            "他没有断言那些痕迹属于谁，只指出其中有几道的高度与水线接近，可能是有人在视线很差的时候留下的记号。"
            "许川蹲下去看了片刻，先确认记号没有通向明显的危险处，才示意姜序把这个细节记住。"
            "他们都明白，一条可疑的痕迹不是答案；它最多只能换来一个值得继续核验的问题。\n\n"
            "“别把我当成已经知道路的人。”姜序忽然说。他的声音压得很低，几乎被管道里的杂音盖住，"
            "“我只能告诉你以前的检修习惯，至于现在还有没有人改动过，必须亲眼看见。”"
            "许川点头，没有把这句话当成推脱。正因为姜序承认自己的边界，他们才可以把能确认的部分和只能猜测的部分分开，"
            "也避免在焦急时把一段旧经验误当成当前的保证。\n\n"
            "短暂的安静里，水滴一下一下落在金属边缘，声音比先前更清楚。许川重新检查手边能够使用的物件，"
            "确认它们没有被水浸坏，也确认没有谁为了图快而把不该带走的东西塞进口袋。"
            "这些琐碎的确认看似拖慢了脚步，却让两人保留了随时后退、互相说明和纠正判断的余地。"
            "在这样狭窄的地方，真正危险的往往不是走得慢，而是误以为自己已经没有必要再检查。\n\n"
            "许川把目光从脚边移开，等姜序也准备好之后，才让两人的注意力重新回到当前方向。"
            "他们没有承诺接下来一定会顺利，也没有把尚未发生的转机提前说成胜利；"
            "但至少此刻，他们知道该带着什么问题继续、该避开什么风险，以及一旦听见新的回应应该先确认什么。"
            "这份克制没有让雨夜变得轻松，却让前方的路不再只剩下盲目的冲动。\n\n"
            "雨声仍从看不见的出口灌进来。许川没有把眼前的进展误认成结局，而是重新核对仍受状态约束的人、地点和证据，"
            "把注意力留给下一项真正能够改变局面的选择。他也明白，任何看似省时的捷径都会留下新的代价；"
            "只有把已经确认的进展守住，下一步的风险才值得承担。"
        )

    def _next(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
        package = context["package"]
        source_node_ref = resolve_source_node(package, context["parent"], state, selected)
        controlled = controlled_followup_directions(package, selected, source_node_ref, state)
        if controlled is not None:
            return controlled
        if state["storyScope"] == "derived":
            turn = state["derivedTurn"]
            if turn == 1:
                return [derived_direction("direction_derivative_follow_lead", "暂离车站并跟进线索", "带唐栖到安全住处整理细节，再核验匿名线索。", {"derivedTurn": 2, "playerLocationId": "location_derivative_shelter", "tangLocationId": "location_derivative_shelter", "derivedAdditions": {"locations": [{"id": "location_derivative_shelter", "name": "临时落脚处", "summary": "离开车站后整理线索的住处。"}], "characters": [{"id": "character_unidentified_contact", "name": "未署名联系人", "summary": "暂不透露身份的线索提供者。"}], "characterReveals": []}})]
            if turn == 2:
                return [derived_direction("direction_derivative_ask_identity", "核验联系人身份", "先判断新线索是否可信，并决定是否接受对方透露的名字。", {"derivedTurn": 3, "derivedAdditions": {"locations": [], "characters": [], "characterReveals": [{"characterId": "character_unidentified_contact", "name": "罗峥", "summary": "自称掌握相似项目编号线索的人；背景与动机仍待核验。"}]}})]
            if turn == 3:
                return [derived_direction("direction_derivative_verify_clue", "核验初步线索", "带着已知细节赴约，先核验项目编号与责任链。", {"derivedTurn": 4, "playerLocationId": "location_derivative_meeting_point", "tangLocationId": "location_derivative_meeting_point", "derivedAdditions": {"locations": [{"id": "location_derivative_meeting_point", "name": "约定会面处", "summary": "许川、唐栖与罗峥核验线索的会面地点。"}], "characters": [], "characterReveals": []}})]
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
        controlled_directions = controlled_followup_directions(
            context["package"], selected, source_node_ref, resolved_state,
        )
        observer = NarrativeFieldStream(stream)
        prompt = self._prompt(
            context, selected, resolved_state, repair,
            terminal_source_branch=controlled_directions == [],
        )
        try:
            completion = self.gateway.complete_json(
                [{"role": "system", "content": "你是中文互动小说叙事规划器。只输出一个 JSON 对象。"}, {"role": "user", "content": prompt}],
                observer.feed if stream else None,
                stream_reset,
            )
            raw_responses.append(completion.raw_response)
            observations.extend({**item, "generationStage": "initial"} for item in completion.observations)
            result = parse_json_content(completion.content)
            if not isinstance(result.get("narrativeText"), str) or not result["narrativeText"].strip():
                raise LlmError("LLM 剧情结果缺少 narrativeText", "model_output_rejected")
            result["narrativeText"] = normalize_narrative_text(result["narrativeText"])
            rejected_narrative_characters = narrative_character_count(result["narrativeText"])
            if rejected_narrative_characters < self.minimum_narrative_characters:
                continuation_completion = self.gateway.complete_json(
                    [
                        {"role": "system", "content": "你是中文互动小说续写器。只输出一个 JSON 对象。"},
                        {"role": "user", "content": self._continuation_prompt(
                            context, selected, resolved_state, result["narrativeText"],
                        )},
                    ],
                    None,
                    stream_reset,
                )
                raw_responses.append(continuation_completion.raw_response)
                observations.extend({**item, "generationStage": "continuation"} for item in continuation_completion.observations)
                continuation_result = parse_json_content(continuation_completion.content)
                continuation = continuation_result.get("narrativeContinuation")
                if not isinstance(continuation, str) or not continuation.strip():
                    raise LlmError("LLM 剧情续写结果缺少 narrativeContinuation", "model_output_rejected")
                continuation = normalize_narrative_text(continuation).strip()
                result["narrativeText"] += "\n\n" + continuation
                rejected_narrative_characters = narrative_character_count(result["narrativeText"])
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
            branch_additions = validate_branch_additions(context["package"], resolved_state, result.get("branchAdditions"))
            state_with_additions = resolved_state
            if has_branch_additions(branch_additions):
                state_with_additions = apply_branch_patch(
                    context["package"], resolved_state, {"derivedAdditions": branch_additions}, source_node_ref,
                )
            result["branchAdditions"] = branch_additions
            guard_narrative(
                result["narrativeText"], state_with_additions, context["characterDetails"],
                context["package"]["world"].get("narrativeGuidelines"), context["package"],
            )
            guard_source_character_names(result["narrativeText"], context["package"], state_with_additions, branch_additions)
            result["summary"] = result.get("summary") if isinstance(result.get("summary"), str) and result["summary"].strip() else selected["summary"]
            result["factDeltas"] = result.get("factDeltas") if isinstance(result.get("factDeltas"), list) else []
            result["openThreads"] = [item for item in result.get("openThreads", []) if isinstance(item, str) and item.strip()]
            if not result["openThreads"]:
                result["openThreads"] = list(context["parent"].get("openThreads", []))
            result["planning"] = result.get("planning") if isinstance(result.get("planning"), dict) else {"citations": [], "confidence": "medium", "stateChangeProposals": []}
            model_directions = valid_model_directions(
                context["package"], source_node_ref, state_with_additions, result.get("nextDirections"), observations, 1,
            )
            if model_directions:
                result["nextDirections"] = merge_followup_directions(
                    model_directions, controlled_directions, observations, 1,
                )
            elif controlled_directions is not None:
                result["nextDirections"] = controlled_directions
                observations.append({"attempt": 1, "outcome": "normalized", "normalization": "replaced_invalid_model_directions_with_storypackage_templates"})
            elif isinstance(result.get("nextDirections"), list) and not result["nextDirections"]:
                result["nextDirections"] = []
            else:
                raise LlmError("LLM 未提供可发布的后续方向", "model_output_rejected")
            try:
                result["storyArc"] = validate_story_arc(result.get("storyArc"))
            except LlmError:
                result["storyArc"] = fallback_story_arc(context, selected, result["nextDirections"])
                observations.append({"attempt": 1, "outcome": "normalized", "normalization": "generated_local_story_arc"})
            if controlled_directions == []:
                chapter = result["storyArc"]["chapter"]
                result["storyArc"] = {
                    **result["storyArc"],
                    "goalDisposition": "completed",
                    "chapter": {**chapter, "status": "complete"},
                }
                observations.append({"attempt": 1, "outcome": "normalized", "normalization": "completed_storypackage_terminal_arc"})
            if completion.used_transport_fallback and stream and not completion.body_was_streamed:
                stream(result["narrativeText"])
            return result, {"operation": "branch_planner", "model": self.gateway.model, "promptVersion": "python-v0.6", "requestSummary": selected["title"], "rawResponse": "\n\n".join(raw_responses), "callObservations": observations}
        except LlmError as error:
            observations.extend(error.observations)
            observations.append({"attempt": 1, "outcome": "failed", "failureKind": error.code, "error": str(error)})
        except ValueError as error:
            observations.append({"attempt": 1, "outcome": "failed", "failureKind": "model_output_rejected", "error": str(error)})
        error = LlmError("LLM Planner 未生成可用剧情：" + observations[-1]["error"], observations[-1]["failureKind"])
        error.audit = {"operation": "branch_planner", "model": self.gateway.model, "promptVersion": "python-v0.6", "requestSummary": selected["title"], "rawResponse": "\n\n".join(raw_responses), "error": observations[-1]["error"], "callObservations": observations, "rejectedNarrativeCharacters": rejected_narrative_characters}
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
        state_guardrail_text = narrative_state_guardrails(state)
        return f"""续写已经生成但篇幅不足的同一章互动小说。不得改写、概述、重复或否定既有正文；只从最后一句之后自然续写。

本回合选择：{selected['title']}。{selected['summary']}
本回合结束后的已确认状态：{json.dumps(state, ensure_ascii=False)}
当前正文已有 {current_characters} 个非空白字符。请追加约 {max(700, target_characters - current_characters)} 至 {max(1100, target_characters - current_characters + 300)} 个中文字符，使合并后的正文达到至少 {self.minimum_narrative_characters} 个非空白字符。续写必须通过新的场景、动作、对话、人物反应和下一项尚未完成的具体问题推进，不得重复已有段落或提前完成下一方向。

当前状态门槛（不可违反）：
{state_guardrail_text}

特别注意：若 `evidenceStatus` 为 `unsecured`，录音笔、录音、维修图纸和任何可用证据都仍未取得；只能提及其位置或风险，绝不可写任何人已拿到、查看、持有、使用、保全或带走它们。不得新增会跨回合影响因果的命名人物、地点或物品。以第三人称许川继续，不能使用“你”。

已有正文：
{narrative}

只输出 JSON：{{"narrativeContinuation":""}}。"""

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
        state_guardrail_text = narrative_state_guardrails(state)
        evidence_rule = (
            "本回合 `evidenceStatus` 固定为 `unsecured`。录音笔、录音、维修图纸和任何可用证据都仍未被取得；"
            "只能写它们的位置、线索或尚待取得的风险，绝不可写任何人已拿到、查看、持有、使用、保全或带走它们。"
            if state.get("evidenceStatus") == "unsecured"
            else "本回合证据状态按上列已确认状态继续，不能虚构新的保全或遗失。"
        )
        character_location_text = state_character_location_context(context["package"], state)
        selected_patch = {key: value for key, value in selected["statePatch"].items() if key != "derivedAdditions"}
        state_transition = {
            key: {"from": context["parent"]["branchState"].get(key), "to": state.get(key)}
            for key in selected_patch
        }
        state_patch_targets = {key: state.get(key) for key in selected_patch}
        terminal_instruction = (
            "本回合抵达已声明的源分支终点。正文应收束当前即时危机，不能安排还需要本分支立刻执行的新行动，"
            "也不能用未兑现的道具、约定或命令制造假菜单。`nextDirections` 必须为 []，"
            "`storyArc.goalDisposition` 必须为 `completed`，`storyArc.chapter.status` 必须为 `complete`。"
            "如需继续，只能在本分支结束后另行创建独立衍生故事；正文不得提及命令或系统。"
            if terminal_source_branch
            else "本回合尚未到达声明的源分支终点；只给出与当前状态一致、下一回合可执行的方向。"
        )
        schema = '{"narrativeText":"", "summary":"", "factDeltas":[], "openThreads":[""], "branchAdditions":{"locations":[],"characters":[],"items":[],"characterReveals":[]}, "nextDirections":[{"id":"","title":"","summary":"","statePatch":{}}], "storyArc":{"activeGoal":"","currentPhase":"","goalDisposition":"continued","chapter":{"title":"","status":"continuing"}}, "planning":{"citations":[{"kind":"branch_node","ref":"' + context["parent"]["id"] + '","rationale":""}],"confidence":"medium","stateChangeProposals":[]}}'
        return f"""根据已确认状态继续中文互动小说。不能改写 StoryPackage，不能凭空让未获得的证据、救援或列车状态发生。

玩家本回合选择：{selected['title']}。{selected['summary']}
本回合必须在正文中实际完成的状态变化：{json.dumps(state_transition, ensure_ascii=False)}
本回合结束后的已确认状态：{json.dumps(state, ensure_ascii=False)}
`nextDirections[].statePatch` 只使用目标原始值：{json.dumps(state_patch_targets, ensure_ascii=False)}。上面的 `from`/`to` 对象只用于说明本回合的状态变化，绝不可复制进 `statePatch`；除 `derivedAdditions` 外，任何 `statePatch` 字段都不得是对象或数组，字段值必须与当前状态中的同名字段类型一致。
当前证据边界（不可违反）：{evidence_rule}
原著不可变事实：\n{fact_text}
世界全局约束：\n{constraint_text}
叙事禁则：\n{guideline_text}
已确认人物细节（新增人物可以暂不透露身份）：\n{character_text}
已确认人物最终位置（正文若明确写到这些人物移动或出现，最后一次明确定位必须与此表一致；中途绕路可以写，但必须在正文结束前回到表中位置）：\n{character_location_text}
已登记地点（涉及这些地点时直接使用，不得重复登记）：\n{location_text}
上一节点摘要：{context['parent']['summary']}
受控连续性上下文：\n{continuity_text}

写作要求：{perspective_instruction}正文必须实际写出玩家所选行动，以及上列状态变化如何发生；不能只写准备、讨论、寻找或尝试，却把结果留给下一回合。再写出选择造成的阻碍、人物反应和新的具体问题。请规划 2,200 至 2,800 个中文字符的 `narrativeText`，以留出高于 2,000 字硬下限的余量；提交 JSON 前按非空白字符自行计数，少于 2,200 时必须继续补充新的场景、动作、对话或后果，不能重复句子或概述。不要替玩家完成后续选择。公开方向必须是下一步可执行的单一行动。每个 `statePatch` 只能包含相对“已确认状态”发生变化的最少字段，绝不可复制整份当前状态；至少改变一个受支持的状态值，且每个状态字段必须使用已确认状态中的枚举值。

分支扩展规则：StoryPackage 固定不改写，但当前分支可以自然出现新地点、新人物、新物品或延后揭示的身份。若正文第一次以名称写出新人物，必须在 `branchAdditions.characters` 同回合登记其 `character_` 前缀 id、名称和简述；若首次引入会跨回合影响行动、取证或因果的物品、工具、文件、标记或线索，必须在 `branchAdditions.items` 同回合登记其 `item_` 前缀 id、名称和简述，后续只能按该登记继续使用或处置，不能重新定义其来历或能力。新地点仅在它是与已登记地点不同、将跨回合复用或成为人物位置/方向目的地的独立场所时，才可在 `branchAdditions.locations` 同回合登记其 `location_` 前缀 id、名称和简述。既有地点内的通道、角落、入口、台阶、门前、低洼段或设备区域只是场景细节，不能注册为新地点，也不得用近义名称重复登记已有地点。身份暂不公开时可先用匿名称谓登记人物，后续通过 `characterReveals` 只揭示一次真实名字。后续方向可以在 `statePatch.derivedAdditions` 中登记将于该选择发生时出现的实体，并在同一补丁中引用新地点。没有新增实体时，`locations`、`characters`、`items`、`characterReveals` 必须全部输出空数组。受保护的世界历史中的时间、保管、隐藏、交接和已发生因果均不能被替换；除非本回合的已确认状态变化明确表示该对象发生转移，否则不得为了铺垫而补写一次未确认的转移。所有新设定都必须服从上面的原著事实、世界全局约束与叙事禁则。正文使用真实段落换行，不能输出字面量 \\n 或 \\r\\n。`storyArc.chapter.title` 必须给本回合一个简洁中文章节名。

必须遵守的当前状态门槛（优先级最高）：
{state_guardrail_text}

分支终点规则（优先级最高）：{terminal_instruction}

提交前在本次请求内自行核对：`narrativeText`、`summary`、`nextDirections` 和 `statePatch` 都不得违反上述门槛；若发现冲突，请在输出前改写为仍能推进剧情的版本。只输出一次完整 JSON，不要解释、自评、草稿版本或重写过程。
{('上次草稿的问题：' + repair + '。请只修复这些问题并保留合理剧情。') if repair else ''}
按此 JSON 格式输出：{schema}"""


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
    signals = {
        "direction_find_token": ["十七号", "铜牌", "储物柜", "线索"], "direction_secure_evidence": ["站务室", "证据", "录音", "调度"],
        "direction_rescue_first": ["救援", "救人", "唐栖", "姜序", "信号室", "隧道"], "direction_verify_records": ["核实", "记录", "录音", "保全"],
        "direction_lower_water_with_proof": ["排水", "水位", "手动阀"], "direction_lower_water_without_proof": ["排水", "水位", "手动阀"],
        "direction_open_signal_room_with_proof": ["打开信号室", "滑栓", "救出"], "direction_open_signal_room_without_proof": ["打开信号室", "滑栓", "救出"],
    }

    def evaluate(self, parent: Dict[str, Any], player_direction: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        normalized = normalize(player_direction)
        if not normalized:
            return {"kind": "clarification_needed", "message": "请用一句话说明希望优先推动哪条剧情方向。"}, None
        if any(word in normalized for word in ("魔法", "法术", "超能力", "瞬移", "复活")):
            return {"kind": "rejected", "message": "当前故事不允许以超自然能力直接解决障碍。请在既有世界规则内说明行动。", "citations": [{"kind": "immutable_fact", "ref": "fact_no_supernatural"}]}, None
        matches = []
        for direction in parent["nextDirections"]:
            words = self.signals.get(direction["id"], [direction["title"]])
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
        self.gateway = gateway

    def evaluate(self, parent: Dict[str, Any], player_direction: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        directions = [{"id": item["id"], "title": item["title"], "summary": item["summary"]} for item in parent["nextDirections"]]
        prompt = f"""将玩家的中文自由文本判定为当前一个已公布方向，或要求澄清/拒绝。不可发明方向，不可把多个目标合为一步；若多个目标并列，选择文本中最先明确的一个。拒绝超自然、瞬移、复活等违背世界设定的内容。\n当前方向：{json.dumps(directions, ensure_ascii=False)}\n玩家输入：{player_direction}\n只输出 {{\"kind\":\"accepted\",\"directionId\":\"已公布ID\",\"rationale\":\"\"}}，或 {{\"kind\":\"clarification_needed\",\"message\":\"\"}}，或 {{\"kind\":\"rejected\",\"message\":\"\",\"citations\":[{{\"kind\":\"immutable_fact\",\"ref\":\"fact_no_supernatural\"}}]}}。"""
        completion = self.gateway.complete_json([{"role": "system", "content": "你是互动小说方向判定器。只输出 JSON。"}, {"role": "user", "content": prompt}])
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
        audit = {"operation": "direction_evaluator", "model": self.gateway.model, "promptVersion": "python-v0.1", "requestSummary": player_direction[:300], "rawResponse": completion.raw_response, "callObservations": completion.observations}
        return evaluation, audit


def normalize(value: str) -> str:
    return re.sub(r"[\s，。！？、；：“”‘’（）()【】]", "", value).lower()


def validate_direction(direction: Dict[str, Any]) -> Dict[str, Any]:
    if not all(isinstance(direction.get(key), str) and direction[key].strip() for key in ("id", "title", "summary")) or not isinstance(direction.get("statePatch"), dict) or not direction["statePatch"]:
        raise LlmError("LLM 后续方向缺少 id、title、summary 或 statePatch")
    return {key: copy.deepcopy(direction[key]) for key in ("id", "title", "summary", "canonicalBeatId", "rejoinTargetId", "statePatch") if key in direction}


def location_name(package: Dict[str, Any], state: Dict[str, Any]) -> str:
    locations = by_id(package["locations"])
    locations.update(by_id(state.get("derivedLocations", [])))
    return locations.get(state["playerLocationId"], {"name": state["playerLocationId"]})["name"]


def plan_result(context: Dict[str, Any], selected: Dict[str, Any], narrative: str, phase: str, directions: List[Dict[str, Any]], confidence: str) -> Dict[str, Any]:
    parent_arc = context["parent"].get("storyArc") or {}
    return {
        "narrativeText": narrative, "summary": phase + "已推进，新的风险和选择仍然存在。", "factDeltas": [],
        "openThreads": list(dict.fromkeys(context["parent"].get("openThreads", []) + ["尚未完成的当前目标"]))[-6:], "nextDirections": directions,
        "storyArc": {"activeGoal": parent_arc.get("activeGoal", selected["title"]), "currentPhase": phase, "goalDisposition": "completed" if not directions else "continued", "chapter": {"title": parent_arc.get("chapter", {}).get("title", "雨夜候车室·共创篇"), "status": "complete" if not directions else "continuing"}},
        "planning": {"citations": [{"kind": "branch_node", "ref": context["parent"]["id"], "rationale": "承接已确认的上一节点。"}], "confidence": confidence, "stateChangeProposals": []},
    }


class CoCreationService:
    def __init__(self, package: Dict[str, Any], store: SessionStore, planner: Planner, evaluator: Optional[DirectionEvaluator] = None, reviewer: Optional[NarrativeReviewer] = None) -> None:
        self.package, self.store, self.planner, self.evaluator = package, store, planner, evaluator or DirectionEvaluator()
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

    def continue_direction(self, session_id: str, parent_id: str, direction_id: str, player_direction: Optional[str] = None, stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None, on_generation_start: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
        contract = self.store.contract(session_id)
        parent = self.store.branch(session_id, parent_id)
        selected = next((item for item in parent["nextDirections"] if item["id"] == direction_id), None)
        if selected is None:
            raise ValueError("当前分支不存在可选方向: " + direction_id)
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
        assert_published_directions(self.package, source_node_ref, resolved, result["nextDirections"])
        node = {**result, "sourceNodeRef": source_node_ref, "branchState": resolved, "canonicalRelation": relation, "selectedDirectionId": direction_id, "playerDirection": player_direction, "createdAt": timestamp()}
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
    ) -> Dict[str, Any]:
        parent = self.store.branch(session_id, parent_id)
        evaluation, audit = self.evaluator.evaluate(parent, player_direction)
        if audit:
            self.store.save_audit(session_id, audit, parent_id)
        self.store.save_direction_evaluation(session_id, parent_id, player_direction, evaluation)
        if evaluation["kind"] != "accepted":
            return evaluation
        if on_direction_accepted:
            on_direction_accepted(evaluation)
        node = self.continue_direction(
            session_id, parent_id, evaluation["directionId"], player_direction, stream, stream_reset,
            on_generation_start,
        )
        return {"kind": "accepted", "node": node, "rationale": evaluation["rationale"]}

    def begin_derivative(self, session_id: str, parent_id: str, goal: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        parent = self.store.branch(session_id, parent_id)
        if parent["nextDirections"]:
            raise ValueError("请先完成当前分支；只有没有后续方向的节点可以创建衍生故事包")
        if parent["branchState"]["tangStatus"] != "rescued":
            raise ValueError("唐栖获救后才能创建衍生故事包")
        if self.store.derived(session_id):
            raise ValueError("当前会话已经创建衍生故事包")
        state = copy.deepcopy(parent["branchState"])
        state.update({"storyScope": "derived", "derivativeStage": "setup"})
        entry = {"id": "branch_" + str(uuid.uuid4()), "kind": "derived_entry", "parentId": parent_id, "sourceNodeRef": parent.get("sourceNodeRef"), "branchState": state, "selectedDirectionId": "direction_begin_derivative", "playerDirection": goal.strip(), "narrativeText": "临潮站的灯还亮着，但这一夜已经收束。许川和唐栖没有替以后下结论；玩家写下的目标会成为独立衍生故事的起点。新的地点、人物、物品与身份揭示只会在后续回合逐步进入，不会改写原始故事包。", "summary": "玩家从已完成分支创建独立衍生故事包。", "factDeltas": [{"id": "fact_derivative_entry", "source": "user", "summary": "衍生目标：" + goal.strip()}], "openThreads": ["衍生故事的第一步"], "nextDirections": [derived_direction("direction_derivative_start", "前往站务室展开衍生篇", "与唐栖前往站务室整理尚未结案的记录，再按设定目标展开第一章。", {"derivativeStage": "active", "derivedTurn": 1, "playerLocationId": "location_station_office", "tangLocationId": "location_station_office"})], "canonicalRelation": "diverged", "storyArc": {"activeGoal": goal.strip(), "currentPhase": "确定衍生故事的第一步", "goalDisposition": "started", "chapter": {"title": "衍生篇·起点", "status": "continuing"}}, "planning": {"citations": [{"kind": "branch_node", "ref": parent_id, "rationale": "衍生故事继承分叉结果。"}], "confidence": "high", "stateChangeProposals": []}, "createdAt": timestamp()}
        package = {"id": "derived_" + str(uuid.uuid4()), "sessionId": session_id, "sourcePackageRef": {"id": self.package["id"], "version": self.package["version"]}, "forkBranchId": parent_id, "title": self.package["metadata"]["title"] + "·衍生篇", "goal": goal.strip(), "createdAt": timestamp(), "revisions": []}
        self.store.save_derived(package)
        return package, self.store.create_derived_entry(session_id, parent_id, entry)
