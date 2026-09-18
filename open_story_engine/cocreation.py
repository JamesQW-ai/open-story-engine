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

from .prompts import render_prompt, catalog_version
from .branch_ledger import BRANCH_LEDGER_KEY, append_branch_ledger, empty_branch_ledger, ensure_branch_ledger, ledger_context
from .content import by_id, story_node_by_id
from .llm import Completion, LlmError, OpenAICompatibleGateway, parse_json_content
from .storage import SessionStore


REPAIR_PLACEHOLDERS = ("⟦REPAIR_GAP_", "[此处缺少事实依据", "[此处存在审查问题")


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
    state.setdefault("derivedRelationships", [])
    state.setdefault("derivedClues", [])
    state.setdefault("derivedEvents", [])
    ensure_branch_ledger(state)
    return state


def empty_branch_additions() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "locations": [], "characters": [], "items": [], "relationships": [], "clues": [], "events": [],
        "characterReveals": [], "changes": [],
    }


BRANCH_PRIVATE_STATE_KEYS = frozenset({
    "derivedLocations", "derivedCharacters", "derivedItems", "derivedCharacterReveals",
    "derivedRelationships", "derivedClues", "derivedEvents", BRANCH_LEDGER_KEY,
    "playerCharacterId", "goalLedger", "threadLedger", "readerEntityStates",
})
CHARACTER_LOCATION_MAP_FIELD = "characterLocationIds"
ITEM_OWNER_MAP_FIELD = "itemOwnerCharacterIds"


def arc_model(package: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the optional, package-owned macro-plot model."""
    model = package.get("story", {}).get("arcModel")
    return model if isinstance(model, dict) else None


def entry_model(package: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return optional StoryPackage-declared character and plot-node choices."""
    model = package.get("story", {}).get("entryModel")
    return model if isinstance(model, dict) else None


def entry_point_by_id(package: Dict[str, Any], entry_point_id: str) -> Dict[str, Any]:
    model = entry_model(package)
    if model is None:
        raise ValueError("StoryPackage 未声明进入选择模型")
    entries = model["entryPoints"]
    indexed = getattr(entries, "get_by_id", None)
    entry = indexed(entry_point_id) if callable(indexed) else next((item for item in entries if item["id"] == entry_point_id), None)
    if entry is None:
        raise ValueError("进入剧情节点不存在: " + entry_point_id)
    return entry


def entry_source_characters(package: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List package-declared source roles that may be selected at session start."""
    model = entry_model(package)
    if model is None:
        player_id = package["initialState"]["player"]["characterId"]
        return [next(item for item in package["characters"] if item["id"] == player_id)]
    characters = by_id(package["characters"])
    return [characters[character_id] for character_id in model["sourceCharacterIds"]]


def default_entry_selection(package: Dict[str, Any]) -> Dict[str, Any]:
    """Keep packages without entryModel compatible with the original single entry."""
    model = entry_model(package)
    if model is None:
        return {
            "kind": "source_character",
            "sourceCharacterId": package["initialState"]["player"]["characterId"],
            "entryPointId": "legacy:start",
        }
    entry = entry_point_by_id(package, model["defaultEntryPointId"])
    source_ids = entry.get("sourceCharacterIds", [])
    if source_ids:
        return {"kind": "source_character", "sourceCharacterId": source_ids[0], "entryPointId": entry["id"]}
    return {"kind": "new_character", "name": "新来者", "entryPointId": entry["id"]}


def entry_points_for_selection(package: Dict[str, Any], selection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return package-declared entry choices for the selected persona."""
    model = entry_model(package)
    if model is None:
        return [{
            "id": "legacy:start", "title": package["story"]["premise"], "summary": package["story"]["premise"],
            "chapterTitle": package["metadata"]["title"], "nodeId": package["story"]["startNodeId"],
            "beatId": package["story"]["narrativeGraph"]["startBeatId"],
            "timelineRefs": [item["id"] for item in package.get("timeline", []) if item.get("knownAtStart")],
            "sourceCharacterIds": [package["initialState"]["player"]["characterId"]],
            "availableToNewCharacter": False,
        }]
    if selection.get("kind") == "source_character":
        character_id = selection.get("sourceCharacterId")
        indexed = getattr(model["entryPoints"], "for_source_character", None)
        if callable(indexed):
            entries = indexed(character_id)
        else:
            entries = [entry for entry in model["entryPoints"] if character_id in entry.get("sourceCharacterIds", [])]
        return entries
    if selection.get("kind") == "new_character":
        indexed = getattr(model["entryPoints"], "for_new_character", None)
        if callable(indexed):
            entries = indexed()
        else:
            entries = [entry for entry in model["entryPoints"] if entry.get("availableToNewCharacter")]
        return entries
    raise ValueError("进入身份类型必须是 source_character 或 new_character")


def new_character_profile_fields(package: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the package-owned schema for a session-only original character."""
    model = entry_model(package)
    if model is None:
        return []
    fields = model.get("newCharacter", {}).get("profileFields", [])
    return copy.deepcopy(fields) if isinstance(fields, list) else []


def persona_profile_text(persona: Dict[str, Any]) -> str:
    """Render only the session-authorized persona facts for prose generation."""
    profile = persona.get("profile")
    if not isinstance(profile, list):
        return "- 原著既有角色：" + str(persona.get("name", "未命名角色"))
    lines = []
    for field in profile:
        if isinstance(field, dict) and isinstance(field.get("label"), str) and field.get("value") is not None:
            lines.append("- " + field["label"] + "：" + str(field["value"]))
    return "\n".join(lines) or "- 新建角色：" + str(persona.get("name", "未命名角色"))


def normalize_entry_selection(package: Dict[str, Any], selection: Optional[Dict[str, Any]], allow_any_source_character: bool = False) -> Dict[str, Any]:
    """Validate a session-only identity and entry node against StoryPackage data.

    With ``allow_any_source_character`` and an explicit entry point, the
    entry-point filtering by persona is relaxed: any package character may
    enter that declared entry point. Calls without an explicit entry point
    retain the official resolver's legacy binding behavior.
    """
    normalized = copy.deepcopy(selection or default_entry_selection(package))
    requested_character_id = normalized.get("sourceCharacterId")
    approved_source_ids = {
        item["id"] for item in entry_source_characters(package)
    }
    explicit_entry_override = (
        allow_any_source_character
        and isinstance(normalized.get("entryPointId"), str)
        and normalized.get("kind") == "source_character"
        and requested_character_id not in approved_source_ids
    )
    if (entry_model(package) or {}).get("policy") == "official_unknown_reader/1" and not explicit_entry_override:
        if normalized.get("kind") != "source_character":
            raise ValueError("官方故事只允许选择已开放人物")
        character = next((c for c in entry_source_characters(package)
                          if c["id"] == normalized.get("sourceCharacterId")), None)
        if character is None:
            raise ValueError("当前人物尚未开放游玩")
        default_id = character.get("defaultEntryPointId")
        if not default_id or normalized.get("entryPointId", default_id) != default_id:
            raise ValueError("必须从该人物的官方默认情节进入")
        normalized["entryPointId"] = default_id
    kind = normalized.get("kind")
    if kind == "source_character":
        character_id = normalized.get("sourceCharacterId")
        identity_pool = (
            {item["id"] for item in package["characters"]}
            if explicit_entry_override
            else {item["id"] for item in entry_source_characters(package)}
        )
        if character_id not in identity_pool:
            raise ValueError("不能以未声明的原著角色进入共创")
    elif kind == "new_character":
        model = entry_model(package)
        if model is None or not model["newCharacter"]["enabled"]:
            raise ValueError("当前故事包不允许新建角色进入")
        fields = new_character_profile_fields(package)
        raw_profile = normalized.get("profile")
        if not fields:
            raw_profile = {"name": normalized.get("name", "")}
            fields = [{"id": "name", "label": "姓名", "type": "text", "minLength": 2, "maxLength": 12}]
        if not isinstance(raw_profile, dict):
            raise ValueError("新建角色必须填写完整身份档案")
        profile: Dict[str, Any] = {}
        for field in fields:
            field_id = field["id"]
            value = raw_profile.get(field_id)
            if field["type"] == "text":
                if not isinstance(value, str):
                    raise ValueError("新建角色档案“" + field["label"] + "”必须填写文本")
                value = value.strip()
                if not field.get("minLength", 1) <= len(value) <= field.get("maxLength", 500):
                    raise ValueError("新建角色档案“" + field["label"] + "”长度不符合要求")
            else:
                if isinstance(value, bool):
                    raise ValueError("新建角色档案“" + field["label"] + "”必须是整数")
                try:
                    value = int(value)
                except (TypeError, ValueError) as error:
                    raise ValueError("新建角色档案“" + field["label"] + "”必须是整数") from error
                if not field["minimum"] <= value <= field["maximum"]:
                    raise ValueError("新建角色档案“" + field["label"] + "”不在允许范围内")
            profile[field_id] = value
        name = profile.get("name", "")
        if not isinstance(name, str) or not 2 <= len(name) <= 12 or not re.fullmatch(r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_-]*", name):
            raise ValueError("新建角色名须为 2 至 12 个中文、英文、数字、下划线或连字符")
        normalized["profile"] = profile
        normalized["name"] = name
    else:
        raise ValueError("进入身份类型必须是 source_character 或 new_character")
    entry_point_id = normalized.get("entryPointId")
    if explicit_entry_override and kind == "source_character" and entry_model(package) is not None:
        entries = list(entry_model(package)["entryPoints"])
    else:
        entries = entry_points_for_selection(package, normalized)
    if entry_point_id is None:
        if len(entries) != 1:
            raise ValueError("请选择进入剧情节点")
        normalized["entryPointId"] = entries[0]["id"]
    elif entry_point_id not in {entry["id"] for entry in entries}:
        raise ValueError("该身份不能从所选剧情节点进入")
    return normalized


def entry_initial_state(package: Dict[str, Any], selection: Optional[Dict[str, Any]] = None, allow_any_source_character: bool = False) -> Dict[str, Any]:
    """Return a copied entry snapshot for the new session, never mutating the package."""
    selection = normalize_entry_selection(package, selection, allow_any_source_character)
    entry = entry_point_by_id(package, selection["entryPointId"])
    beat = find_beat(package, entry["beatId"])
    state = initial_branch_state(beat)
    opening_location = entry.get("sourceCharacterLocationIds", {}).get(selection.get("sourceCharacterId"))
    if opening_location is not None and script_generated_package(package):
        if opening_location not in {place["id"] for place in package["locations"]}:
            raise ValueError("身份开场地点不存在")
        state["playerLocationId"] = opening_location
    _bind_entry_player(package, state, selection, entry["id"])
    if script_generated_package(package):
        entry_chapter_id = entry.get("sourceChapterId")
        owner_ids = {
            item["id"]: item["initialOwnerCharacterId"]
            for item in package.get("items", [])
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("initialOwnerCharacterId"), str)
            and _source_chapter_is_available(item.get("availableFromSourceChapterId"), entry_chapter_id)
        }
        if owner_ids:
            state[ITEM_OWNER_MAP_FIELD] = owner_ids
    if (entry_model(package) or {}).get("policy") == "official_unknown_reader/1":
        state.update(copy.deepcopy(entry["openingState"]))
    return state


def _bind_entry_player(package: Dict[str, Any], state: Dict[str, Any], persona: Dict[str, Any], entry_ref: str) -> None:
    """Bind the chosen player to the branch, leaving source entities unchanged."""
    if not script_generated_package(package):
        return
    if persona.get("kind") == "source_character":
        player_id = persona["sourceCharacterId"]
    else:
        player_id = "character_player_" + hashlib.sha256(persona["name"].encode("utf-8")).hexdigest()[:12]
        profile = persona.get("profile", {})
        if isinstance(profile, list):
            profile = {field["id"]: field["value"] for field in profile}
        character = {
            "id": player_id, "name": persona["name"],
            "summary": "玩家创建的角色。", "profile": copy.deepcopy(profile),
        }
        state["derivedCharacters"].append(character)
        append_branch_ledger(state, [{
            "kind": "character", "operation": "added", "entityId": player_id,
            "summary": "玩家在入口确认角色档案。", "before": None,
            "after": {**character, "locationId": state["playerLocationId"]},
        }], {"kind": "player_profile", "ref": entry_ref})
    state["playerCharacterId"] = player_id
    state[CHARACTER_LOCATION_MAP_FIELD] = {player_id: state["playerLocationId"]}


def _source_chapter_is_available(item_chapter_id: Any, entry_chapter_id: Any) -> bool:
    """Compare script-generated chapter ids without assuming a fixed story title."""
    if not isinstance(item_chapter_id, str) or not isinstance(entry_chapter_id, str):
        return False
    item_match = re.search(r"(\d+)$", item_chapter_id)
    entry_match = re.search(r"(\d+)$", entry_chapter_id)
    if item_match is None or entry_match is None:
        return item_chapter_id == entry_chapter_id
    return int(item_match.group(1)) <= int(entry_match.group(1))


def arc_by_id(package: Dict[str, Any], arc_id: str) -> Dict[str, Any]:
    model = arc_model(package)
    if model is None:
        raise ValueError("StoryPackage 未声明大方向模型")
    arcs = model["arcs"]
    indexed = getattr(arcs, "get_by_id", None)
    arc = indexed(arc_id) if callable(indexed) else next((item for item in arcs if item["id"] == arc_id), None)
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
    # Entry points may use a presentation location while the source beat keeps
    # its canonical internal location. Source progress remains authoritative for
    # the first phase menu; turn-time state validation stays unchanged.
    if not directions and state.get("storyScope") == "source":
        beats = package["story"]["narrativeGraph"]["beats"]
        indexed = getattr(beats, "get_by_source_progress", None)
        beat = indexed(state.get("sourceProgress")) if callable(indexed) else next(
            (item for item in beats if item.get("branchState", {}).get("sourceProgress") == state.get("sourceProgress")),
            None,
        )
        if beat is not None and beat.get("nodeId") == source_node_ref:
            directions = [branch_direction(item) for item in beat.get("nextDirections", [])]
    return [
        {**direction, "directionLevel": "phase", "arcId": arc_id}
        for direction in directions
        if direction["id"] in phase_ids
    ]


def chapter_title_for_direction(package: Dict[str, Any], selected: Dict[str, Any]) -> str:
    """Apply the source package's recorded heading convention to one chapter title."""
    title = selected.get("title")
    if not isinstance(title, str) or not title.strip():
        return "未命名章节"
    style = package.get("metadata", {}).get("chapterHeadingStyle")
    patch = selected.get("statePatch")
    progress = patch.get("sourceProgress") if isinstance(patch, dict) else None
    match = re.search(r"_(\d+)$", progress) if isinstance(progress, str) else None
    if style not in ("chinese_dunhao", "arabic_dunhao", "chinese_chapter", "arabic_chapter") or match is None:
        return title.strip()
    number = int(match.group(1))
    if style == "chinese_dunhao":
        return _chinese_chapter_number(number) + "、" + title.strip()
    if style == "arabic_dunhao":
        return str(number) + "、" + title.strip()
    if style == "chinese_chapter":
        return "第" + _chinese_chapter_number(number) + "章 " + title.strip()
    return "第" + str(number) + "章 " + title.strip()


def _chinese_chapter_number(number: int) -> str:
    if number <= 0:
        return str(number)
    digits = "零一二三四五六七八九"
    units = ("", "十", "百", "千")
    groups: List[str] = []
    remaining = number
    for unit in units:
        digit = remaining % 10
        if digit:
            groups.append(digits[digit] + unit)
        elif groups and remaining >= 10 and not groups[-1].startswith("零"):
            groups.append("零")
        remaining //= 10
        if not remaining:
            break
    value = "".join(reversed(groups)).replace("零零", "零").rstrip("零")
    return value[1:] if value.startswith("一十") else value


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
    source_heading_style = package.get("metadata", {}).get("chapterHeadingStyle")
    if source_heading_style:
        chapter_title = chapter_title_for_direction(package, selected)
    else:
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
    return any(value[key] for key in empty_branch_additions())


def validate_branch_additions(package: Dict[str, Any], state: Dict[str, Any], value: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Validate branch-private world expansion without mutating StoryPackage."""
    if value is None:
        return empty_branch_additions()
    allowed = set(empty_branch_additions())
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("分支扩展只能包含 locations、characters、items、relationships、clues、events、characterReveals、changes")
    additions = empty_branch_additions()
    known_ids = {item["id"] for item in package["locations"] + package["characters"] + package["items"]}
    derived_locations = state.get("derivedLocations", [])
    derived_characters = state.get("derivedCharacters", [])
    derived_items = state.get("derivedItems", [])
    derived_relationships = state.get("derivedRelationships", [])
    derived_clues = state.get("derivedClues", [])
    derived_events = state.get("derivedEvents", [])
    known_ids.update(item["id"] for item in (
        derived_locations + derived_characters + derived_items + derived_relationships + derived_clues + derived_events
    ))
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
    known_characters = {item["id"] for item in package["characters"] + derived_characters + additions["characters"]}
    for collection, prefix in (("relationships", "relationship_"), ("clues", "clue_"), ("events", "event_")):
        entries = value.get(collection, [])
        if not isinstance(entries, list):
            raise ValueError(f"分支扩展的 {collection} 必须是数组")
        for entity in entries:
            required = ("id", "summary")
            if collection == "relationships":
                required += ("fromCharacterId", "toCharacterId")
            else:
                required += ("name",)
            if not isinstance(entity, dict) or not all(
                isinstance(entity.get(key), str) and entity[key].strip() for key in required
            ):
                raise ValueError(f"分支扩展的{collection}缺少必要字段")
            if not re.fullmatch(re.escape(prefix) + r"[a-z0-9_]{2,80}", entity["id"]) or entity["id"] in known_ids:
                raise ValueError(f"分支扩展实体 id 无效或已存在: {entity['id']}")
            if collection == "relationships" and (
                entity["fromCharacterId"] not in known_characters or entity["toCharacterId"] not in known_characters
            ):
                raise ValueError("分支关系必须引用已登记角色")
            additions[collection].append({key: entity[key].strip() for key in required})
            known_ids.add(entity["id"])
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
    known_by_kind = {
        "location": {item["id"] for item in package["locations"] + derived_locations + additions["locations"]},
        "character": {item["id"] for item in package["characters"] + derived_characters + additions["characters"]},
        "item": {item["id"] for item in package["items"] + derived_items + additions["items"]},
        "relationship": {item["id"] for item in derived_relationships + additions["relationships"]},
        "clue": {item["id"] for item in derived_clues + additions["clues"]},
        "event": {item["id"] for item in derived_events + additions["events"]},
    }
    changes = value.get("changes", [])
    if not isinstance(changes, list):
        raise ValueError("分支扩展的 changes 必须是数组")
    for change in changes:
        if not isinstance(change, dict) or not all(
            isinstance(change.get(key), str) and change[key].strip() for key in ("kind", "entityId", "summary")
        ):
            raise ValueError("分支实体变化必须包含 kind、entityId、summary")
        if change["kind"] not in known_by_kind or change["entityId"] not in known_by_kind[change["kind"]]:
            raise ValueError("分支实体变化必须引用已登记实体")
        attributes = change.get("attributes", {})
        if not isinstance(attributes, dict) or any(
            not isinstance(key, str) or not key or isinstance(item, (dict, list))
            for key, item in attributes.items()
        ):
            raise ValueError("分支实体变化 attributes 必须是标量对象")
        from .item_lifecycle import ATTRIBUTE, destroyed
        if ATTRIBUTE in attributes or (change['kind'] == 'item' and destroyed(state, change['entityId'])):
            raise ValueError('道具永久损毁及其后果必须由已核对正文的行动契约处理')
        additions["changes"].append({
            "kind": change["kind"].strip(), "entityId": change["entityId"].strip(),
            "summary": change["summary"].strip(), "attributes": copy.deepcopy(attributes),
        })
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


def _branch_ledger_changes(
    current: Dict[str, Any], next_state: Dict[str, Any], additions: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Derive auditable facts from a locally accepted state transition."""
    changes: List[Dict[str, Any]] = []
    for field, after in next_state.items():
        if field in BRANCH_PRIVATE_STATE_KEYS or current.get(field) == after:
            continue
        before = current.get(field)
        if field == CHARACTER_LOCATION_MAP_FIELD and isinstance(after, dict):
            before_mapping = before if isinstance(before, dict) else {}
            for character_id, location_id in after.items():
                if before_mapping.get(character_id) != location_id:
                    changes.append({
                        "kind": "character", "operation": "changed", "entityId": character_id,
                        "summary": "角色位置已由受控方向更新。",
                        "before": {"locationId": before_mapping.get(character_id)}, "after": {"locationId": location_id},
                    })
        elif field == ITEM_OWNER_MAP_FIELD and isinstance(after, dict):
            before_mapping = before if isinstance(before, dict) else {}
            for item_id, owner_id in after.items():
                if before_mapping.get(item_id) != owner_id:
                    changes.append({
                        "kind": "item", "operation": "changed", "entityId": item_id,
                        "summary": "物品归属已由受控方向更新。",
                        "before": {"ownerCharacterId": before_mapping.get(item_id)}, "after": {"ownerCharacterId": owner_id},
                    })
        else:
            changes.append({
                "kind": "event", "operation": "changed", "entityId": "state:" + field,
                "summary": "运行时状态字段“" + field + "”已由受控方向更新。", "before": before, "after": after,
            })
    for collection, kind in (
        ("locations", "location"), ("characters", "character"), ("items", "item"),
        ("relationships", "relationship"), ("clues", "clue"), ("events", "event"),
    ):
        for entity in additions[collection]:
            changes.append({
                "kind": kind, "operation": "added", "entityId": entity["id"], "summary": entity["summary"],
                "before": None, "after": copy.deepcopy(entity),
            })
    for reveal in additions["characterReveals"]:
        changes.append({
            "kind": "character", "operation": "changed", "entityId": reveal["characterId"], "summary": reveal["summary"],
            "before": {"identity": None}, "after": {"identity": reveal["name"]},
        })
    for change in additions["changes"]:
        changes.append({
            "kind": change["kind"], "operation": "changed", "entityId": change["entityId"], "summary": change["summary"],
            "before": None, "after": copy.deepcopy(change["attributes"]),
        })
    return changes


def apply_branch_patch(
    package: Dict[str, Any], current: Dict[str, Any], patch: Dict[str, Any], source_node_ref: str,
    ledger_source: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if set(patch) & {"characterOutcomeStates", "goalLedger", "threadLedger", "readerEntityStates"}:
        raise ValueError("人物后果和目标必须由已核对正文的结果契约更新")
    if not patch:
        raise ValueError("剧情方向必须声明至少一个状态变化")
    unsupported = set(patch) - set(current) - {"derivedAdditions"}
    if unsupported:
        raise ValueError("剧情方向包含不受支持的状态字段: " + "、".join(sorted(unsupported)))
    private_fields = set(patch) & BRANCH_PRIVATE_STATE_KEYS
    if private_fields:
        raise ValueError("剧情方向不能直接改写分支私有状态字段: " + "、".join(sorted(private_fields)) + "；请使用 derivedAdditions")
    movements = patch.get(CHARACTER_LOCATION_MAP_FIELD)
    for cid, location in (movements.items() if isinstance(movements, dict) else []):
        outcome = current.get('characterOutcomeStates', {}).get(cid, {})
        if outcome.get('status') == 'missing' and location is not None:
            raise ValueError('失踪人物重现必须由已核对正文的结果契约更新：' + cid)
        if outcome.get('permanence') == 'permanent' and outcome.get('status') in ('dead', 'departed') and current.get(CHARACTER_LOCATION_MAP_FIELD, {}).get(cid) != location:
            raise ValueError('方向不能移动已永久下线的人物：' + cid)
    for key, value in patch.items():
        if key == "derivedAdditions" or value is None:
            continue
        if key == CHARACTER_LOCATION_MAP_FIELD:
            if not isinstance(value, dict) or any(
                not isinstance(character_id, str) or not isinstance(location_id, str)
                for character_id, location_id in value.items()
            ):
                raise ValueError("剧情方向的 characterLocationIds 必须是角色 ID 到地点 ID 的对象")
            continue
        if isinstance(value, (dict, list)):
            raise ValueError(f"剧情方向的 {key} 必须使用目标原始值，不能使用对象或数组")
        if type(value) is not type(current[key]):
            raise ValueError(f"剧情方向的 {key} 类型必须与当前状态一致")
    next_state = copy.deepcopy(current)
    additions = validate_branch_additions(package, current, patch.get("derivedAdditions"))
    for key, value in patch.items():
        if key != "derivedAdditions" and value is not None:
            if key == CHARACTER_LOCATION_MAP_FIELD:
                next_state[key] = {**current.get(CHARACTER_LOCATION_MAP_FIELD, {}), **value}
            else:
                next_state[key] = value
    for collection, state_key, label in (
        ("locations", "derivedLocations", "地点"), ("characters", "derivedCharacters", "人物"), ("items", "derivedItems", "物品"),
        ("relationships", "derivedRelationships", "关系"), ("clues", "derivedClues", "线索"), ("events", "derivedEvents", "事件"),
    ):
        known = {entry["id"] for entry in next_state[state_key]}
        for entity in additions.get(collection, []):
            if entity["id"] in known:
                raise ValueError(f"派生故事不能重复引入{label}: {entity['id']}")
            next_state[state_key].append(copy.deepcopy(entity))
            known.add(entity["id"])
    player_id = current.get("playerCharacterId")
    if isinstance(player_id, str) and "playerLocationId" in patch:
        explicit_location = patch.get(CHARACTER_LOCATION_MAP_FIELD, {}).get(player_id)
        if explicit_location is not None and explicit_location != next_state["playerLocationId"]:
            raise ValueError("玩家位置与角色位置补丁冲突")
        next_state[CHARACTER_LOCATION_MAP_FIELD] = {
            **next_state.get(CHARACTER_LOCATION_MAP_FIELD, {}), player_id: next_state["playerLocationId"],
        }
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
    known_character_ids = {character["id"] for character in package["characters"]} | {
        character["id"] for character in next_state["derivedCharacters"]
    }
    character_locations = next_state.get(CHARACTER_LOCATION_MAP_FIELD, {})
    if character_locations is not None and (
        not isinstance(character_locations, dict)
        or any(character_id not in known_character_ids or location_id not in known_locations
               for character_id, location_id in character_locations.items())
    ):
        raise ValueError("共创状态的 characterLocationIds 引用了不存在的角色或地点")
    for field in state_model.get("locationReferenceFields", []):
        if next_state.get(field) not in known_locations:
            raise ValueError(f"共创状态的 {field} 引用了不存在的地点")
    for invariant in state_model.get("invariants", []):
        if state_matches(invariant["when"], next_state) and not state_matches(invariant["require"], next_state):
            raise ValueError(invariant["message"])
    append_branch_ledger(
        next_state, _branch_ledger_changes(current, next_state, additions),
        ledger_source or {"kind": "direction", "ref": source_node_ref, "nodeRef": source_node_ref},
    )
    return next_state


def branch_direction(direction: Dict[str, Any]) -> Dict[str, Any]:
    return {key: copy.deepcopy(direction[key]) for key in ("id", "title", "summary", "canonicalBeatId", "followupBeatId", "rejoinTargetId", "statePatch", "directionLevel", "arcId", "isFreeText") if key in direction}


def create_contract(package: Dict[str, Any], session_id: str, selection: Optional[Dict[str, Any]] = None, allow_any_source_character: bool = False) -> Dict[str, Any]:
    """Freeze the chosen StoryPackage entry without changing the package itself."""
    selection = normalize_entry_selection(package, selection, allow_any_source_character)
    entry = entry_point_by_id(package, selection["entryPointId"])
    if selection["kind"] == "source_character":
        player = next(character for character in package["characters"] if character["id"] == selection["sourceCharacterId"])
        persona: Dict[str, Any] = {"kind": "source_character", "sourceCharacterId": player["id"], "name": player["name"]}
    else:
        persona = {"kind": "new_character", "name": selection["name"]}
        if new_character_profile_fields(package):
            persona["profile"] = [
                {"id": field["id"], "label": field["label"], "value": selection["profile"][field["id"]]}
                for field in new_character_profile_fields(package)
            ]
    return {
        "id": "contract_" + str(uuid.uuid4()), "sessionId": session_id, "mode": "derivative_co_creation",
        "sourcePackageRef": {"id": package["id"], "version": package["version"]},
        "entryPointId": entry["id"], "entryBeatId": entry["beatId"], "entryNodeId": entry["nodeId"],
        "entryChapterTitle": entry["chapterTitle"], "entrySourceChapterId": entry.get("sourceChapterId"), "canonicalPrefixNodeIds": [entry["nodeId"]],
        "canonicalTimelineRefs": copy.deepcopy(entry.get("timelineRefs", [])),
        "continuityScope": "canonical_until_entry", "persona": persona,
        **({"openingContext": copy.deepcopy(entry["openingContext"]),
            "continuityContract": copy.deepcopy(entry_model(package)["continuityContract"])}
           if entry.get("openingContext") else {}),
        "immutableFactRefs": [fact["id"] for fact in package["world"]["immutableFacts"]
                              if not entry.get("openingContext") or
                              fact.get("lineRange", {}).get("end", float("inf")) <= entry["openingContext"]["sourceCutoffLine"]],
        "direction": next(item for item in package["directions"] if item["id"] == package["defaultDirectionId"]),
        "provenance": [{"kind": "source", "ref": package["id"] + "@" + package["version"], "note": "固定原著故事包引用"}], "createdAt": timestamp(),
    }


def entry_node(package: Dict[str, Any], contract: Dict[str, Any], allow_any_source_character: bool = False) -> Dict[str, Any]:
    try:
        beat = find_beat(package, contract.get("entryBeatId"))
    except ValueError as error:
        raise ValueError("共创起始节点没有叙事锚点") from error
    model = arc_model(package)
    persona = contract.get("persona", {})
    selection = {**persona, "entryPointId": contract["entryPointId"]}
    if isinstance(selection.get("profile"), list):
        selection["profile"] = {field["id"]: field["value"] for field in selection["profile"]}
    state = entry_initial_state(package, selection, allow_any_source_character)
    if (entry_model(package) or {}).get("policy") == "official_unknown_reader/1":
        from .reader_threads import STATE_KEY, initial_threads
        state[STATE_KEY] = initial_threads(package, contract)
    directions = macro_directions(package, model["entryArcIds"], state) if model is not None else [branch_direction(item) for item in beat["nextDirections"]]
    new_character = persona.get("kind") == "new_character"
    entry = entry_point_by_id(package, contract["entryPointId"])
    narrative = entry.get("newCharacterNarrative") if new_character else entry.get("sourceCharacterNarratives", {}).get(persona.get("sourceCharacterId"))
    return {
        "id": "branch_" + str(uuid.uuid4()), "kind": "source_entry", "sourceNodeRef": contract["entryNodeId"],
        "branchState": state, "narrativeText": narrative or beat.get("sourceExcerpt", {}).get("text", beat["narrativeAnchor"]),
        "summary": entry.get("openingSummary", beat["summary"]), "factDeltas": [{"id": "fact_source_entry", "source": "source", "summary": "原著前史已继承"}],
        "openThreads": entry.get("openingThreads", beat["openThreads"]), "nextDirections": directions,
        **({"openingActions": copy.deepcopy(entry["openingActions"])} if entry.get("openingActions") else {}),
        **({"openingContext": copy.deepcopy(entry["openingContext"])} if entry.get("openingContext") else {}),
        **({"openingClues": list(entry['openingClues'])} if entry.get('openingClues') else {}),
        "entryChapter": {"title": contract.get("entryChapterTitle", package["metadata"]["title"]), "sourceChapterId": contract.get("entrySourceChapterId"), "entryPointId": contract.get("entryPointId")},
        "canonicalRelation": "diverged" if narrative else "on_line", "planning": {"citations": [{"kind": "canonical_node", "ref": contract["entryNodeId"], "rationale": "共创从原著节点进入。"}], "confidence": "high", "stateChangeProposals": []}, "createdAt": timestamp(),
    }


def script_generated_package(package: Dict[str, Any]) -> bool:
    """Generated packages use source beats as constraints, not displayed prose."""
    return package.get("metadata", {}).get("authoringSource") == "source_text_script"


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
    beats = package["story"]["narrativeGraph"]["beats"]
    indexed = getattr(beats, "get_by_id", None)
    beat = indexed(beat_id) if callable(indexed) else next((item for item in beats if item["id"] == beat_id), None)
    if beat is None:
        raise ValueError("叙事锚点不存在: " + beat_id)
    return beat


def beat_for_state(package: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve the current source beat without scanning a lazy narrative graph."""
    beats = package["story"]["narrativeGraph"]["beats"]
    indexed = getattr(beats, "get_by_source_progress", None)
    beat = indexed(state.get("sourceProgress")) if callable(indexed) else None
    if beat is not None and matches_storypackage_state(initial_branch_state(beat), state):
        return beat
    for candidate in beats:
        if matches_storypackage_state(initial_branch_state(candidate), state):
            return candidate
    return None


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
    expected_state = {key: value for key, value in initial_branch_state(beat).items() if key not in BRANCH_PRIVATE_STATE_KEYS}
    actual_state = {key: value for key, value in resolved_state.items() if key not in BRANCH_PRIVATE_STATE_KEYS}
    if expected_state != actual_state:
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
        {key: value for key, value in expected.items() if key not in BRANCH_PRIVATE_STATE_KEYS | {CHARACTER_LOCATION_MAP_FIELD}}
        == {key: value for key, value in actual.items() if key not in BRANCH_PRIVATE_STATE_KEYS | {CHARACTER_LOCATION_MAP_FIELD}}
    )


def controlled_scene_directions(package: Dict[str, Any], source_node_ref: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the package menu for exactly this source scene and state."""
    if state["storyScope"] != "source":
        return []
    beat = beat_for_state(package, state)
    if beat is not None and beat["nodeId"] == source_node_ref:
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
    beat = beat_for_state(package, state)
    if beat is not None and beat["nodeId"] == source_node_ref:
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


def source_continuity_context(
    package: Dict[str, Any], lineage: List[Dict[str, Any]], state: Dict[str, Any], timeline_refs: Optional[List[str]] = None,
) -> str:
    """Give the planner a bounded StoryPackage digest, never the original corpus."""
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
        if node.get("canonicalRelation") != "on_line" and narrative:
            excerpts.append(narrative[-3600:])
    allowed_timeline = set(timeline_refs) if timeline_refs is not None else None
    protected_history = [
        item.get("description", "").strip()
        for item in package.get("timeline", [])
        if allowed_timeline is None or item.get("id") in allowed_timeline
    ]
    protected_history = [item for item in protected_history if item]
    return "\n".join([
        "已确认道具归属：" + "；".join(facts),
        "受保护的世界历史（仅用于保持因果一致；不可把尚未确认的内容直接当作人物已知事实）："
        + ("；".join(protected_history) if protected_history else "无"),
        "分支状态账本（跨回合因果只以此处的结构化、带来源条目为准）：" + ledger_context(state),
        "已确认的分支正文片段（只可延续，不能改写其中已出现的人物、地点、道具归属或因果）：",
        "\n\n".join(excerpts) or "无",
    ])


def state_character_locations(package: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """Bind characters to authoritative locations without story-specific names.

    Packages may declare ``narrativeGuidelines.characterLocationStateFields`` as
    ``{characterId: stateField}``. For existing packages, the conventional
    ``<character-id-prefix>LocationId`` fields are inferred when unambiguous;
    ``playerLocationId`` belongs to the branch player, falling back to the
    package's player for legacy sessions.
    This lets a package opt into explicit bindings without requiring a new
    runtime schema version.
    """
    locations = by_id(package.get("locations", []))
    locations.update(by_id(state.get("derivedLocations", [])))
    character_by_id = by_id(package.get("characters", []))
    character_by_id.update(by_id(state.get("derivedCharacters", [])))
    player_id = state.get("playerCharacterId") or package.get("initialState", {}).get("player", {}).get("characterId")
    guidelines = package.get("world", {}).get("narrativeGuidelines", {})
    declared = guidelines.get("characterLocationStateFields", {})
    bindings: Dict[str, str] = {}
    if isinstance(declared, dict):
        bindings.update({character_id: field for character_id, field in declared.items()
                         if character_id in character_by_id and isinstance(field, str)
                         and (field != "playerLocationId" or character_id == player_id)})
    mapped_locations = state.get(CHARACTER_LOCATION_MAP_FIELD, {})
    if isinstance(mapped_locations, dict):
        bindings.update({
            character_id: CHARACTER_LOCATION_MAP_FIELD
            for character_id, location_id in mapped_locations.items()
            if character_id in character_by_id and isinstance(location_id, str) and location_id in locations
        })
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
        location_id = mapped_locations.get(character_id) if field == CHARACTER_LOCATION_MAP_FIELD else state.get(field)
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
    if state.get("playerCharacterId"):
        return "playerLocationId"
    focal_id = package.get("world", {}).get("narrativeGuidelines", {}).get("focalCharacterId")
    if not isinstance(focal_id, str):
        focal_id = package.get("initialState", {}).get("player", {}).get("characterId")
    positions = state_character_locations(package, state)
    for position in positions.values():
        if position["characterId"] == focal_id:
            if position["stateField"] == CHARACTER_LOCATION_MAP_FIELD:
                return "playerLocationId" if "playerLocationId" in state else None
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
    movement = r"(?:位于|回到|返回|回|来到|走进|进入|抵达|赶到|走向|走到|沿着|站在|坐在|停在)"
    character_names = [item["name"] for item in package.get("characters", [])] + [item["name"] for item in state.get("derivedCharacters", [])]
    for name, expected in locations.items():
        final_location: Optional[str] = None
        doorway_location: Optional[str] = None
        subject: Optional[str] = None
        for sentence in sentence_list:
            sentence = sentence.strip()
            explicit_subject = next((value for value in character_names if sentence.startswith(value)), None)
            if explicit_subject:
                subject = explicit_subject
            elif sentence.startswith(("他", "她")) and subject == name:
                sentence = name + sentence[1:]
            if name not in sentence:
                continue
            if doorway_location and re.search(
                rf"{re.escape(name)}[^。！？\n]{{0,12}}(?:挤进|跨进|迈入|走进|进入)[^。！？\n]{{0,4}}(?:门缝|门内|门里|那扇门|这扇门)", sentence,
            ) and not re.search(r"(?:没有|并未|不曾|不敢|准备|打算|想要)[^。！？\n]{0,8}(?:挤进|跨进|迈入|走进|进入)", sentence):
                final_location = doorway_location
                doorway_location = None
            placements = []
            for location in registered_locations:
                location_name = location.get("name")
                if not isinstance(location_name, str) or not location_name:
                    continue
                if re.search(
                    rf"{re.escape(name)}[^。！？\n]{{0,10}}(?:站在|走到|来到|停在){re.escape(location_name)}(?:的)?门口", sentence,
                ):
                    doorway_location = location_name
                direct_move = re.search(
                    rf"{re.escape(name)}(?P<placement_prefix>[^。！？\n]{{0,10}}){movement}[^。！？\n]{{0,4}}{re.escape(location_name)}(?!(?:的)?(?:门|窗|外|方向))",
                    sentence,
                )
                if direct_move:
                    prefix = re.split(r'随后|然后|却|但是|但', direct_move.group('placement_prefix'))[-1]
                    if re.search(r'说明|询问|谈起|回忆|记得|如何|怎么|可以|能够|可$|准备|打算|想要|没有|并未|不曾|不敢', prefix):
                        direct_move = None
                named_placement = re.search(
                    rf"{re.escape(location_name)}(?:里|内|中)(?:只|正|还|就)?(?:有|站着|坐着|躲着|是)?{re.escape(name)}",
                    sentence,
                )
                if direct_move or named_placement:
                    match = direct_move or named_placement
                    placements.append((match.start(), len(location_name), location_name))
            if placements:
                final_location = max(placements)[2]
                doorway_location = None
        if final_location is not None and final_location != expected["locationName"]:
            raise ValueError(
                "剧情正文与已确认状态矛盾："
                f"{name} 的最终位置应为 {expected['locationName']}，不能写为 {final_location}。"
            )


def guard_unregistered_communication(text: str, package: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Reject a new unknown message before prose turns it into branch canon."""
    if state.get("storyScope") != "source" or not script_generated_package(package):
        return
    narration = narration_outside_dialogue(text)
    unknown_sender = r"(?:未知|陌生|不明)(?:号码|发件人|来电|短信|消息|语音)"
    new_unknown_message = r"(?:一条|收到|出现)[^。！？\n]{0,16}(?:新消息|新短信|新语音|新来电)[^。！？\n]{0,24}(?:未知|陌生|不明)"
    if re.search(unknown_sender, narration) or re.search(new_unknown_message, narration):
        raise ValueError("剧情正文引入了未登记的通信事实。")


def guard_offstage_reports(text: str, scope: Dict[str, Any]) -> None:
    """Background roles cannot become offstage sources of new causal facts."""
    evidence = "\n".join(
        [cue["text"] for cue in scope.get("narrativeBrief", [])]
        + [cue["text"] for cue in scope.get("priorNarrativeBrief", [])]
        + [fact["text"] for fact in scope.get("world", {}).get("immutableFacts", [])]
        + [scope.get("currentBeat", {}).get("summary", "")]
        + [character.get("detail", "") for character in scope.get("characterDetails", [])]
    )
    roles = ("司机", "调度员", "调度那边", "站长", "值班员", "站务员", "工作人员", "陌生人", "模糊的男声", "传话的人")
    for role in roles:
        if role in evidence:
            continue
        pattern = re.escape(role) + r"[^。！？\n]{0,25}(?:说|报告|通知|回应|回答|告诉|确认|联系不上|关机|失联|给出|传来)"
        if re.search(pattern, text):
            raise ValueError("剧情正文引入了未登记的信息来源或通信结果: " + role)
        for paragraph in re.split(r"\n\s*\n", text):
            # An object/comparison such as "面对一个陌生人" does not
            # introduce that person as the speaker of another quoted line.
            introduced = r"(?:^|[。！？；，,]\s*)(?:一个|一名|那名|那个)" + re.escape(role)
            if re.search(introduced, paragraph) and re.search(
                r"[“\"]|(?:说|报告|通知|回答|告诉)[：:，,]", paragraph,
            ):
                raise ValueError("剧情正文引入了未登记的信息来源或通信结果: " + role)
    contract = scope.get("actionContract", {})
    for pattern in contract.get("forbiddenPatterns", []):
        for match in re.finditer(pattern, text):
            # Questions, conditions, and explicit negatives do not complete an action.
            start = max(text.rfind(mark, 0, match.start()) for mark in ("。", "！", "？", "\n")) + 1
            prefix = text[start:match.start()]
            following = re.split(r"[。！？\n]", text[match.end():], maxsplit=1)[0]
            if re.search(r"(?:如果|假如|只要|一旦|不能|不得|没有|尚未|未曾)[^。！？]*$", prefix + match.group()):
                continue
            if re.match(r"(?:了)?(?:吗|么|没有|了吗)", following):
                continue
            raise ValueError("剧情正文将尚未完成的要求写成执行结果: " + match.group())


def guard_unbound_source_outcomes(text: str, package: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Keep untracked source-character outcomes and item transfers unresolved.

    A script-generated package may deliberately leave a source character's
    location unknown at a chosen entry point. Prose must not turn that unknown
    into an unrecorded disappearance, rescue, or relocation. Likewise an item
    transfer changes future causality and needs a state transition, not prose.
    """
    if state.get("storyScope") != "source" or not script_generated_package(package):
        return
    bound_character_ids = {
        details["characterId"] for details in state_character_locations(package, state).values()
    }
    unbound_names = [
        character.get("name") for character in package.get("characters", [])
        if character.get("id") not in bound_character_ids
        and character.get("id") not in state.get("characterOutcomeStates", {})
        and isinstance(character.get("name"), str)
    ]
    terminal_outcome = r"(?:不在(?:这里|现场|[\u4e00-\u9fff]{0,8})?|离开|逃出|获救|死亡|被困(?:在[\u4e00-\u9fff]{0,12})?|到站|进站|抵达|来到|到达|躲在|出现在)"
    protected_local_outcome = r"(?:没有回应|进入|走进|走(?:了|向|进|出|回|到|之前)|回应|回答|出声|敲门|呼救|来过|去往|前往)"
    protected_names = _source_fact_protected_character_names(package, state)
    for name in unbound_names:
        outcome = terminal_outcome
        if name in protected_names:
            outcome = r"(?:" + terminal_outcome[3:-1] + "|" + protected_local_outcome[3:-1] + ")"
        for match in re.finditer(re.escape(name) + r"([^。！？\n]{0,12})" + outcome, text):
            if re.match(r"的(?:电话|手机|号码|名字)", match.group(1)):
                continue
            if re.search(r"(?:他|她|其)", match.group(1)):
                continue
            # Reported train movements are not movements of the speaker.
            if re.search(r"(?:说|提到|表示|解释|回答)[^。！？]{0,6}(?:末班车|列车|火车|班车)$", match.group(1)):
                continue
            if any(other.get("name") in match.group(1) for other in package.get("characters", [])
                   if other.get("name") and other["name"] != name):
                continue
            raise ValueError(f"{name} 的去向或结果尚未由状态登记，正文不能擅自确认。冲突片段：" + match.group(0))
        if name not in protected_names:
            continue
        # A protected character can be referred to through a pronoun after the
        # name has established the local subject. Treat a claimed movement or
        # contact in that short context as stateful too, including dialogue.
        for sentence in re.split(r"[。！？\n]+", text):
            reference = sentence.rfind(name)
            if reference < 0:
                continue
            tail = sentence[reference + len(name):]
            # A name is not the antecedent of every pronoun in the next 120
            # characters. Stop at sentence boundaries and explicit subjects.
            if any(other.get("name") in tail for other in package.get("characters", [])
                   if other.get("name") and other["name"] != name):
                continue
            match = re.search(r"(?:他|她|其)([^。！？\n]{0,12})" + outcome, tail)
            if match and not re.search(r"(?:没有|未|不|是否|可能)", match.group(1)):
                raise ValueError(f"{name} 的去向或结果尚未由状态登记，正文不能通过代词擅自确认。冲突片段：" + sentence)
    inventory_names = {
        item.get("name") for item in package.get("items", [])
        if isinstance(item.get("name"), str) and item.get("id") in set(state.get("inventory", []))
    }
    transferable_names = [
        item.get("name") for item in package.get("items", [])
        if isinstance(item.get("name"), str) and item.get("name") not in inventory_names
    ]
    character_pattern = "|".join(
        re.escape(character["name"])
        for character in package.get("characters", [])
        if isinstance(character.get("name"), str)
    )
    if not character_pattern:
        character_pattern = "[\u4e00-\u9fff]{2,4}"
    ledger_owners = {}
    if package.get('world', {}).get('narrativeGuidelines', {}).get('ledgerItemAvailability'):
        for entry in state.get(BRANCH_LEDGER_KEY, {}).get('entries', []):
            if entry.get('kind') == 'item' and isinstance(entry.get('after'), dict) and entry['after'].get('ownerCharacterId'):
                ledger_owners[entry['entityId']] = entry['after']['ownerCharacterId']
    for item_name in transferable_names:
        transfer = re.compile(
            r"(?:" + character_pattern + r")[^。！？\n]{0,16}(?:递来|递给|交给|塞给|交到)[^。！？\n]{0,16}" + re.escape(item_name)
        )
        for match in transfer.finditer(narration_outside_dialogue(text)):
            item_id = next(item['id'] for item in package['items'] if item['name'] == item_name)
            receiver = re.search(r'(?:递来|递给|交给|塞给|交到)(?:了)?(?P<name>你|' + character_pattern + ')', match.group())
            receiver_id = (state.get('playerCharacterId') if receiver and receiver['name'] == '你' else
                           next((c['id'] for c in package['characters'] if receiver and c['name'] == receiver['name']), None))
            if receiver_id is None or ledger_owners.get(item_id) != receiver_id:
                raise ValueError(f"{item_name} 的交接尚未由状态登记，正文不能擅自发生。")


def _source_fact_protected_character_names(package: Dict[str, Any], state: Dict[str, Any]) -> set[str]:
    """Return names whose source facts make even local contact stateful.

    A source fact such as a missing character's unreachable phone means prose
    cannot make that character answer or arrive. Other visible source roles can
    still speak or take a local step in a scene unless an authoritative state
    fact says otherwise. The rule is source-derived, not tied to any title.
    """
    progress = state.get("sourceProgress")
    current_index = int(progress.rsplit("_", 1)[-1]) if isinstance(progress, str) and progress.rsplit("_", 1)[-1].isdigit() else None
    status_markers = ("无法接通", "没有回应", "失联", "失踪", "下落不明", "被困", "失去联系")
    protected: set[str] = set()
    for fact in package.get("world", {}).get("immutableFacts", []):
        if not isinstance(fact, dict) or not isinstance(fact.get("text"), str):
            continue
        fact_progress = fact.get("sourceProgress")
        fact_index = int(fact_progress.rsplit("_", 1)[-1]) if isinstance(fact_progress, str) and fact_progress.rsplit("_", 1)[-1].isdigit() else None
        if current_index is not None and fact_index is not None and fact_index > current_index:
            continue
        if not any(marker in fact["text"] for marker in status_markers):
            continue
        unreachable = re.search(r"([\u4e00-\u9fff]{2,3})的(?:电话|手机|通讯|联络)[^。！？]{0,12}(?:无法接通|无法联系|失去联系)", fact["text"])
        if unreachable:
            protected.add(unreachable.group(1))
            continue
        protected.update(
            character["name"] for character in package.get("characters", [])
            if isinstance(character, dict) and isinstance(character.get("name"), str) and character["name"] in fact["text"]
        )
    return protected


def source_fact_protection_context(package: Dict[str, Any], state: Dict[str, Any]) -> str:
    """Render source-derived restrictions for unresolved character facts."""
    protected_names = _source_fact_protected_character_names(package, state)
    if not protected_names:
        return "- 无"
    status_markers = ("无法接通", "没有回应", "失联", "失踪", "下落不明", "被困", "失去联系")
    facts = [
        fact["text"]
        for fact in package.get("world", {}).get("immutableFacts", [])
        if isinstance(fact, dict)
        and isinstance(fact.get("text"), str)
        and any(name in fact["text"] for name in protected_names)
        and any(marker in fact["text"] for marker in status_markers)
    ]
    names = "、".join(sorted(protected_names))
    fact_text = "；".join(facts) if facts else "母本已将其状态保留为未确定"
    return (
        f"- {names}：{fact_text}。不得补写其过去、当前或之后的到达、离开、路线、位置、对话、回应或行动；"
        "也不得通过“他/她/其”等代词间接断言。已登记的历史通信原句可以准确引用，这不代表重新取得联系。只能写已确认人物的寻找、联络未果或基于现状的反应。"
    )


def guard_unregistered_persistent_items(text: str, package: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Reject new durable props before prose can make them future canon.

    This is a category guard, not a list of one story's props: source packages
    may only reuse objects that the compiler has registered, while incidental
    scenery remains free to be described without a new state record.
    """
    if state.get("storyScope") != "source" or not script_generated_package(package):
        return
    known_names = {
        item.get("name") for item in package.get("items", []) + state.get("derivedItems", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    inventory_names = {
        item.get("name") for item in package.get("items", []) + state.get("derivedItems", [])
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item.get("id") in set(state.get("inventory", []))
    }
    owner_ids = state.get(ITEM_OWNER_MAP_FIELD, {})
    owner_item_names = {
        item.get("name") for item in package.get("items", [])
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(owner_ids, dict)
        and item.get("id") in owner_ids
    }
    # A source item may become available through a validated scene event,
    # without pretending that the player owned it at session creation.
    available_ids = {
        e.get('entityId') for e in state.get(BRANCH_LEDGER_KEY, {}).get('entries', [])
        if e.get('kind') == 'item' and isinstance(e.get('after'), dict)
        and e['after'].get('available') is True
    }
    if package.get('world', {}).get('narrativeGuidelines', {}).get('ledgerItemAvailability'):
        owner_item_names.update(item['name'] for item in package.get('items', []) if item['id'] in available_ids)
    durable_terms = (
        "手电筒", "手电", "电筒", "门禁卡", "定位标签", "钥匙", "通行证", "录音笔", "图纸", "档案",
        "日志", "文件袋", "信件", "账本", "令牌", "玉佩", "长剑", "药瓶", "纸团", "纸条", "纸片",
    )
    narrative = narration_outside_dialogue(text)
    for term in durable_terms:
        if term not in narrative or not _new_persistent_reference(narrative, term):
            continue
        if not any(term in name for name in known_names):
            raise ValueError("剧情正文引入了未登记的可持续物品: " + term)
        if not any(term in name for name in inventory_names | owner_item_names):
            raise ValueError("剧情正文将未确认取得的可持续物品写成可用实体: " + term)
    for term in ("脚印", "划痕", "暗格", "暗门", "密道", "秘密通道", "布料"):
        if term in narrative and _new_trace_reference(narrative, term):
            raise ValueError("剧情正文引入了未登记的可取证痕迹或隐藏出入口：" + term)
    known_locations = {
        location["name"] for location in package.get("locations", [])
        if isinstance(location, dict) and isinstance(location.get("name"), str)
    }
    location_pattern = r"([\u4e00-\u9fff]{1,6})(?:通道|隧道|地下室|密室|值班室|配电间|配电室|机房|储物间|库房|暗室|夹层)"
    for match in re.finditer(location_pattern, narrative):
        # The CJK span can include the preceding clause (“影子被通道…”).
        # Keep the noun after the grammatical marker; a named new passage
        # (“秘密通道”) still remains a new location and is checked below.
        candidate = re.split(r'[被把]', match.group(0))[-1]
        previous_break = narrative.rfind("\n\n", 0, match.start())
        paragraph_start = previous_break + 2 if previous_break >= 0 else 0
        paragraph_end = narrative.find("\n\n", match.end())
        paragraph = narrative[max(0, paragraph_start):paragraph_end if paragraph_end >= 0 else len(narrative)]
        if (re.fullmatch(r"(?:是|一条|一段|条|段|的)*[窄宽长短]+的(?:通道|隧道)", candidate)
                and any(name.endswith(("通道", "隧道")) and name in paragraph for name in known_locations)):
            continue
        if (
            candidate not in {"这条通道", "这段通道", "这条隧道", "这段隧道"}
            and not any(candidate in name or name in candidate for name in known_locations)
            and _new_location_reference(narrative, candidate)
        ):
            raise ValueError("剧情正文引入了未登记的可进入地点: " + candidate)


def _sentences_containing(text: str, term: str) -> List[str]:
    return [sentence for sentence in re.split(r"(?<=[。！？])", text) if term in sentence]


def _new_persistent_reference(narrative: str, term: str) -> bool:
    """Distinguish a new usable prop from an atmospheric passing mention.

    The state ledger does not need to record rain, light, or a background
    sound. It must, however, own an object once the prose discovers, takes,
    transfers, operates, or frames it as a clue. This keeps the guard focused
    on future causality rather than flattening ordinary scene description.
    """
    action_markers = (
        "发现", "找到", "取出", "拿出", "拿起", "捡起", "拾起", "藏起", "收起", "塞进",
        "递来", "递给", "交给", "握住", "攥住", "插入", "转动", "打开", "解锁", "撬开",
        "线索", "证据", "遗留", "留下", "一把", "这把", "那把",
    )
    for sentence in _sentences_containing(narrative, term):
        if term == "钥匙" and any(suffix in sentence for suffix in ("钥匙孔", "钥匙扣声")):
            continue
        if any(marker in sentence for marker in action_markers):
            return True
    return False


def _new_trace_reference(narrative: str, term: str) -> bool:
    """Return whether a trace is being promoted into a new reusable clue."""
    clue_markers = (
        "发现", "辨认", "循着", "顺着", "指向", "通向", "尽头", "入口", "线索", "证据",
        "留下", "遗留", "新鲜", "新近", "刚刚", "取下", "捡起", "对照", "追踪",
    )
    sentences = _sentences_containing(narrative, term)
    if term == "布料":
        sentences = [sentence for sentence in sentences if re.search(
            r"(?:一块|一片|撕下|取下|遗留|留下的|挂着的)[^。！？]{0,8}布料|布料[^。！？]{0,12}(?:线索|证据|指向|遗留)", sentence,
        )]
    return any(any(marker in sentence for marker in clue_markers) for sentence in sentences)


def _new_location_reference(narrative: str, location_name: str) -> bool:
    """Return whether prose makes an unregistered place actionable this turn."""
    action_markers = (
        "发现", "找到", "进入", "走进", "走回", "回到", "抵达", "前往", "赶到", "通往", "尽头", "入口",
        "门后", "门口", "门开", "穿过", "躲进",
    )
    for sentence in _sentences_containing(narrative, location_name):
        # An unknown entrance is not a discovered entrance. Limit this to
        # locative questions, preserving affirmative actions in other clauses.
        clauses = re.split(r"[，,；;]", sentence)
        for clause in clauses:
            if location_name not in clause:
                continue
            unknown = re.search(
                r"(?:入口|位置|在哪|在哪里|在哪儿|在何处)[^。！？，；]{0,10}(?:不知道|不清楚|无法确定|未能确认)"
                r"|(?:不知道|不清楚|无法确定|未能确认)[^。！？，；]{0,16}(?:在哪|在何处|入口的位置)", clause,
            )
            if unknown and not re.search(r"(?:进入|走进|抵达|穿过|发现了|找到了)", clause):
                continue
            negated_action = r"(?:没有|没|尚未|还未|未曾|并未|无法|不能|不曾)(?:能|能够|成功)?(?:再|继续)?(?:" + "|".join(action_markers) + r")"
            affirmative = re.sub(negated_action, "", clause)
            if re.search(negated_action, clause) and not any(
                marker in affirmative for marker in ("发现", "找到", "进入", "走进", "走回", "回到", "抵达", "前往", "赶到", "穿过", "躲进")
            ):
                continue
            if any(marker in affirmative for marker in action_markers):
                return True
        # A following clause can complete an action on this location.
        if any(re.search(r"(?:随后|然后|便|就|却|仍然|已经)?(?:走进|进入|穿过|抵达)(?:了|那里|其中|里面)", c)
               for c in clauses if location_name not in c):
            return True
    return False


def guard_narrative(
    text: str,
    state: Dict[str, Any],
    character_details: List[Dict[str, str]],
    narrative_guidelines: Optional[Dict[str, Any]] = None,
    package: Optional[Dict[str, Any]] = None,
    *, check_final_locations: bool = True,
) -> None:
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
            if not state_matches(assertion["when"], state):
                continue
            focal_id = state.get('playerCharacterId') or (narrative_guidelines or {}).get('focalCharacterId')
            focal_name = next((c['name'] for c in package.get('characters', []) if c['id'] == focal_id), None)
            communication_conflict = _communication_fact_conflict(text, assertion, focal_name,
                [c['name'] for c in package.get('characters', [])])
            if communication_conflict:
                raise ValueError(assertion["message"] + "冲突片段：" + communication_conflict)
            for pattern in assertion["forbiddenPatterns"]:
                for violation in re.finditer(pattern, text):
                    if _vehicle_departure_is_intended(text, violation):
                        continue
                    raise ValueError(assertion["message"] + "冲突片段：" + violation.group(0))
        if check_final_locations:
            guard_character_final_locations(text, package, state)
        guard_unbound_source_outcomes(text, package, state)
        guard_unregistered_communication(text, package, state)
        guard_unregistered_persistent_items(text, package, state)


def _vehicle_departure_is_intended(text: str, match: re.Match) -> bool:
    """Distinguish a requested departure from a completed traffic change."""
    if not re.match(r"(?:列车|火车|班车|客车|货车|船只|轮船)", match.group(0)):
        return False
    suffix = text[match.end():match.end() + 2]
    if re.search(r"已经|正在|开始|随即|终于|刚刚|刚过|已离站", match.group(0)) or suffix.startswith(("了", "过")):
        return False
    prefix = re.split(r"[。！？；\n]", text[:match.start()])[-1]
    return bool(re.search(
        r"(?:想让|要让|准备让|打算让|希望|要求|[“\"]让)(?:末班|这趟|那趟)?$", prefix,
    ))


def _communication_fact_conflict(text: str, assertion: Dict[str, Any], player_name=None, character_names=()) -> Optional[str]:
    """Reject fabricated last-contact timestamps and substituted source messages."""
    claim = assertion.get("communicationTimestamp")
    if not isinstance(claim, dict):
        return None
    name = claim.get("characterName")
    known_times = claim.get("knownMessageTimes")
    if not isinstance(name, str):
        return None
    known = {
        value for value in known_times
        if isinstance(value, str) and value
    } if isinstance(known_times, list) else set()
    if known:
        communication_terms = ("消息", "语音", "短信", "来电", "电话", "未接通")
        for sentence in (part for part in re.split(r"(?<=[。！？])", text) if part):
            if name not in sentence or not any(term in sentence for term in communication_terms):
                continue
            if "最后一条" not in sentence and "最后那条" not in sentence:
                continue
            explicit_times = re.findall(r"[零一二三四五六七八九十两]+点[零一二三四五六七八九十两]+分|\d{1,2}[:：]\d{2}", sentence)
            if explicit_times and not all(time in known for time in explicit_times):
                return sentence.strip()
    known_messages = claim.get("knownMessageTexts")
    if isinstance(known_messages, list):
        normalized_messages = {
            re.sub(r"[\s，,。！？:：、]", "", value)
            for value in known_messages if isinstance(value, str) and value
        }
        for match in re.finditer(r"[“\"]([^”\"]{1,240})[”\"]", text):
            prefix = text[:match.start()]
            # Only a quote directly introduced by a communication paragraph is
            # message content. A later speaker's dialogue is not that message.
            paragraph = re.split(r"\n\s*\n", prefix.rstrip())[-1][-400:]
            if name not in paragraph or not any(term in paragraph for term in ("语音", "短信", "消息", "录音")):
                continue
            if not paragraph.endswith(("：", ":", "——")) and not re.search(
                r"(?:语音|短信|消息|录音)(?:[，,]|[^。！？]{0,20}(?:响起|传来|写着)[，,]?)?$", paragraph,
            ):
                continue
            if re.search(r"[”\"]", paragraph):
                continue
            last_sentence = re.split(r"[。！？]", paragraph)[-1]
            if (not re.search(r'语音|短信|消息|录音(?!笔)', last_sentence)
                    and not re.search(r'声音[^。！？]{0,25}[：:]\s*$', last_sentence)):
                # Holding/putting down a recorder is not playing a message.
                # Require an actual message or voice introduction at this quote.
                continue
            voice_names = '|'.join(map(re.escape, [*character_names, '你']))
            voices = list(re.finditer('(' + voice_names + r')的声音[^。！？]{0,25}$', last_sentence))
            if voices:
                voice = player_name if voices[-1][1] == '你' else voices[-1][1]
                if voice and voice != name:
                    # A different recording's explicit speaker is not the
                    # protected character's original message.
                    continue
            if (re.search(r"(?:说|问|回答|回应|开口)(?:他|她|你|[\u4e00-\u9fff]{2,3})?[：:，,]\s*$", last_sentence)
                    and not any(term in last_sentence for term in ("语音", "短信", "消息", "录音"))):
                # An intervening action can separate the speaker from “说”.
                # Mentioning a recorder earlier in the paragraph does not turn
                # this person's face-to-face speech into a stored message.
                continue
            speaker = re.search(r"([\u4e00-\u9fff]{2,3})(?:说|问|回答)[：:，,]?\s*$", last_sentence)
            if re.fullmatch(r"(?:他|她|[\u4e00-\u9fff]{2,3})(?:说|问|回答)[：:，,]?\s*", last_sentence):
                # A new explicit speaker attribution closes the earlier message
                # reference, including a pronoun such as "他问：".
                continue
            if speaker and speaker.group(1) not in (name, "她", "他"):
                continue
            content = re.sub(r"[\s，,。！？:：、]", "", match.group(1))
            if content and not any(content in message for message in normalized_messages):
                return match.group(0)
    anchors = claim.get("knownMessageAnchors")
    if not isinstance(anchors, list):
        return None
    known_anchors = {value for value in anchors if isinstance(value, str) and value}
    if not known_anchors:
        return None
    # Require an actual content attribution. Merely mentioning a message and
    # questioning its credibility does not assert what that message contained.
    attribution = re.compile(
        re.escape(name) + r"[^。！？；，,\n]{0,40}(?:语音|短信|消息|来电|电话)"
        r"(?:里|中|里面)?(?:的内容)?(?:[，,](?:她|他))?(?:说|写着|内容是|内容为|提到|表示|告诉)"
        r"[^。！？；\n]{1,120}"
    )
    for match in attribution.finditer(text):
        if not any(anchor in match.group(0) for anchor in known_anchors):
            return match.group(0)
    return None


def guard_source_character_names(
    text: str, package: Dict[str, Any], state: Dict[str, Any], additions: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    session_persona: Optional[Dict[str, str]] = None,
) -> None:
    """Keep names tracked by this branch, while allowing declared additions."""
    if state["storyScope"] != "source":
        return
    additions = additions or empty_branch_additions()
    known = {character["name"] for character in package["characters"]}
    known.update(character["name"] for character in state.get("derivedCharacters", []))
    known.update(character["name"] for character in additions["characters"])
    known.update(reveal["name"] for reveal in state.get("derivedCharacterReveals", []))
    known.update(reveal["name"] for reveal in additions["characterReveals"])
    if session_persona and isinstance(session_persona.get("name"), str):
        known.add(session_persona["name"])
    generic_suffixes = ("人", "工", "员", "者", "客", "长", "们", "面")
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
        "慢慢", "渐渐", "依旧", "一直", "已经", "立刻", "终于", "刚刚", "任他", "任她",
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
        "- 状态中的地点、人物状态、道具归属和已发生事件均为事实；可补充合理过程与环境细节，新增因果实体必须先由运行时登记。",
        "- 对尚未由状态登记去向的原著角色，只能维持未知或等待核验，不能自行确认其离开、获救、被困或抵达某处；未登记的道具交接同样不能发生。",
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


def guard_repeated_paragraphs(text: str) -> None:
    paragraphs = [re.sub(r"\s+", "", value) for value in re.split(r"\n\s*\n", text)]
    seen = set()
    repeated = 0
    for paragraph in paragraphs:
        if len(paragraph) < 60:
            continue
        if paragraph in seen:
            repeated += len(paragraph)
        seen.add(paragraph)
    if repeated > max(400, narrative_character_count(text) * 0.3):
        raise ValueError("剧情正文存在大段循环重复，不能以重复段落补足篇幅。")


def trim_repeated_tail(text: str) -> str:
    """Recover a unique prefix only when every remaining paragraph is a loop.

    Keep the first complete cycle. Never discard a new ending or deduplicate
    repetitions in the middle of a chapter, which could change its meaning.
    """
    try:
        guard_repeated_paragraphs(text)
        return text
    except ValueError:
        pass
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    for index, paragraph in enumerate(paragraphs):
        if len(paragraph) < 60:
            continue
        for start in range(max(0, index - 8), index):
            if paragraphs[start] != paragraph:
                continue
            cycle = paragraphs[start:index]
            tail = paragraphs[index:]
            if len(tail) < len(cycle) * 2:
                continue
            if all(part == cycle[offset % len(cycle)] or (
                offset == len(tail) - 1 and cycle[offset % len(cycle)].startswith(part)
            ) for offset, part in enumerate(tail)):
                return "\n\n".join(paragraphs[:index])
    guard_repeated_paragraphs(text)
    return text


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


def _mock_player_facing_action(value: str) -> str:
    """Keep the structural fixture readable in the player's second-person POV."""
    action = re.sub(r"^自定行动[：:]\s*", "", str(value or "").strip())
    return action.replace("我", "你")


class MockPlanner(Planner):
    @staticmethod
    def _stream_text(text: str, stream: Callable[[str], None], chunk_size: int = 96) -> None:
        """Make fixture and pre-generated prose observable as real deltas."""
        for start in range(0, len(text), chunk_size):
            stream(text[start:start + chunk_size])

    def plan(self, context: Dict[str, Any], selected: Dict[str, Any], resolved_state: Dict[str, Any], stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None, repair: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        title = selected["title"]
        narrative = self._narrative(context["package"], selected, resolved_state)
        if narrative_character_count(narrative) < 2000:
            narrative += self._chapter_extension(context["package"], selected, resolved_state)
        next_directions = self._next(context, selected, resolved_state)
        result = plan_result(context, selected, narrative, title, next_directions, "medium")
        result["planning"]["narrativeOrigin"] = "mock_structural_fixture"
        if stream:
            self._stream_text(narrative, stream)
        return result, None

    @staticmethod
    def _taixu_progress_scene(progress: int, action: str, location: str) -> str:
        """Keep the browser fixture finite while exercising real player turns."""
        if progress >= 4:
            return (
                f"你按自己的决定带着登记簿和纸包离开{location}，没有把尚未核实的猜测留在试炼场里。"
                "换班核查的人在山门内侧接过登记簿，先对照页码、时间和执事承认的异响，再确认纸包里的金屑仍由你本人保管。"
                "他没有替你判断是谁改过封条，只在交接栏写下‘待核’二字，并请你说明每一项记录来自哪里。\n\n"
                "你把经过按顺序说清：木牌刻痕里的金屑、黄纸边角的新浆痕、登记簿被涂改的空白，以及青露草根部与旧剑痕同向的浅沟。"
                "陆照临补充了他亲眼看见的部分，执事则在听到‘昨夜异响’时沉默了一瞬。核查人把这些内容分成已确认和待查两栏，"
                "没有让任何一个人的推测冒充结论。\n\n"
                "交接完成后，山门外的雨声渐渐变轻。你回头看见试炼场的白线和木栅栏缩在雾里，青露草的叶尖还挂着水珠，"
                "却已经不再需要你用下一次行动去证明刚才发生过什么。第一场外门试炼的规则已经完成，相关记录也有了负责继续核查的人；"
                "木牌和金屑留下的疑问没有被抹掉，而是被准确地放进了后续查验的卷宗。\n\n"
                "陆照临问你还要不要回去再看一眼。你摇头，把纸包收进内袋，告诉他现在最重要的是让交接记录先走完流程。"
                "他没有催你追上新的线索，只把刚才记下的页码复述一遍。你们在山门下停了片刻，确认彼此记住的是同一组事实，"
                "然后沿着湿滑的石阶下山。直到铜铃声被雨幕隔开，你才意识到这段试炼真正留下的，不是一个方便的答案，"
                "而是一份能够交给后来者继续核验的记录。"
            )
        if progress == 2:
            return (
                f"你没有立刻回到白线内，而是按住木牌，沿着{location}东侧的石壁走到登记台前。"
                "你说明要核对昨夜异响的时间，并请负责登记试炼物品的弟子先查阅纸面记录。"
                "他翻开湿透的簿册，指给你看一行被墨水反复涂改过的空白：时间写在子时，物品栏却没有对应的编号。\n\n"
                "陆照临站在你身后半步，没有替你解释，也没有把木牌交出去。你让登记弟子只核对纸面上的内容，"
                "不要求他猜测是谁动过封闭区域。那一行空白旁边压着半片青露草叶，叶脉里粘着比纸包更粗的金色碎屑。\n\n"
                "你把碎屑的位置记下，随后回头看向执事。执事终于承认，昨夜确实有人报过异响，"
                "但登记簿没有写名字，也没有写明封条是否被重新换过。这个承认没有替你找出幕后的人，却让木牌、黄纸和药草有了同一个需要核对的时间点。\n\n"
                "铜铃已经响过两次，试炼队列开始向药圃移动。你把纸包收好，决定带着这份记录完成眼前的取药要求，"
                "同时让陆照临记住登记簿的页码。你们没有闯入封闭区域，也没有把猜测说成证据；下一步只剩下在规则允许的范围内取回青露草，并看清木牌究竟会把你们引向哪里。"
            )
        return (
            f"你带着登记簿上的页码回到试炼队列，先按规矩越过石径，前往药圃寻找青露草。雨势压低了山风，"
            "可木牌刻痕里的金屑在纸包中轻轻作响，提醒你刚才的异常还没有结束。陆照临替你看住身后的白线，"
            "你沿着药草边缘逐株核对叶脉，不抢先采摘，也不把封闭区域的动静带进尚未发生的结论。\n\n"
            "你很快找到一株被踩弯的青露草。根部泥土里没有新的金粉，只有一道与石壁旧剑痕方向相同的浅沟。"
            "你先拍下泥痕的位置，再按试炼要求取下完整叶片。执事在远处看见了你的动作，没有再阻拦，只让登记弟子把页码和时间一并记下。\n\n"
            "回到白线内时，铜铃响起最后一声。你把青露草和木牌分别交验，纸包仍留在自己手里。"
            "执事确认试炼物品无误，也承认封闭区域的异响会在换班后重新核查。这个结果没有解释全部金屑，"
            "却把你能确认的事实保留下来：木牌没有被调包，登记簿确有涂改，昨夜有人靠近过封闭区域，而你已经在规则内完成了第一场试炼。\n\n"
            "陆照临问你接下来要不要继续追查。你看了一眼纸包，没有急着给出答案。眼前的试炼已经结束，"
            "但这条线索仍然属于你的选择；你可以带着记录离开，也可以在下一段故事里继续追问。"
        )

    @staticmethod
    def _taixu_progress_extension(progress: int, location: str) -> str:
        if progress == 2:
            return (
                f"你没有把登记簿上的空白当成答案。回到{location}边缘后，你让陆照临复述他看见的页码、时间和那片青露草叶，"
                "逐项确认哪些是他的亲眼所见，哪些只是你们根据金屑方向作出的推测。登记弟子愿意替你作证，但他不愿在没有执事签名的情况下把簿册交出。\n\n"
                "你接受了这个限制，只请他在页码旁写下核对时间。执事站在雨幕里观察你们，没有再要求立即交回木牌。"
                "你趁这个间隙检查黄纸的边角，发现新换的封条比旧纸薄了一层，黏合处还留着未干的浆痕。它说明有人动过封条，却不能说明那个人是谁。\n\n"
                "铜铃催促队列继续。你把纸包贴身收好，把木牌放回掌心，和陆照临一同退回白线内。你们约定先完成试炼，"
                "再按登记簿留下的时间寻找换班记录。这个约定没有替你们建立信任，却给下一步留下了可核对的顺序：先取药、再验物、最后追问封闭区域。\n\n"
                "你回头看了一眼那道栅栏。黄纸在风里掀起一角，露出下面旧封条的一小段黑线。你没有伸手撕开它，"
                "只把这个细节告诉陆照临，然后跟着队列向药圃走去。直到脚步离开石壁，你仍能听见登记台旁翻动纸页的声音。"
            )
        return (
            f"你把青露草交到登记台上，先说明根部浅沟和木牌金屑之间只是方向相同，不能直接当成同一来源。"
            "执事核验叶片完整，登记弟子按页码补写时间；两个人的记录终于落在同一张纸上。没有人替你宣布幕后真相，"
            "也没有人因为你追问封条就取消你的资格。\n\n"
            "陆照临把纸包推回你手边，问你是否要把金屑一并交验。你摇头，说明纸包是你刮下的样本，当前只愿把它作为后续核对的线索。"
            "他没有逼你改变决定，只提醒你，若下一次再靠近封闭区域，最好先找到能确认换班记录的人。\n\n"
            "雨势渐缓，药圃里的新弟子开始散开。你把登记簿页码、浅沟位置和执事承认的异响按顺序记在心里，"
            "再确认木牌仍在自己手中。试炼的规则已经完成了它要求的部分：你取回了青露草，没有越线，没有争抢，也没有把未核实的怀疑写成事实。\n\n"
            "你和陆照临一起离开白线。身后的栅栏重新沉入雨雾，黄纸边角却在风里留下最后一次翻动。"
            "这段经历可以在这里暂时收束，已确认的记录会跟着你保留下来；若你继续前行，下一次选择将从这些事实开始，而不是从重复的试炼场景开始。"
        )

    @staticmethod
    def _taixu_terminal_extension(location: str) -> str:
        """Give the fixture's natural ending enough room without replaying its last scene."""
        return (
            f"下山前，你在{location}外的檐下把交接内容重新核对了一遍。纸包封口完整，登记簿的页码、异响时间和青露草的浅沟位置都已经写入核查单，"
            "负责换班的人还在末尾补上了经手人的名字。每一项记录都有来源，每一项尚未确认的内容也都保留着待查标记；"
            "这让你不必用自己的判断替代流程，也让真正负责的人知道应该从哪一页、哪一道封条开始复核。\n\n"
            "你没有把执事的迟疑解释成认罪。那只是当时被你们共同看见的反应，是否与封闭区域有关，还要等换班记录和现场痕迹对上。"
            "同样，金屑与剑痕方向相同，也只能说明两处留下了值得比较的痕迹。你把这些边界讲给核查人听，"
            "他因此把纸包列为暂存样本，而不是把它直接送进结论栏。\n\n"
            "陆照临站在檐外，替你挡住从山道上卷来的风。你们没有再争论要不要立刻折返，因为这一次离开并不等于放弃。"
            "你已经完成了能够在规则内完成的部分，也把下一次调查需要的入口交给了正确的人。若封闭区域真的有人动过，"
            "后续核查会留下新的时间和经手记录；若只是旧木、旧纸和雨水造成的错觉，记录同样会给出可以复核的解释。\n\n"
            "山门里的灯一盏盏亮起来，试炼场的弟子陆续散去。你回想起最初把金屑刮下来的那一刻，"
            "当时你只有一个难以证实的疑问；现在，疑问仍然存在，却已经不再孤零零地悬在你手里。它有页码、有时间、有见证人，"
            "也有明确的后续责任。故事没有用突然揭开的真相替你省略这段过程，而是让每一个人都带着自己亲眼看见的部分离开现场。\n\n"
            "你和陆照临沿石阶往下走。半山的雾遮住了栅栏，雨水冲淡了泥里的脚印，只有纸包边缘微微硌着你的指节。"
            "你知道这条路线在这里收束，并不是因为所有问题都已经有了答案，而是因为当前能由你承担的行动已经完成，"
            "剩下的核验已经交到有权限继续处理的人手里。你最后看了一眼山门，确认登记簿没有被带走，便转身走进夜色。\n\n"
            "走到第一处转弯时，你听见身后有人喊你的名字。换班核查的人站在门檐下，手里举着刚盖好印的交接单，"
            "告诉你副本已经入档，若后续记录与今天的页码对不上，会按这份交接单追溯经手人。你点头收下这句确认，"
            "没有再要求他当场给出谁动过封条的答案。答案要靠记录之间的相互印证，而不是靠离场前的一次猜测。\n\n"
            "陆照临把自己的记忆又整理了一遍：他看见过黄纸边角、听见过栅栏后的金属声，也看见你始终没有越过白线。"
            "你们把这些内容和登记簿上的文字逐项比对，发现没有一项需要为了让故事完整而被夸大。那一点克制让你们都松了口气，"
            "因为真正的后续调查必须从能够被两个人同时复述的事实开始。\n\n"
            "石阶尽头已经看不见试炼场的灯。你把木牌放回袖中，纸包贴在内袋最稳妥的位置，脚下的水洼映出一线昏黄的山门灯火。"
            "这场雨没有替你冲掉疑问，也没有突然把疑问变成真相；它只是让所有留下的痕迹更加需要被记录。你沿着山道继续下行，"
            "身后的铃声终于完全停下，当前这段故事也在交接完成的事实里安静地合上了。\n\n"
            "你没有把这份安静当作遗忘。明天换班之后，新的记录会从今天的页码接上；如果有人试图改动木牌或封条，"
            "那次改动也必须面对已经入档的时间与经手人。你能做的部分到此为止，正因为边界清楚，后续的人才有可能把事实继续追下去。\n\n"
            "陆照临最后回头看了一眼山门，确认那张交接单已经被收进柜中。他没有再问你要不要返回，"
            "只说下山的路已经记住。你们并肩走远，谁也没有把尚未核实的答案带进这段已经完成的记录。"
            "雨水从檐角落下，身后的灯火逐渐缩成一点，直到被山雾完全遮住。你把这一天的页码牢牢记在心里，脚步也终于不再回望。"
            "山道向下延伸，夜色替你收好剩下的脚印。远处的城灯一盏接一盏亮起，接住你终于完成的这一程，也照亮前路。夜行终有归处。"
        )

    def _narrative(self, package: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> str:
        location = location_name(package, state)
        second_person = package.get("id") == "taixu-relics-part1"
        progress = state.get("freeTextProgress")
        focal_id = package.get("world", {}).get("narrativeGuidelines", {}).get("focalCharacterId")
        focal_name = next((item["name"] for item in package.get("characters", []) if item["id"] == focal_id), "焦点人物")
        focal = "你"
        companions = [name for name, position in state_character_locations(package, state).items()
                      if name != focal_name and position["locationName"] == location]
        companion_text = "、".join(companions) if companions else "身边的人"
        current_beat = beat_for_state(package, state)
        node_id = current_beat.get("nodeId") if current_beat else None
        node = story_node_by_id(package, node_id) or {}
        thread_titles = [item.get("title") for item in state.get("threadLedger", [])
                         if isinstance(item, dict) and item.get("status") == "open" and item.get("title")]
        objective = "；".join(thread_titles[:2]) or node.get("objective", package["story"].get("longTermGoal", "当前目标"))
        pressure = node.get("pressure", {}).get("effect", "仍在累积的压力")
        action = _mock_player_facing_action(selected.get("summary") or selected["title"]) if second_person else str(selected.get("summary") or selected["title"]).strip()
        if second_person and isinstance(selected.get("title"), str) and selected["title"].strip():
            title = re.sub(r"^自定行动[：:]\s*", "", selected["title"].strip())
            action = title.split("。", 1)[0].strip() or action
        if second_person:
            if isinstance(progress, int) and progress >= 2:
                return self._taixu_progress_scene(progress, action, location)
            objective_text = "木牌上的金屑从何而来，以及如何完成这场外门试炼"
            return (
                f"{location}没有因为“{action}”而安静下来。你把木牌压在掌心，沿着石壁走出几步，"
                f"陆照临跟在你身侧，视线越过你的肩头，落在那条不许靠近的封闭区域边缘。{companion_text}没有催你回头，"
                "却也听见了执事重新敲响的铜铃。\n\n"
                f"木牌上的刻痕被雨气浸得发暗。{objective_text}。这两个问题，忽然有了比纸面规矩更具体的重量。"
                "你把纸包里的金屑倒在掌心，细小的亮色很快被风吹散，只留下几粒卡在指缝里的硬痕。"
                "它们不像泥，也不像石壁上剥落的砂砾；每一粒都像是从某件金属物上刮下来的。\n\n"
                f"陆照临压低声音问你要不要先回到白线内。你没有立刻答应，只把木牌翻过来，让他看见刻痕深处那道几乎被黑泥遮住的细口。"
                "他伸手想接，你先收回手腕，说明自己还没有确认这东西的来历。两个人之间留下短暂的停顿，谁也没有把猜测说成答案。\n\n"
                "封闭区域的木栅栏比远处看上去更旧，横木上挂着被雨水打湿的黄纸。纸角没有印记，背面却沾着同样细碎的金粉。"
                "你蹲下来观察地面，发现有人曾在这里停过：泥里有半枚鞋印，鞋尖朝向栅栏，旁边还压着一截折断的草茎。"
                "这不是足以定罪的证据，却说明这道边界最近并非无人经过。\n\n"
                "执事终于看见了你。他从场边走来，先看木牌，再看你脚下的位置，语气比刚才宣布规矩时更冷。山腰的风雨和人群的目光一起压过来，"
                "雨幕里传来石块滚落的闷响，试炼场上等候钟声的弟子纷纷转过身。你没有把木牌递出去，也没有越过木栅栏，只问他是否见过刻痕里的金屑。\n\n"
                "执事的手停在袖口，目光短暂地落向黄纸。他说那只是旧木受潮后的杂色，叫你把东西交回去。"
                "你听出他回答得太快，又追问黄纸为什么换过边角。陆照临站到你与场边之间，既没有替你承认什么，也没有把你拉回白线内。"
                "这份迟疑让围观的人安静下来，连远处的钟声都像被雨水压低了一层。"
            )
        narrative = (
            f"{location}没有因为“{action}”这个决定而立刻安静下来。{focal}先停住脚步，"
            f"和{companion_text}确认眼前能够看见的路径、工具与风险；你们知道这一回合只推进已选择的事情，不能替下一步预支结果。\n\n"
            f"当前要解决的是“{objective}”。这个方向界定了本回合推进的范围，{focal}没有把它当作保证，"
            "而是把它拆成能核验的判断：谁在场，地点是否可达，已确认的线索还缺少什么，以及一旦受阻应当如何留下退路。\n\n"
            "周围的细节仍在提醒你，叙事不能替代事实。已经发生的变化可以被看见、被讨论，也可以带来新的情绪和阻力；"
            "尚未确认的人、物、地点和结果则必须继续保持悬而未决。任何看似省事的跳跃，都会让之后的选择失去可追溯的依据。\n\n"
            f"{focal}把注意力从笼统的愿望收回到当前动作上。{companion_text}各自保留了不同的顾虑，"
            "却都同意先把能确认的部分做扎实：检查环境、说明限制、记录变化，并让新的方向真正对应一个尚未完成的问题。\n\n"
            f"场景之外的{pressure}没有消失。{focal}因此没有宣布胜利，也没有替任何人作出未经选择的承诺；"
            "你只确认这一步已经改变了什么，并把其余问题留给下一次明确的决定。"
        )
        if not second_person:
            narrative = narrative.replace("你们", "他们").replace("你", focal_name)
        return narrative

    def _chapter_extension(self, package: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> str:
        location = location_name(package, state)
        second_person = package.get("id") == "taixu-relics-part1"
        progress = state.get("freeTextProgress")
        if second_person and isinstance(progress, int) and progress >= 2:
            if progress >= 4:
                return self._taixu_terminal_extension(location)
            return self._taixu_progress_extension(progress, location)
        focal = "你"
        if second_person:
            return (
                f"你沿着{location}的石壁慢慢后退半步，把木牌和纸包分开收好。雨水顺着檐角落下，"
                "在脚边积成一圈浅亮的水洼。水面映出栅栏、黄纸和执事的靴尖，也映出陆照临没有移开的目光。\n\n"
                "你让他先说自己看见了什么。他指出黄纸背面有一道被指甲刮过的折痕，折痕下压着极细的黑线；"
                "那条线与木牌刻痕的方向不完全相同，却都避开了正面最容易被人发现的位置。你把这两个细节记住，没有急着把它们拼成同一个答案。\n\n"
                "执事催促你回到试炼队列。你问他封闭区域里是否存放过旧剑或废弃的阵具，他只说那里没有值得看的东西。"
                "回答落下时，栅栏后传来一声金属相碰的轻响，短促得像有人在黑暗里碰倒了什么。执事的肩膀随之绷紧。\n\n"
                "陆照临听见了那声响。他没有直接冲过去，而是把脚停在白线外，问你是要留下来继续问，还是先去找负责登记试炼物品的人。"
                "你看了看手里的木牌：金屑已经被纸包收好，黑泥却在刻痕里重新聚成一小块。那块污痕的边缘，出现了刚才没有的浅白细线。\n\n"
                "你用衣袖挡住木牌，换了一个角度。细线像旧剑划过木面留下的毛刺，方向正对着石壁深处。"
                "石壁上那些年代久远的剑痕被雨水冲得发亮，其中有一道新旧不一，末端停在栅栏投下的阴影里。你沿着它看过去，"
                "没有看见人，却看见一小片被踩扁的青露草。试炼要求取回的药草，本不该出现在这里。\n\n"
                "场边有人开始议论，说你为了查一块木牌耽误了所有人的时间。也有人提醒执事，封闭区域刚才确实传出过声音。"
                "议论像潮气一样贴近过来，你却只让陆照临替你挡住一侧视线，自己蹲下检查草叶上的泥。泥里混着一点金色，"
                "比纸包里的颗粒更粗，像是从某个磨损严重的机关边缘掉下来的。\n\n"
                "你把草叶原样放回，站起身问执事：如果这块木牌只是普通试炼物，为什么它的刻痕会与封闭区域的剑痕相互指向。"
                "这一次，他没有立刻回答。那段沉默足够长，长到陆照临已经把手按在剑鞘上，长到雨幕后的脚步声又响了一次。\n\n"
                "你没有越过木栅栏，也没有把陆照临推到你前面。你只是把木牌举到执事能够看清的位置，要求他先说明自己确认过的事实。"
                "如果他坚持让所有人回到队列，你就会记下他的说法；如果他愿意打开封条，你要先确认里面有没有人、有没有受损的试炼物，以及谁最后一次进入过这里。\n\n"
                "执事终于伸出手，却停在离木牌一掌远的地方。他告诉你，封闭区域昨夜曾有人报过异响，登记簿上没有留下名字。"
                "这句话没有解释金屑从何而来，却让眼前的风险有了新的落点：有人可能在试炼开始前动过木牌，也可能把不该出现的药草带到了边界。\n\n"
                "陆照临看向你，等你决定是否继续追问。铜铃再次响起，山道上的雨势忽然加重，石壁深处传来细微的水声。"
                "你把纸包收进内袋，指尖压住木牌背面那道新出现的白线，知道下一步无论向前还是退回，都必须带着这几个尚未核实的事实。"
            )
        extension = (
            f"\n\n{location}中的光线、声音和气味都在不断提醒{focal}：环境并不会为叙事让路。"
            "你先检查能够使用的物件、可撤回的路径和身边人的反应，再决定是否继续把风险交给下一步。"
            "这种确认并不华丽，却让每一项变化都有来源，也让角色的犹豫、分歧和承担能够落在具体事实之上。\n\n"
            f"内容包为此刻声明的状态边界仍然有效。{focal}没有把这些限制当作背景说明，"
            "而是把它们带进每一次观察和对话：什么已经得到证实，什么还只是推测，什么必须等到玩家明确选择后才能发生。\n\n"
            "短暂的安静没有消除压力，反而让未解的问题显出轮廓。有人提出可能的办法，也有人指出其代价；"
            "你们因此把计划拆开，先核实最接近当前场景的细节，再保留对后续走向的判断。这样，故事可以继续推进，"
            "却不会用一句方便的结论覆盖仍未解决的因果。\n\n"
            f"{focal}听完不同意见，没有立刻选择最省力的一条路。你先让每个人说明自己真正看见了什么，"
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
            f"{focal}没有催促谁立刻给出承诺。你让场景保持足够的停顿，先确认眼前的变化已经被共同看见，"
            "再说明这一回合到此为止。这样做不是拖延，而是避免把下一次行动的门槛偷偷藏进已经完成的叙述里。"
            "只有当玩家明确选择新的方向，人物才会跨过那条边界，承担随之而来的新事实。\n\n"
            "远处的动静仍在持续，时间和外部压力也没有停止计算。可角色已经不再只是在等待一个偶然的转机；"
            "你们知道应当带着什么问题进入下一段场景，也知道哪些事实必须在转身前被守住。"
            "这种明确并不保证结果，却让每一次失败、让步或收获都有能够回看的原因。\n\n"
            f"{focal}又回头看了一眼{location}。那些看似无关紧要的痕迹仍然留在原处："
            "被反复使用过的边角、被匆忙挪开的物件、说到一半便停住的话。它们未必立刻构成线索，"
            "却提醒每个人，真正可靠的判断需要允许自己暂时不知道。角色可以提出怀疑，可以调整计划，"
            "也可以承认此前的选择并不充分；唯一不能做的，是为了尽快结束场景而把不确定性伪装成确定事实。\n\n"
            "于是你们把刚才发生的事重新说了一遍，确认谁看见了什么、谁承担了什么、哪些变化已经留在现场。"
            "这段复盘没有让气氛松弛，反而让原先模糊的风险有了轮廓。每个人都明白，下一次选择不仅会推动目标，"
            "也会决定关系是否值得信任、资源是否还能使用，以及后来的人将如何理解这一刻的因果。\n\n"
            f"{focal}没有要求所有人达成同一种解释。你只要求下一步能够被清楚地说出来："
            "目的是什么，边界在哪里，行动失败后谁需要负责收拾后果。这样的约定让人物仍有分歧，"
            "却不必靠误解或突然出现的万能答案维持冲突。它也给读者留下了判断的余地，"
            "让接下来的方向成为真正的选择，而不是早已被正文替代完成的程序。\n\n"
            "在重新出发之前，你们没有再增加新的事实，只把已知信息按轻重放回心里："
            "眼前能够处理的障碍、尚待核验的说法，以及一旦局势变化就必须优先回应的人。"
            "这份排序并不替代行动，却让行动拥有了清晰的起点。\n\n"
            f"{focal}最后重新确认“{_mock_player_facing_action(selected.get('summary') or selected['title'])}”只完成了本回合应完成的部分。"
            "你把余下的问题留在可选择的方向里，让下一次行动决定谁去做、在哪里做，以及它究竟会改变什么。"
        )
        if not second_person:
            focal_name = next((item["name"] for item in package.get("characters", []) if item["id"] == package.get("world", {}).get("narrativeGuidelines", {}).get("focalCharacterId")), "焦点人物")
            extension = extension.replace("你们", "他们").replace("你", focal_name)
        return extension

    def _next(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
        if (
            context.get("package", {}).get("id") == "taixu-relics-part1"
            and selected.get("isFreeText")
            and isinstance(state.get("freeTextProgress"), int)
            and state["freeTextProgress"] >= 3
        ):
            return []
        return scripted_followup_directions(context, selected, state)


def scripted_followup_directions(
    context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Publish only StoryPackage-defined choices after a completed chapter."""
    if selected.get("isFreeText"):
        progress = state.get("freeTextProgress")
        if not isinstance(progress, int):
            return []
        active_goal = next((goal for goal in state.get("goalLedger", [])
                            if isinstance(goal, dict) and goal.get("status") == "active"
                            and isinstance(goal.get("title"), str) and goal["title"].strip()), None)
        title = "继续：" + active_goal["title"] if active_goal else "继续调查眼前线索"
        summary = ("围绕“" + active_goal["title"] + "”继续行动，依据眼前已确认的事实推进。"
                   if active_goal else "沿着当前已确认的线索继续行动，先核对眼前变化。")
        return [{
            "id": "direction_free_continue_" + str(progress + 1),
            "title": title,
            "summary": summary,
            "statePatch": {"freeTextProgress": progress + 1},
            "isFreeText": True,
        }]
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
    def __init__(self, gateway: OpenAICompatibleGateway, minimum_narrative_characters: int = 0, context_resolver: Optional[Any] = None, verify_source_facts: bool = False, concise: bool = False) -> None:
        self.gateway = gateway
        self.minimum_narrative_characters = minimum_narrative_characters
        self.context_resolver = context_resolver
        self.last_prompt_context: Dict[str, Any] = {"mode": "package"}
        self.writing_scope: Optional[Dict[str, Any]] = None
        self.verify_source_facts = verify_source_facts
        self.concise = concise

    @staticmethod
    def _repairable_semantic_error(error: ValueError) -> bool:
        """Limit rewrite retries to source-causality violations.

        Invalid transport, response shape, perspective, and state-machine
        failures remain immediately visible to callers. A prose-only repair is
        useful only when the model invented an unregistered causal entity or
        claimed an outcome for a source fact intentionally kept unresolved.
        """
        message = str(error)
        return (
            message.startswith("剧情正文引入了未登记")
            or message.startswith("剧情正文将未确认取得")
            or "的去向或结果尚未由状态登记" in message
            or message.startswith("剧情正文与已确认的交通状态矛盾")
            or message.startswith("剧情正文与已确认的通信状态矛盾")
            or message.startswith("剧情正文引入了未登记的通信事实")
            or (message.startswith("剧情正文与已确认状态矛盾") and "最终位置应为" in message)
            or message.startswith("剧情正文将尚未完成的要求写成执行结果")
            or message.startswith("剧情正文事实核对未通过")
            or message.startswith("剧情正文存在大段循环重复")
        )

    @staticmethod
    def _merge_semantic_repair_audit(
        initial_raw_responses: List[str], initial_observations: List[Dict[str, Any]], retry_audit: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = copy.deepcopy(retry_audit)
        retry_observations = []
        for item in merged.get("callObservations", []):
            item = dict(item)
            if item.get("generationStage") == "initial":
                item["generationStage"] = "semantic_repair"
                item["retryReason"] = "source_causality_guard"
            elif item.get("generationStage") == "continuation":
                item["generationStage"] = "semantic_repair_continuation"
            elif item.get("generationStage") == "expansion":
                item["generationStage"] = "semantic_repair_expansion"
            elif item.get("generationStage") == "fact_review":
                item["generationStage"] = "semantic_repair_fact_review"
            elif item.get("generationStage") == "fact_review_format_repair":
                item["generationStage"] = "semantic_repair_fact_review_format_repair"
            elif item.get("generationStage") == "fact_support_review":
                item["generationStage"] = "semantic_repair_fact_support_review"
            retry_observations.append(item)
        merged["rawResponse"] = "\n\n".join(
            response for response in initial_raw_responses + [merged.get("rawResponse", "")]
            if response
        )
        merged["callObservations"] = initial_observations + retry_observations
        return merged

    def plan(self, context: Dict[str, Any], selected: Dict[str, Any], resolved_state: Dict[str, Any], stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None, repair: Optional[str] = None, repair_narrative: Optional[str] = None, repair_replacements: Optional[List[Dict[str, Any]]] = None, repair_conflicts: Optional[List[Dict[str, Any]]] = None, repair_partial_replacements: Optional[List[Dict[str, Any]]] = None) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        raw_responses: List[str] = []
        observations: List[Dict[str, Any]] = []
        generation_stage = "initial"
        rejected_narrative_characters: Optional[int] = None
        source_node_ref = resolve_source_node(context["package"], context["parent"], resolved_state, selected)
        prompt = self._prompt(
            context, selected, resolved_state, repair,
            terminal_source_branch=controlled_followup_directions(
                context["package"], selected, source_node_ref, resolved_state,
            ) == [],
        )
        system_instruction = render_prompt('core.narrative_system')
        if self.writing_scope is not None and not selected.get("isFreeText") and not self.concise:
            system_instruction = (
                render_prompt('core.rewrite_system')
            )
        messages = [{"role": "system", "content": system_instruction}, {"role": "user", "content": prompt}]
        if repair_narrative:
            messages.extend([
                {"role": "assistant", "content": repair_narrative[:6000]},
                {"role": "user", "content": (
                    render_prompt('core.narrative_retry',
                        error=str(repair),
                    )
                )},
            ])
        try:
            fact_review_enabled = self.verify_source_facts and self.writing_scope is not None
            targeted_repair = (fact_review_enabled and repair_narrative
                               and self._repairable_semantic_error(ValueError(str(repair)))
                               and not str(repair).startswith("剧情正文存在大段循环重复"))
            if targeted_repair and len(repair_conflicts or []) > 8:
                raise LlmError(f"事实问题涉及 {len(repair_conflicts)} 段，超过 8 处局部修改上限；未请求局部修订", "model_output_rejected")
            if targeted_repair and repair_conflicts:
                self._check_fact_repair_size(repair_narrative, [item["paragraphId"] for item in repair_conflicts])
            if repair_replacements is not None:
                # Suggestions from the failed review are applied locally, then
                # pass the same guards and a fresh review below. No HTTP retry
                # is invented in the audit for this local operation.
                completion = Completion(json.dumps({"replacements": repair_replacements}, ensure_ascii=False), "", [])
            elif targeted_repair:
                editing_narrative = repair_narrative
                editing_conflicts = repair_conflicts
                if repair_partial_replacements:
                    editing_narrative = self._apply_fact_replacements(repair_narrative, json.dumps({"replacements": repair_partial_replacements}))
                    fixed_ids = {item["paragraphId"] for item in repair_partial_replacements}
                    editing_conflicts = [item for item in repair_conflicts if item["paragraphId"] not in fixed_ids]
                completion = self.gateway.complete_json(self._fact_repair_messages(
                    context, selected, resolved_state, editing_narrative, str(repair), editing_conflicts,
                ))
            else:
                completion = self.gateway.complete_text(
                    messages,
                    None if fact_review_enabled else stream,
                    stream_reset,
                )
            raw_responses.append(completion.raw_response)
            observations.extend({**item, "generationStage": "initial"} for item in completion.observations)
            if targeted_repair and repair_replacements is None and repair_partial_replacements:
                replacements = parse_json_content(completion.content).get("replacements")
                if not isinstance(replacements, list):
                    raise LlmError("事实修订响应格式无效", "model_output_rejected")
                completion = Completion(json.dumps({"replacements": repair_partial_replacements + replacements}),
                                        completion.raw_response, completion.observations)
            narrative = (self._apply_fact_replacements(repair_narrative, completion.content)
                         if targeted_repair else plain_model_narrative(completion.content))
            if targeted_repair:
                self._validate_fact_repair(narrative, repair_conflicts or [])
                observations.append({"outcome": "normalized", "normalization": "applied_fact_replacements"})
            if not narrative:
                raise LlmError("LLM 剧情正文为空", "model_output_rejected")
            rejected_narrative_characters = narrative_character_count(narrative)
            if fact_review_enabled:
                recovered = trim_repeated_tail(narrative)
                if recovered != narrative:
                    observations.append({"outcome": "normalized", "normalization": "removed_repeated_tail",
                                         "removedCharacters": narrative_character_count(narrative) - narrative_character_count(recovered)})
                    narrative = recovered
                    rejected_narrative_characters = narrative_character_count(narrative)
            guard_repeated_paragraphs(narrative)
            partial_issue = None
            before_expansion = None
            expansion_added_text = None
            if rejected_narrative_characters < self.minimum_narrative_characters:
                try:
                    if self.writing_scope is not None:
                        guard_offstage_reports(narrative, self.writing_scope)
                    guard_narrative(
                        narrative, resolved_state, context["characterDetails"],
                        context["package"]["world"].get("narrativeGuidelines"), context["package"],
                        check_final_locations=False,
                    )
                    guard_source_character_names(
                        narrative, context["package"], resolved_state, empty_branch_additions(),
                        context.get("contract", {}).get("persona"),
                    )
                except ValueError as error:
                    if not fact_review_enabled or not self._repairable_semantic_error(error):
                        raise
                    partial_issue = error
            # Review a flawed short draft before expanding it. Its corrected
            # version will return here once, with the semantic retry consumed.
            if rejected_narrative_characters < self.minimum_narrative_characters and partial_issue is None:
                generation_stage = "expansion" if fact_review_enabled else "continuation"
                if fact_review_enabled:
                    continuation_completion = self.gateway.complete_json(self._scene_expansion_messages(
                        context, selected, resolved_state, narrative,
                    ))
                else:
                    continuation_completion = self.gateway.complete_text(
                        [
                            {"role": "system", "content": system_instruction},
                            {"role": "user", "content": self._continuation_prompt(
                                context, selected, resolved_state, narrative,
                            )},
                        ],
                        None,
                        stream_reset,
                    )
                raw_responses.append(continuation_completion.raw_response)
                observations.extend({**item, "generationStage": generation_stage} for item in continuation_completion.observations)
                if fact_review_enabled:
                    before_expansion = narrative
                    narrative = self._apply_scene_expansion(narrative, continuation_completion.content, observations)
                    position = before_expansion.rfind(self._expansion_sentences(before_expansion)[-1])
                    expansion_added_text = narrative[position:position + len(narrative) - len(before_expansion)]
                else:
                    continuation = plain_model_narrative(continuation_completion.content)
                    if not continuation:
                        raise LlmError("LLM 续写正文为空", "model_output_rejected")
                    narrative += "\n\n" + continuation
                rejected_narrative_characters = narrative_character_count(narrative)
                guard_repeated_paragraphs(narrative)
                initial_was_streamed = stream and completion.body_was_streamed
                if initial_was_streamed:
                    stream("\n\n" + continuation)
                if rejected_narrative_characters < self.minimum_narrative_characters:
                    raise LlmError(
                        f"LLM 剧情正文少于 {self.minimum_narrative_characters} 个非空白字符",
                        "model_output_rejected",
                    )
            if repair_conflicts:
                self._validate_fact_repair(narrative, repair_conflicts)
            local_issue = partial_issue
            try:
                guard_narrative(
                    narrative, resolved_state, context["characterDetails"],
                    context["package"]["world"].get("narrativeGuidelines"), context["package"],
                    check_final_locations=partial_issue is None,
                )
                if self.writing_scope is not None:
                    guard_offstage_reports(narrative, self.writing_scope)
                guard_source_character_names(
                    narrative, context["package"], resolved_state, empty_branch_additions(), context.get("contract", {}).get("persona"),
                )
            except ValueError as error:
                if not fact_review_enabled or not self._repairable_semantic_error(error):
                    raise
                local_issue = partial_issue or error
            if fact_review_enabled:
                generation_stage = "fact_review"
                if stream_reset:
                    stream_reset("fact_review")
                review_messages = self._fact_review_messages(
                    context, selected, resolved_state, narrative, str(local_issue) if local_issue is not None else None,
                )
                review_data = json.loads(review_messages[1]["content"])
                if before_expansion is not None:
                    review_data["expandedParagraphId"] = len(self._draft_paragraphs(narrative))
                    review_data["expansionAddedText"] = expansion_added_text
                    review_data["checkExpansion"] = "检查扩写段是否重演了前文已完成的发现、取得物品、移动或询问；明确的回忆和思考不算重新执行。"
                if repair_conflicts:
                    review_data["priorIssues"] = [{"paragraphId": item["paragraphId"], "reason": item.get("reason", "无依据的事实")}
                                                 for item in repair_conflicts]
                if repair_narrative and targeted_repair:
                    old_paragraphs, new_paragraphs = self._draft_paragraphs(repair_narrative), self._draft_paragraphs(narrative)
                    review_data["changedParagraphIds"] = [old["id"] for old, new in zip(old_paragraphs, new_paragraphs) if old["text"] != new["text"]]
                    review_data["checkRepairContinuity"] = "检查修改段的相邻段落是否有无对象的追问、依赖已删除断言的反问；检查当前全文，不能只核对修改段。"
                review_messages[1]["content"] = json.dumps(review_data, ensure_ascii=False)
                original_review = None
                repair_ids = None
                try:
                    for review_attempt in range(2):
                        review_completion = self.gateway.complete_json(review_messages)
                        raw_responses.append(review_completion.raw_response)
                        observations.extend({**item, "generationStage": generation_stage} for item in review_completion.observations)
                        try:
                            reviewed_content = self._normalize_fact_review(review_completion.content)
                            if original_review is not None:
                                reviewed_content = self._merge_fact_review_records(original_review, reviewed_content, repair_ids)
                            evidence = self._fact_evidence(context, selected, resolved_state)
                            self._check_fact_review(reviewed_content, narrative, {item["id"] for item in evidence}, evidence)
                        except LlmError as error:
                            if review_attempt or error.code != "model_output_rejected":
                                raise
                            generation_stage = "fact_review_format_repair"
                            observations.append({"outcome": "retrying", "retryReason": "fact_review_contract", "error": str(error)})
                            repair_ids = getattr(error, "invalid_paragraph_ids", None)
                            if repair_ids:
                                original_review = reviewed_content
                                data = json.loads(review_messages[1]["content"])
                                data["paragraphs"] = [item for item in data["paragraphs"] if item["id"] in repair_ids]
                                data["requiredRuleClaims"] = [item for item in data.get("requiredRuleClaims", []) if item["paragraphId"] in repair_ids]
                                data["requiredHistoryClaims"] = [item for item in data.get("requiredHistoryClaims", []) if item["paragraphId"] in repair_ids]
                                data["requiredAttributionClaims"] = [item for item in data.get("requiredAttributionClaims", []) if item["paragraphId"] in repair_ids]
                                data["onlyParagraphIds"] = repair_ids
                                data["formatProblems"] = str(error)
                                data["checksToRepair"] = [item for item in parse_json_content(reviewed_content)["checks"] if item["paragraphId"] in repair_ids]
                                review_messages = [review_messages[0], {"role": "user", "content": json.dumps(data, ensure_ascii=False)}] + review_messages[2:]
                            else:
                                review_messages.append({"role": "user", "content": "核对记录格式错误：" + str(error) + "。重新返回全部 checks。"})
                            review_messages.append({"role": "user", "content":
                                render_prompt('core.fact_evidence_repair')})
                        else:
                            break
                except ValueError as error:
                    if local_issue is not None:
                        combined = ValueError(str(error) + "；本地校验：" + str(local_issue))
                        combined.corrected_paragraphs = getattr(error, "corrected_paragraphs", None)
                        combined.partial_corrected_paragraphs = getattr(error, "partial_corrected_paragraphs", None)
                        combined.fact_conflicts = getattr(error, "fact_conflicts", None)
                        raise combined from error
                    raise
            if local_issue is not None:
                raise local_issue
            if fact_review_enabled:
                generation_stage = "fact_support_review"
                support_completion = self.gateway.complete_json(self._fact_support_review_messages(
                    context, selected, resolved_state, narrative, reviewed_content, expansion_added_text,
                ))
                raw_responses.append(support_completion.raw_response)
                observations.extend({**item, "generationStage": "fact_support_review"} for item in support_completion.observations)
                self._check_fact_support_review(support_completion.content, reviewed_content, narrative, evidence)
            next_directions = scripted_followup_directions(context, selected, resolved_state)
            result = plan_result(context, selected, narrative, selected["title"], next_directions, "medium")
            result["branchAdditions"] = empty_branch_additions()
            observations.append({"attempt": 1, "outcome": "normalized", "normalization": "generated_scripted_turn_metadata"})
            if stream and not completion.body_was_streamed:
                stream(narrative)
            return result, {"operation": "branch_planner", "model": self.gateway.model, "promptVersion": "python-v0.27+" + catalog_version(), "requestSummary": selected["title"], "rawResponse": "\n\n".join(raw_responses), "callObservations": observations, "promptContext": copy.deepcopy(self.last_prompt_context)}
        except LlmError as error:
            if error.raw_response:
                raw_responses.append(error.raw_response)
            observations.extend({**item, "generationStage": generation_stage} for item in error.observations)
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
            if repair is None and self._repairable_semantic_error(error):
                observations.append({
                    "attempt": 1,
                    "outcome": "retrying",
                    "retryReason": "source_causality_guard",
                    "error": str(error),
                })
                if stream_reset:
                    stream_reset("semantic_repair")
                try:
                    result, retry_audit = self.plan(
                        context, selected, resolved_state, stream, stream_reset, repair=str(error),
                        repair_narrative=narrative,
                        repair_replacements=getattr(error, "corrected_paragraphs", None),
                        repair_conflicts=getattr(error, "fact_conflicts", None),
                        repair_partial_replacements=getattr(error, "partial_corrected_paragraphs", None),
                    )
                except LlmError as retry_error:
                    retry_audit = getattr(retry_error, "audit", None)
                    if isinstance(retry_audit, dict):
                        retry_error.audit = self._merge_semantic_repair_audit(
                            raw_responses, observations, retry_audit,
                        )
                    raise
                if isinstance(retry_audit, dict):
                    return result, self._merge_semantic_repair_audit(raw_responses, observations, retry_audit)
                return result, retry_audit
            observations.append({"attempt": 1, "outcome": "failed", "failureKind": "model_output_rejected", "error": str(error)})
        error = LlmError("LLM Planner 未生成可用剧情：" + observations[-1]["error"], observations[-1]["failureKind"])
        error.audit = {"operation": "branch_planner", "model": self.gateway.model, "promptVersion": "python-v0.27+" + catalog_version(), "requestSummary": selected["title"], "rawResponse": "\n\n".join(raw_responses), "error": observations[-1]["error"], "callObservations": observations, "rejectedNarrativeCharacters": rejected_narrative_characters, "promptContext": copy.deepcopy(self.last_prompt_context)}
        raise error

    def _fact_evidence(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
        scope = self.writing_scope or {}
        evidence = {
            "currentAction": selected["summary"], "actionContract": scope.get("actionContract"),
            "sourceSceneCues": scope.get("narrativeBrief", []),
            "priorSourceSceneCues": scope.get("priorNarrativeBrief", []),
            "knownFacts": scope.get("world", {}).get("immutableFacts", []),
            "characterDetails": scope.get("characterDetails", []),
            "characterLocations": state_character_locations(context["package"], state),
            "registeredItems": [{"id": item["id"], "name": item["name"]} for item in scope.get("items", [])],
            "stateTransition": {key: {"from": context["parent"]["branchState"].get(key), "to": state.get(key)}
                                for key in selected["statePatch"]},
            "afterState": {key: value for key, value in state.items() if key != BRANCH_LEDGER_KEY},
            "confirmedBranchLedger": ledger_context(state),
            "confirmedBranchContext": scope.get("continuityText"),
            "characterIdentityEvidence": scope.get("characterIdentityEvidence", []),
            "sourceDialogueContext": scope.get("sourceDialogueContext", []),
        }
        records = []
        for kind, value in evidence.items():
            for item in value if isinstance(value, list) else [value]:
                if kind in ("sourceSceneCues", "priorSourceSceneCues"):
                    item = item["text"]
                elif kind == "knownFacts":
                    item = {key: item[key] for key in ("text", "communication") if key in item}
                elif kind == "actionContract" and isinstance(item, dict):
                    item = item.get("instruction", item)
                elif kind == "characterLocations":
                    item = {name: location["locationName"] for name, location in item.items()}
                if item:
                    records.append({"id": f"e{len(records) + 1}", "kind": kind, "value": item})
        return records

    @staticmethod
    def _draft_paragraphs(narrative: str) -> List[Dict[str, Any]]:
        return [{"id": index + 1, "text": text} for index, text in enumerate(re.split(r"\n\s*\n", narrative))]

    def _scene_expansion_messages(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any], narrative: str) -> List[Dict[str, str]]:
        needed = max(200, self.minimum_narrative_characters + 150 - narrative_character_count(narrative))
        paragraphs = self._draft_paragraphs(narrative)
        return [
            {"role": "system", "content": (
                render_prompt('core.scene_expansion',
                    needed=f'{needed}',
                )
            )},
            {"role": "user", "content": json.dumps({
                "additionalCharacters": needed,
                "paragraphs": paragraphs, "paragraphIdToExpand": paragraphs[-1]["id"],
                "endingToPreserve": self._expansion_sentences(paragraphs[-1]["text"])[-1],
            }, ensure_ascii=False)},
        ]

    @staticmethod
    def _expansion_sentences(text: str) -> List[str]:
        return [part.strip() for part in re.findall(r'[^。！？!?]+[。！？!?]+[”’」』"]?|[^。！？!?]+$', text) if part.strip()]

    @staticmethod
    def _apply_scene_expansion(narrative: str, content: str, observations: Optional[List[Dict[str, Any]]] = None) -> str:
        expansion = parse_json_content(content).get("expansion")
        if not isinstance(expansion, dict):
            raise LlmError("短稿扩写响应格式无效", "model_output_rejected")
        parts = re.split(r"(\n\s*\n)", narrative)
        paragraph_id, text = expansion.get("paragraphId"), expansion.get("text")
        if (type(paragraph_id) is not int or paragraph_id != (len(parts) + 1) // 2
                or not isinstance(text, str) or not text.strip() or re.search(r"\n\s*\n", text)):
            raise LlmError("短稿扩写只能定位最后一个自然段", "model_output_rejected")
        text = text.strip()
        sentences = LlmPlanner._expansion_sentences(parts[-1])
        if text.endswith(sentences[-1]):
            if len(sentences[-1]) < 12:
                raise LlmError("短稿扩写只需新增内容，不能复制原结尾", "model_output_rejected")
            text = text[:-len(sentences[-1])].strip()
            if not text or all(sentence in LlmPlanner._expansion_sentences(narrative)
                               for sentence in LlmPlanner._expansion_sentences(text)):
                raise LlmError("短稿扩写没有新增内容", "model_output_rejected")
            if observations is not None:
                observations.append({"outcome": "normalized", "normalization": "removed_expansion_ending_echo",
                                     "removedCharacters": narrative_character_count(sentences[-1])})
        for sentence in LlmPlanner._expansion_sentences(narrative):
            if len(sentence) >= 40 and sentence in text:
                raise LlmError("短稿扩写重复了原有长句", "model_output_rejected")
        position = parts[-1].rfind(sentences[-1])
        parts[-1] = parts[-1][:position] + text + parts[-1][position:]
        return "".join(parts)

    def _fact_repair_messages(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any], narrative: str, issues: str, conflicts: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, str]]:
        paragraphs = self._draft_paragraphs(narrative)
        data = {"evidence": self._fact_evidence(context, selected, state), "problems": issues, "paragraphs": paragraphs}
        if conflicts:
            by_id = {item["paragraphId"]: item["claims"] for item in conflicts}
            # Keep rejected quotations in the audit, not in the editing input:
            # they otherwise invite completion of the removed text from memory.
            data["problems"] = [{"paragraphId": item["paragraphId"],
                                 "reason": item.get("reason", "标记处的断言无依据或与已确认事实矛盾，请改为有依据的表述。")}
                                for item in conflicts]
            if "；本地校验：" in issues:
                data["localIssue"] = issues.split("；本地校验：", 1)[1]
            data["paragraphs"] = [{"id": p["id"], "text": self._mask_fact_claims(p["text"], by_id[p["id"]])}
                                  for p in paragraphs if p["id"] in by_id]
            neighbors = {index for key in by_id for index in (key - 1, key + 1)} - set(by_id)
            data["contextParagraphs"] = [p for p in paragraphs if p["id"] in neighbors]
        return [
            {"role": "system", "content": (
                render_prompt('core.fact_repair')
            )},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ]

    def _fact_review_messages(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any], narrative: str, local_issue: Optional[str] = None) -> List[Dict[str, str]]:
        return [
            {"role": "system", "content": self._fact_review_scope() + (
                render_prompt('core.fact_review')
            )},
            {"role": "user", "content": json.dumps({"evidence": [{"id": e["id"], "kind": e["kind"], "spans": self._referenced_fact_spans(e["value"], e["id"])}
                                                                  for e in self._fact_evidence(context, selected, state)], "localIssue": local_issue,
                                                    "requiredRuleClaims": [{"paragraphId": p["id"], "claims": self._rule_claims(p["text"])}
                                                                           for p in self._draft_paragraphs(narrative) if self._rule_claims(p["text"])],
                                                    "requiredHistoryClaims": [{"paragraphId": p["id"], "claims": self._past_claims(p["text"])}
                                                                              for p in self._draft_paragraphs(narrative) if self._past_claims(p["text"])],
                                                    "requiredAttributionClaims": [{"paragraphId": p["id"], "claims": self._attribution_claims(p["text"]),
                                                                                   "neighborParagraphs": [neighbor for neighbor in self._draft_paragraphs(narrative)
                                                                                                          if abs(neighbor["id"] - p["id"]) == 1]}
                                                                                  for p in self._draft_paragraphs(narrative) if self._attribution_claims(p["text"])],
                                                    "paragraphs": [{"id": p["id"], "spans": self._referenced_fact_spans(p["text"], f"p{p['id']}")}
                                                                   for p in self._draft_paragraphs(narrative)]}, ensure_ascii=False)},
        ]

    @staticmethod
    def _normalize_fact_review(content: str) -> str:
        try:
            review = parse_json_content(content)
        except LlmError as error:
            # A malformed review can use the existing single format retry;
            # transport errors never enter this parser or retry path.
            raise LlmError(str(error), "model_output_rejected") from error
        for check in review.get("checks", []) if isinstance(review.get("checks"), list) else []:
            if not isinstance(check, dict):
                continue
            if "evidenceIds" not in check:
                refs = []
                for support in check.get("supports", []) if isinstance(check.get("supports"), list) else []:
                    if isinstance(support, dict) and isinstance(support.get("evidenceRef"), str):
                        ref = support["evidenceRef"].split(":", 1)[0]
                        if ref not in refs:
                            refs.append(ref)
                check["evidenceIds"] = refs
            if "reason" not in check and check.get("status") in ("supported", "non_factual"):
                check["reason"] = "引用依据待校验" if check["status"] == "supported" else "普通描写，无持久事实"
        return json.dumps(review, ensure_ascii=False)

    @staticmethod
    def _fact_review_scope() -> str:
        return (
            render_prompt('core.fact_review_scope')
        )

    @staticmethod
    def _check_fact_support_review(content: str, initial_review: str, narrative: str, evidence: List[Dict[str, Any]]) -> None:
        """Apply an independent critique through the existing repair contract."""
        review = parse_json_content(content)
        checked, conflicts = review.get("checkedParagraphIds"), review.get("conflicts")
        expected = {p["id"] for p in LlmPlanner._draft_paragraphs(narrative)}
        if "checks" in review:
            if "checkedParagraphIds" in review or "conflicts" in review:
                raise LlmError("事实支持复核不能混用两种判定格式", "model_output_rejected")
            records = review["checks"]
            if (not isinstance(records, list) or any(not isinstance(c, dict)
                    or c.get("status") not in ("allowed", "conflict") for c in records)):
                raise LlmError("事实支持复核缺少明确的逐段判定", "model_output_rejected")
            checked = [c.get("paragraphId") for c in records]
            conflicts = [c for c in records if c["status"] == "conflict"]
            if any(c["status"] == "allowed" and (c.get("quotes") or c.get("correctedText")) for c in records):
                raise LlmError("事实支持复核通过条目不能同时包含错误引文或修订", "model_output_rejected")
        if (not isinstance(checked, list) or any(type(i) is not int for i in checked)
                or len(checked) != len(expected) or set(checked) != expected or not isinstance(conflicts, list)):
            raise LlmError("事实支持复核未覆盖全部段落", "model_output_rejected")
        checks = {c["paragraphId"]: c for c in parse_json_content(initial_review)["checks"]}
        seen = set()
        for conflict in conflicts:
            if not isinstance(conflict, dict):
                raise LlmError("事实支持复核问题格式无效", "model_output_rejected")
            paragraph_id = conflict.get("paragraphId")
            if type(paragraph_id) is not int or paragraph_id not in expected or paragraph_id in seen:
                raise LlmError("事实支持复核问题编号无效或重复", "model_output_rejected")
            seen.add(paragraph_id)
            checks[paragraph_id] = {
                "paragraphId": paragraph_id, "status": "conflict", "evidenceIds": [],
                "quotes": conflict.get("quotes"), "reason": conflict.get("reason"),
                "correctedText": conflict.get("correctedText"),
            }
        # Preserve strict quote location, correction limits and unchanged prose.
        LlmPlanner._check_fact_review(json.dumps({"checks": list(checks.values())}, ensure_ascii=False),
                                     narrative, {e["id"] for e in evidence}, evidence)

    def _fact_support_review_messages(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any], narrative: str, initial_review: str, expansion_added_text: Optional[str] = None) -> List[Dict[str, str]]:
        checks = {c["paragraphId"]: c for c in parse_json_content(initial_review)["checks"]}
        return [
            {"role": "system", "content": self._fact_review_scope() + (
                render_prompt('core.fact_support_review')
            )},
            {"role": "user", "content": json.dumps({
                "task": "verify_fact_support",
                "expansionAddedText": expansion_added_text,
                "evidence": [{"id": e["id"], "kind": e["kind"], "spans": self._referenced_fact_spans(e["value"], e["id"])}
                             for e in self._fact_evidence(context, selected, state)],
                "paragraphs": [{"id": p["id"], "spans": self._referenced_fact_spans(p["text"], f"p{p['id']}"),
                                "candidateSupports": checks[p["id"]].get("supports", [])}
                               for p in self._draft_paragraphs(narrative)],
            }, ensure_ascii=False)},
        ]

    @staticmethod
    def _check_fact_repair_size(narrative: str, paragraph_ids: List[int]) -> None:
        paragraphs = re.split(r"\n\s*\n", narrative)
        if any(type(i) is not int or not 1 <= i <= len(paragraphs) for i in paragraph_ids):
            raise LlmError("事实修订段落编号无效", "model_output_rejected")
        changed_characters = sum(len(paragraphs[i - 1]) for i in set(paragraph_ids))
        limit = max(200, len(narrative) // 2)
        if changed_characters > limit:
            raise LlmError(f"事实修订超出局部修改范围（涉及原文 {changed_characters} 字符，上限 {limit}）", "model_output_rejected")

    @staticmethod
    def _apply_fact_replacements(narrative: str, content: str) -> str:
        payload = parse_json_content(content)
        replacements = payload.get("replacements") if isinstance(payload, dict) else None
        if not isinstance(replacements, list) or not replacements:
            raise LlmError("事实修订响应格式无效", "model_output_rejected")
        if len(replacements) > 8:
            raise LlmError(f"事实修订超过 8 处局部修改上限（实际 {len(replacements)} 处）", "model_output_rejected")
        parts = re.split(r"(\n\s*\n)", narrative)
        original_parts = parts[:]
        seen = set()
        for replacement in replacements:
            if not isinstance(replacement, dict):
                raise LlmError("事实修订响应格式无效", "model_output_rejected")
            paragraph_id, text = replacement.get("paragraphId"), replacement.get("text")
            if (type(paragraph_id) is not int or not 1 <= paragraph_id <= (len(parts) + 1) // 2
                    or paragraph_id in seen or not isinstance(text, str) or not text.strip()
                    or re.search(r"\n\s*\n", text)):
                raise LlmError("事实修订段落编号无效、重复或不是单个段落", "model_output_rejected")
            index = (paragraph_id - 1) * 2
            if any(marker in text for marker in REPAIR_PLACEHOLDERS):
                raise LlmError("事实修订仍包含待修复标记", "model_output_rejected")
            text = text.strip()
            # A model may echo a complete context paragraph before its edit.
            # Remove only a provable long prefix; the original neighbor stays.
            for neighbor_index in (index - 2, index + 2):
                if 0 <= neighbor_index < len(original_parts):
                    neighbor = original_parts[neighbor_index].strip()
                    if len(neighbor) >= 60 and text.startswith(neighbor):
                        text = text[len(neighbor):].strip()
            if not text:
                raise LlmError("事实修订只重复了相邻段落", "model_output_rejected")
            if parts[index].strip() == text.strip():
                raise LlmError("事实修订未作修改", "model_output_rejected")
            seen.add(paragraph_id)
            parts[index] = text.strip()
        LlmPlanner._check_fact_repair_size(narrative, list(seen))
        result = "".join(parts)
        if any(marker in result for marker in REPAIR_PLACEHOLDERS):
            raise LlmError("事实修订仍包含待修复标记", "model_output_rejected")
        return result

    @staticmethod
    def _fact_claim_text(text: str) -> str:
        source, _ = LlmPlanner._fact_claim_positions(text)
        return source

    @staticmethod
    def _fact_claim_positions(text: str) -> Tuple[str, List[int]]:
        attributions = re.finditer(r'”[，,]?(?:他|她|[\u4e00-\u9fff]{2,4})(?:低声|轻声|小声|缓缓|低低|轻轻|冷冷)?(?:说|问|答|说道|问道|答道)[，,：:]“', text)
        ignored = {index for match in attributions for index in range(match.start(), match.end())}
        positions = [index for index, char in enumerate(text)
                     if index not in ignored and not re.match(r'[\s“”"‘’\x27，,。！？!?；;：:]', char)]
        return "".join(text[index] for index in positions), positions

    @staticmethod
    def _mask_fact_claims(paragraph: str, claims: List[str]) -> str:
        source, positions = LlmPlanner._fact_claim_positions(paragraph)
        spans = []
        for claim in claims:
            claim = LlmPlanner._fact_claim_text(claim)
            cursor, found = 0, False
            while claim:
                offset = source.find(claim, cursor)
                if offset < 0:
                    break
                spans.append((positions[offset], positions[offset + len(claim) - 1] + 1))
                cursor, found = offset + len(claim), True
            if not found:
                raise LlmError("事实修订无法定位待修复断言", "model_output_rejected")
        merged = []
        # Remove the surrounding faulty sentence from this ephemeral editing
        # input too: leaving a premise such as '信号恢复之前' invites the
        # same unsupported restriction in different words. The stored draft
        # remains untouched until complete replacements pass validation.
        boundaries = [0] + [match.end() for match in re.finditer(r'[。！？!?]+[”’」』"]?', paragraph)]
        if boundaries[-1] != len(paragraph):
            boundaries.append(len(paragraph))
        spans = [(max(boundary for boundary in boundaries if boundary <= start),
                  min(boundary for boundary in boundaries if boundary >= end)) for start, end in spans]
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for number, (start, end) in reversed(list(enumerate(merged, 1))):
            paragraph = paragraph[:start] + f"⟦REPAIR_GAP_{number}⟧" + paragraph[end:]
        return paragraph

    @staticmethod
    def _locate_fact_claims(quotes: List[str], paragraph: str) -> List[str]:
        """Match literal clauses in order; only punctuation and speech tags vary."""
        source = LlmPlanner._fact_claim_text(paragraph)
        claims = []
        for quote in quotes:
            cursor = 0
            for clause in re.split(r"[。！？!?；;\n]+", quote):
                # Legacy reviews sometimes join speech around an attribution.
                clause = re.sub(r'^[“”"\s]*[\u4e00-\u9fff]{1,10}(?:说|问|答)[，,：:\s“”"]+', "", clause)
                claim = LlmPlanner._fact_claim_text(clause)
                if not claim:
                    continue
                offset = source.find(claim, cursor)
                if offset < 0:
                    raise LlmError("事实核对未提供可定位的正文证据", "model_output_rejected")
                cursor = offset + len(claim)
                claims.append(claim)
        if not claims:
            raise LlmError("事实核对未提供可定位的正文证据", "model_output_rejected")
        return list(dict.fromkeys(claims))

    @staticmethod
    def _validate_fact_repair(narrative: str, conflicts: List[Dict[str, Any]]) -> None:
        text = LlmPlanner._fact_claim_text(narrative)
        paragraphs = LlmPlanner._draft_paragraphs(narrative)
        for conflict in conflicts:
            paragraph = LlmPlanner._fact_claim_text(paragraphs[conflict["paragraphId"] - 1]["text"])
            allowed = conflict.get("allowedOccurrences", {})
            retained = [claim for claim in conflict["claims"]
                        if claim in paragraph or text.count(claim) > allowed.get(claim, 0)]
            if retained:
                raise LlmError("事实修订仍保留已拒绝断言：" + "、".join(retained), "model_output_rejected")

    @staticmethod
    def _merge_fact_review_records(original: str, repaired: str, paragraph_ids: List[int]) -> str:
        payload, patch = parse_json_content(original), parse_json_content(repaired)
        checks = patch.get("checks")
        if (not isinstance(checks, list) or len(checks) != len(paragraph_ids)
                or any(not isinstance(item, dict) or type(item.get("paragraphId")) is not int for item in checks)):
            raise LlmError("事实核对格式修正未覆盖指定段落", "model_output_rejected")
        by_id = {item["paragraphId"]: item for item in checks}
        if set(by_id) != set(paragraph_ids) or len(by_id) != len(checks):
            raise LlmError("事实核对格式修正越界或重复", "model_output_rejected")
        for item in payload["checks"]:
            if item["paragraphId"] in by_id and item.get("status") == "conflict":
                if by_id[item["paragraphId"]].get("status") != "conflict":
                    raise LlmError("事实核对格式修正不能撤销已有事实问题", "model_output_rejected")
                by_id[item["paragraphId"]]["reason"] = item["reason"]
        merged = {item["paragraphId"]: item for item in payload["checks"]}
        merged.update(by_id)
        return json.dumps({"checks": [merged[key] for key in sorted(merged)]}, ensure_ascii=False)

    @staticmethod
    def _rule_claims(paragraph: str) -> List[str]:
        claims = []
        for sentence in re.findall(r"[^。！？!?\n]+[。！？!?]?", paragraph):
            if sentence.endswith(("？", "?")) or re.search(r"如果|假如|也许|或许|是否|会不会", sentence):
                continue
            # A condition and its restriction can be in adjacent clauses.
            # Keep them together: the restriction alone loses the new rule.
            condition = re.search(
                r'(?:线路|通信|通讯|信号|系统|设备|终端|电池|排水泵)[^，,；;。！？!?“”"]{0,24}'
                r'(?:之前|之后|以前|以后|前|后)[，,]?[^。！？!?“”"]{0,24}'
                r'(?:不能|不准|不得|禁止|才允许|才能)[^。！？!?“”"]*', sentence,
            )
            if condition:
                claims.append(condition.group().strip(" 。！？!?"))
            for clause in re.split(r"[，,；;“”\"]", sentence):
                if (re.search(r"线路|通信|通讯|信号|系统|设备|终端|电池|排水泵", clause)
                        and re.search(r"只允许|只能|都走|共用|一律|必须通过|规定", clause)):
                    claims.append(clause.strip(" 。！？!?"))
        return claims

    @staticmethod
    def _past_claims(paragraph: str) -> List[str]:
        claims = []
        for sentence in re.findall(r"[^。！？!?\n]+[。！？!?]?", paragraph):
            if re.search(r"如果|假如|也许|或许|是否|会不会", sentence):
                continue
            for match in re.finditer(r"(?:你|他|她)(?:让|叫|要求|逼)我[^，,；;。！？!?“”\"]{1,12}(?:的时候|那天|当时)", sentence):
                claims.append(match.group())
        return claims

    @staticmethod
    def _attribution_claims(paragraph: str) -> List[str]:
        # Only explicit retrospective source tags, not general questions or
        # the act of quoting known information in a new speaker's dialogue.
        return list(dict.fromkeys(re.findall(
            r'[你他她](?:刚才|先前|之前|当时|上次)(?:说的|告诉我的)(?=[。！!，,]*[”"]|[。！!]\s*$)',
            paragraph,
        )))

    @staticmethod
    def _check_source_attributions(check: Dict[str, Any], paragraphs: List[Dict[str, Any]], evidence: List[Dict[str, Any]]) -> None:
        index = check["paragraphId"] - 1
        claims = LlmPlanner._attribution_claims(paragraphs[index]["text"])
        if not claims:
            return
        records = check.get("sourceAttributions")
        if (not isinstance(records, list) or len(records) != len(claims)
                or any(not isinstance(item, dict) or not isinstance(item.get("claim"), str) for item in records)
                or {item["claim"] for item in records} != set(claims)):
            raise LlmError("显式来源声明缺少完整 sourceAttributions，不能仅引用相同话语后通过", "model_output_rejected")
        nearby = "\n".join(p["text"] for p in paragraphs[max(0, index - 1):index + 2])
        sources = {item["id"]: item["value"] for item in evidence}
        for item in records:
            claimed, speaker, refs = item.get("claimedSource"), item.get("sourceSpeaker"), item.get("evidenceIds")
            if (not isinstance(claimed, str) or not claimed.strip() or claimed not in nearby
                    or not isinstance(speaker, str) or not speaker.strip()
                    or not isinstance(refs, list) or not refs
                    or any(not isinstance(ref, str) or ref not in sources for ref in refs)
                    or not any(speaker in json.dumps(sources[ref], ensure_ascii=False) for ref in refs)):
                raise LlmError("来源姓名或其引用无法定位，不能猜测；无法确认的声明应标为 conflict", "model_output_rejected")
            if claimed != speaker:
                raise LlmError(f"信息来源不一致：正文指向 {claimed}，证据指向 {speaker}。应标为 conflict 并只修订来源声明", "model_output_rejected")

    @staticmethod
    def _fact_spans(value: Any) -> List[Dict[str, Any]]:
        """Number literal sentences without merging fields or paraphrasing."""
        spans = []
        def visit(item: Any, path: List[Any]) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    visit(child, path + [key])
            elif isinstance(item, list):
                for index, child in enumerate(item):
                    visit(child, path + [index])
            else:
                text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                for match in re.finditer(r'[^。！？!?\n]+(?:[。！？!?]+[”’」』"]?|(?=\n|$))', text):
                    if match.group().strip():
                        spans.append({"id": len(spans) + 1, "path": path, "text": match.group().strip()})
        visit(value, [])
        return spans

    @staticmethod
    def _referenced_fact_spans(value: Any, namespace: str) -> List[Dict[str, Any]]:
        # Include position and field path so repeated sentences remain distinct.
        # The signature prevents a reviewer from inventing refs by recounting.
        return [{"ref": namespace + ":" + hashlib.sha256(json.dumps(span, ensure_ascii=False).encode()).hexdigest()[:12],
                 "path": span["path"], "text": span["text"]}
                for span in LlmPlanner._fact_spans(value)]

    @staticmethod
    def _fact_quote_is_contained(quote: str, text: str) -> bool:
        # Try literal punctuation normalization before dropping speech tags.
        # A quote may end at "许川说" while that same tag sits between two
        # quoted spans in the full paragraph; both are still literal evidence.
        literal = lambda value: re.sub(r'[\s“”"‘’\x27，,。！？!?；;：:]', '', value)
        return bool(literal(quote)) and (
            literal(quote) in literal(text)
            or LlmPlanner._fact_claim_text(quote) in LlmPlanner._fact_claim_text(text)
        )

    @staticmethod
    def _check_fact_supports(check: Dict[str, Any], paragraph: str, evidence: List[Dict[str, Any]]) -> None:
        """Validate provenance spans, without claiming to prove entailment."""
        supports = check.get("supports")
        if not isinstance(supports, list) or not supports:
            raise LlmError("supported 缺少逐项事实与证据原句 supports", "model_output_rejected")
        values = {item["id"]: item["value"] for item in evidence}
        resolved_supports = []
        def strings(value: Any) -> List[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                return [text for child in value.values() for text in strings(child)]
            if isinstance(value, list):
                return [text for child in value for text in strings(child)]
            return [json.dumps(value, ensure_ascii=False)]
        for support in supports:
            if not isinstance(support, dict):
                raise LlmError("事实支持记录格式无效", "model_output_rejected")
            if "claimRef" in support or "evidenceRef" in support:
                if set(support) != {"claimRef", "evidenceRef"}:
                    raise LlmError("事实支持不能混用 ref 与编号或手写引文", "model_output_rejected")
                claims = {s["ref"]: s["text"] for s in LlmPlanner._referenced_fact_spans(paragraph, f"p{check['paragraphId']}")}
                sources = {s["ref"]: (ref, s["text"]) for ref, value in values.items()
                           for s in LlmPlanner._referenced_fact_spans(value, ref)}
                claim_ref, source_ref = support["claimRef"], support["evidenceRef"]
                if not isinstance(claim_ref, str) or claim_ref not in claims:
                    raise LlmError("正文引用 ref 不存在于当前段落：" + str(claim_ref)
                                   + "。复制本段 spans 中的完整 ref，不得计数或猜测。", "model_output_rejected")
                if not isinstance(source_ref, str) or source_ref not in sources:
                    raise LlmError("证据引用 ref 不存在：" + str(source_ref)
                                   + "。复制 evidence.spans 中的完整 ref。", "model_output_rejected")
                ref, quote = sources[source_ref]
                support = {"claim": claims[claim_ref], "evidenceId": ref, "evidenceQuote": quote}
            if "claimId" in support or "evidenceSpanId" in support:
                ref = support.get("evidenceId")
                if not isinstance(ref, str) or ref not in values or ref not in check["evidenceIds"]:
                    raise LlmError("事实支持编号引用了无效证据 ID", "model_output_rejected")
                claims, sources = LlmPlanner._fact_spans(paragraph), LlmPlanner._fact_spans(values[ref])
                claim_id, source_id = support.get("claimId"), support.get("evidenceSpanId")
                if (type(claim_id) is not int or not 1 <= claim_id <= len(claims)
                        or type(source_id) is not int or not 1 <= source_id <= len(sources)):
                    raise LlmError(
                        f"事实支持原句编号无效或越界：claimId={claim_id!r}，当前段可选 id 为 {[s['id'] for s in claims]}；"
                        f"evidenceId={ref} 的 evidenceSpanId={source_id!r}，该证据可选 id 为 {[s['id'] for s in sources]}。"
                        "只选择输入 spans 中已列出的 id，不得自行重新分句计数；没有支持依据应标为 conflict。",
                        "model_output_rejected",
                    )
                if "claim" in support or "evidenceQuote" in support:
                    raise LlmError("事实支持不能混用编号与手写引文", "model_output_rejected")
                support = {"claim": claims[claim_id - 1]["text"], "evidenceId": ref,
                           "evidenceQuote": sources[source_id - 1]["text"]}
            resolved_supports.append(support)
            claim, ref, quote = support.get("claim"), support.get("evidenceId"), support.get("evidenceQuote")
            normalized_claim = LlmPlanner._fact_claim_text(claim) if isinstance(claim, str) else ""
            if not normalized_claim or not LlmPlanner._fact_quote_is_contained(claim, paragraph):
                raise LlmError("事实支持引文无法定位到当前段落：" + str(claim)
                               + "。逐字摘录当前段落的连续片段；不同片段拆成多个 supports，不得概括或引用邻段。", "model_output_rejected")
            if not isinstance(ref, str) or ref not in check["evidenceIds"] or ref not in values:
                raise LlmError("事实支持引文无法定位到指定证据 ID：" + str(ref), "model_output_rejected")
            normalized_quote = LlmPlanner._fact_claim_text(quote) if isinstance(quote, str) else ""
            if not normalized_quote or not any(LlmPlanner._fact_quote_is_contained(quote, value) for value in strings(values[ref])):
                raise LlmError("事实支持引文无法定位到证据 " + ref + "：" + str(quote)
                               + "。摘录该证据实际原句；没有支持依据时应改为 conflict。", "model_output_rejected")
        for claim in LlmPlanner._rule_claims(paragraph):
            matching = [support for support in resolved_supports
                        if LlmPlanner._fact_claim_text(claim) in LlmPlanner._fact_claim_text(support["claim"])]
            if not matching:
                raise LlmError("事实支持遗漏设备或通信规则：" + claim, "model_output_rejected")
            # This narrow contract deliberately requires a source quotation for
            # operational restrictions. A real but unrelated citation cannot
            # establish a new equipment rule. It is not a general entailment
            # test for the rest of the novel's paraphrased facts.
            if not any(LlmPlanner._fact_claim_text(claim) in LlmPlanner._fact_claim_text(support["evidenceQuote"])
                       for support in matching):
                raise LlmError("设备或通信规则缺少对应原句依据：" + claim
                               + "。相关话题不等于该规则；改为 conflict，并使用素材原句或不新增规则的描写。", "model_output_rejected")
        for claim in LlmPlanner._past_claims(paragraph):
            if not any(LlmPlanner._fact_claim_text(claim) in LlmPlanner._fact_claim_text(support["claim"])
                       and LlmPlanner._fact_claim_text(claim) in LlmPlanner._fact_claim_text(support["evidenceQuote"])
                       for support in resolved_supports):
                raise LlmError("往事断言缺少对应原句依据：" + claim + "。相邻发言不能证明这次过去行为，应标为 conflict。", "model_output_rejected")

    @staticmethod
    def _check_fact_review(content: str, narrative: str, evidence_ids: Set[str], evidence: Optional[List[Dict[str, Any]]] = None) -> None:
        review = parse_json_content(content)
        checks = review.get("checks") if isinstance(review, dict) else None
        paragraphs = LlmPlanner._draft_paragraphs(narrative)
        source_texts = [LlmPlanner._fact_claim_text(str(item["value"].get("text", "") if isinstance(item["value"], dict) else item["value"]))
                        for item in evidence or [] if item["kind"] in ("sourceSceneCues", "priorSourceSceneCues", "knownFacts", "confirmedBranchContext")]
        if not isinstance(checks, list):
            raise LlmError("事实核对未覆盖全部段落", "model_output_rejected")
        seen, issues, corrections, conflicts, format_errors = set(), [], [], [], {}
        for check in checks:
            if not isinstance(check, dict):
                raise LlmError("事实核对响应格式无效", "model_output_rejected")
            paragraph_id, status = check.get("paragraphId"), check.get("status")
            if type(paragraph_id) is not int or not 1 <= paragraph_id <= len(paragraphs) or paragraph_id in seen:
                raise LlmError("事实核对段落编号无效或重复", "model_output_rejected")
            seen.add(paragraph_id)
            refs, reason = check.get("evidenceIds"), check.get("reason")
            if (not isinstance(status, str) or status not in {"supported", "non_factual", "conflict"}
                    or not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in evidence_ids for ref in refs)
                    or not isinstance(reason, str) or not reason.strip()):
                format_errors[paragraph_id] = "事实核对段落或素材引用无效"
                continue
            if status == "supported" and not refs:
                format_errors[paragraph_id] = "事实核对缺少支持素材"
                continue
            if status == "supported" and evidence is not None:
                try:
                    LlmPlanner._check_fact_supports(check, paragraphs[paragraph_id - 1]["text"], evidence)
                    LlmPlanner._check_source_attributions(check, paragraphs, evidence)
                except LlmError as error:
                    format_errors[paragraph_id] = str(error)
                    continue
            if status == "non_factual" and LlmPlanner._rule_claims(paragraphs[paragraph_id - 1]["text"]):
                format_errors[paragraph_id] = "设备或通信规则需要逐项事实核对，不能标为 non_factual"
                continue
            if status == "non_factual" and LlmPlanner._past_claims(paragraphs[paragraph_id - 1]["text"]):
                format_errors[paragraph_id] = "过去行为需要独立依据，不能标为 non_factual"
                continue
            if status == "non_factual" and LlmPlanner._attribution_claims(paragraphs[paragraph_id - 1]["text"]):
                format_errors[paragraph_id] = "显式信息来源声明需要比对说话人，不能标为 non_factual"
                continue
            if status == "conflict":
                quotes = check.get("quotes")
                if quotes is None and isinstance(check.get("quote"), list):
                    quotes = check["quote"]
                if quotes is None and isinstance(check.get("quote"), str) and check["quote"].strip():
                    format_errors[paragraph_id] = "请用 quotes 数组列出最小错误断言，不将已有依据的正确事实纳入必须删除的片段"
                    continue
                if not isinstance(quotes, list) or not quotes or any(not isinstance(q, str) or not q.strip() for q in quotes):
                    format_errors[paragraph_id] = "事实核对未提供可定位的正文证据"
                    continue
                try:
                    claims = LlmPlanner._locate_fact_claims(quotes, paragraphs[paragraph_id - 1]["text"])
                except LlmError as error:
                    format_errors[paragraph_id] = str(error)
                    continue
                corrected = check.get("correctedText")
                if isinstance(corrected, str) and len(claims) > 1:
                    revised_text = LlmPlanner._fact_claim_text(corrected)
                    retained_supported = [claim for claim in claims if claim in revised_text and any(claim in source for source in source_texts)]
                    if retained_supported and any(claim not in revised_text for claim in claims):
                        format_errors[paragraph_id] = ("错误引文夹带素材已有且修订稿仍保留的片段：" + "、".join(retained_supported)
                                                       + "。重新定位最小错误断言，保留原 reason 中的全部问题，不要求删除有依据的相邻事实。")
                        continue
                full_text = LlmPlanner._fact_claim_text(narrative)
                rejected_text = LlmPlanner._fact_claim_text(paragraphs[paragraph_id - 1]["text"])
                conflicts.append({"paragraphId": paragraph_id, "claims": claims, "reason": reason,
                                  "allowedOccurrences": {claim: full_text.count(claim) - rejected_text.count(claim)
                                                         for claim in claims}})
                issues.append(f"段落 {paragraph_id}「{'；'.join(quotes)}」：{reason}")
                corrected = check.get("correctedText")
                if (isinstance(corrected, str) and corrected.strip()
                        and corrected.strip() != paragraphs[paragraph_id - 1]["text"].strip()
                        and not re.search(r"\n\s*\n", corrected)):
                    corrections.append({"paragraphId": paragraph_id, "text": corrected})
        for missing_id in sorted(set(range(1, len(paragraphs) + 1)) - seen):
            format_errors[missing_id] = "缺少该段核对记录"
        if format_errors:
            error = LlmError("；".join(f"段落 {key}：{value}" for key, value in format_errors.items()), "model_output_rejected")
            error.invalid_paragraph_ids = list(format_errors)
            raise error
        if issues:
            error = ValueError("剧情正文事实核对未通过：" + "；".join(issues))
            error.fact_conflicts = conflicts
            usable = []
            for correction in corrections:
                try:
                    fixed = LlmPlanner._apply_fact_replacements(narrative, json.dumps({"replacements": [correction]}))
                    LlmPlanner._validate_fact_repair(fixed, [item for item in conflicts if item["paragraphId"] == correction["paragraphId"]])
                except LlmError:
                    continue
                usable.append(correction)
            if usable:
                try:
                    LlmPlanner._apply_fact_replacements(narrative, json.dumps({"replacements": usable}))
                except LlmError:
                    usable = []
            error.partial_corrected_paragraphs = usable
            if len(corrections) == len(issues):
                try:
                    fixed = LlmPlanner._apply_fact_replacements(narrative, json.dumps({"replacements": corrections}))
                    LlmPlanner._validate_fact_repair(fixed, conflicts)
                except LlmError as correction_error:
                    error = ValueError(str(error) + "；修订建议无效：" + str(correction_error))
                    error.fact_conflicts = conflicts
                    error.partial_corrected_paragraphs = usable
                else:
                    error.corrected_paragraphs = corrections
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
        scope = self.context_resolver.resolve(context, selected, state) if self.context_resolver else context["package"]
        scope_text = json.dumps({
            "immutableFacts": scope["world"].get("immutableFacts", []),
            "sourceSceneCues": scope.get("narrativeBrief", []),
            "actionContract": scope.get("actionContract", {}),
            "characters": [item["name"] for item in scope.get("characters", [])],
            "locations": [item["name"] for item in scope.get("locations", [])],
            "items": [item["name"] for item in scope.get("items", [])],
        }, ensure_ascii=False)
        state_guardrail_text = "\n".join([
            narrative_state_guardrails(context["package"], state),
            "母本事实已保留为未确定状态的人物（优先级高于戏剧性补白）：",
            source_fact_protection_context(context["package"], state),
        ])
        return render_prompt('core.continuation',
            title=f"{selected['title']}",
            summary=f"{selected['summary']}",
            state_json=f'{json.dumps({key: value for (key, value) in state.items() if key != BRANCH_LEDGER_KEY}, ensure_ascii=False)}',
            ledger_context=f'{ledger_context(state)}',
            current_characters=f'{current_characters}',
            max=f'{max(700, target_characters - current_characters)}',
            max_2=f'{max(1100, target_characters - current_characters + 300)}',
            minimum_narrative_characters=f'{self.minimum_narrative_characters}',
            scope_text=f'{scope_text}',
            location_name=f"{location_name(context['package'], state)}",
            state_character_location_context=f"{state_character_location_context(context['package'], state)}",
            state_guardrail_text=f'{state_guardrail_text}',
            narrative=f'{narrative}',
            state_guardrail_text_2=f'{state_guardrail_text}',
        )

    def _prompt(
        self,
        context: Dict[str, Any],
        selected: Dict[str, Any],
        state: Dict[str, Any],
        repair: Optional[str],
        terminal_source_branch: bool = False,
    ) -> str:
        module_context = self.context_resolver.resolve(context, selected, state) if self.context_resolver else None
        self.writing_scope = module_context
        self.last_prompt_context = (
            {
                "mode": "modules",
                "currentChapterId": module_context["currentChapter"]["id"],
                "currentBeatId": module_context["currentBeat"]["id"],
                "modulePaths": module_context["modulePaths"],
            }
            if module_context else {"mode": "package"}
        )
        self.last_prompt_context["branchLedgerEntries"] = len(state.get(BRANCH_LEDGER_KEY, {}).get("entries", []))
        prompt_world = module_context["world"] if module_context else context["package"]["world"]
        fact_text = "\n".join(
            "- " + (
                fact["communication"]["speakerName"] + "的已登记" + fact["communication"]["kind"] + "原句："
                if isinstance(fact.get("communication"), dict) else ""
            ) + fact["text"] for fact in prompt_world["immutableFacts"]
        )
        constraint_text = "\n".join("- " + item for item in prompt_world.get("globalConstraints", [])) or "- 未声明额外全局约束"
        guideline_text = "\n".join(
            "- " + item for item in prompt_world.get("narrativeGuidelines", {}).get("prohibitions", [])
        ) or "- 未声明额外叙事禁则"
        guidelines = prompt_world.get("narrativeGuidelines", {})
        registered_locations = (module_context["locations"] if module_context else context["package"].get("locations", [])) + state.get("derivedLocations", [])
        location_text = "\n".join(
            "- " + location["name"] + "：" + location.get("description", location.get("summary", ""))
            for location in registered_locations
        ) or "- 暂无已登记地点"
        current_location_name = location_name(context["package"], state)
        source_scene_text = "\n".join(
            "- " + cue["text"] for cue in (module_context or {}).get("narrativeBrief", [])
        ) or "- 未提供额外场景材料"
        if module_context and module_context.get("priorNarrativeBrief"):
            source_scene_text += ("\n\n以下仅为已经发生的前史，只能作为记忆或因果背景，禁止重新发现物品、重新相识或重演这些事件：\n"
                                  + "\n".join("- " + cue["text"] for cue in module_context["priorNarrativeBrief"]))
        if module_context and module_context.get("sourceDialogueContext"):
            source_scene_text += ("\n\n对白指代的相邻原文（按来源编号连续排列，phase=prior 只可承接，不能重演；"
                                  "不能把同一句话移交给另一个信息来源，无法确定时不写‘你刚才说的’）：\n"
                                  + json.dumps(module_context["sourceDialogueContext"], ensure_ascii=False))
        persona = context.get("contract", {}).get("persona", {})
        focal_id = persona.get("sourceCharacterId") or guidelines.get("focalCharacterId")
        focal_name = persona.get("name") or next(
            (item["name"] for item in (module_context["characters"] if module_context else context["package"].get("characters", [])) if item["id"] == focal_id), "主角",
        )
        perspective = guidelines.get("perspective", "third_person_limited")
        perspective_instruction = (
            f"以第三人称限知、连贯的场景推进来写，焦点人物一律称“{focal_name}”或“他/她”，绝不可使用“你”指代焦点人物；"
            if perspective == "third_person_limited"
            else f"严格遵循 StoryPackage 声明的 {perspective} 视角与焦点人物“{focal_name}”；"
        )
        profile_text = persona_profile_text(persona)
        character_details = module_context["characterDetails"] if module_context else context["characterDetails"]
        character_text = "\n".join("- " + item["name"] + "：" + item["detail"] for item in character_details) or "- 暂无额外已确认外观细节"
        registered_character_names = [
            character["name"] for character in context["package"].get("characters", [])
            if isinstance(character, dict) and isinstance(character.get("name"), str)
        ]
        registered_character_text = "、".join(registered_character_names) or "无"
        continuity_text = module_context["continuityText"] if module_context else source_continuity_context(
            context["package"], context.get("lineage", [context["parent"]]), state,
            context.get("contract", {}).get("canonicalTimelineRefs"),
        )
        branch_ledger_text = ledger_context(state)
        module_scope_text = (
            "模块化故事上下文范围：当前章节《" + module_context["currentChapter"]["title"] + "》的剧情节点“"
            + module_context["currentBeat"]["summary"] + "”；"
            + ("仅承接前序剧情节点：" + "；".join(module_context["previousBeatSummaries"]) + "。" if module_context["previousBeatSummaries"] else "没有注入未来节点。")
            if module_context else "故事上下文来源：兼容单文件 StoryPackage。"
        )
        state_guardrail_text = narrative_state_guardrails(context["package"], state)
        character_location_text = state_character_location_context(context["package"], state)
        protected_character_text = source_fact_protection_context(context["package"], state)
        context_items = module_context["items"] if module_context else context["package"].get("items", [])
        available_item_names = [
            item["name"] + "（已确认在焦点人物持有范围内）"
            for item in context_items + state.get("derivedItems", [])
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item.get("id") in set(state.get("inventory", []))
        ]
        owner_names = {
            character["id"]: character["name"] for character in context["package"].get("characters", [])
            if isinstance(character, dict) and isinstance(character.get("id"), str) and isinstance(character.get("name"), str)
        }
        item_owner_ids = state.get(ITEM_OWNER_MAP_FIELD, {})
        if not isinstance(item_owner_ids, dict):
            item_owner_ids = {}
        available_item_names.extend(
            item["name"] + "（已确认由" + owner_names[item_owner_ids[item["id"]]] + "持有）"
            for item in context_items
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("id"), str)
            and item_owner_ids.get(item["id"]) in owner_names
        )
        available_item_text = "、".join(available_item_names) or "- 当前没有已确认可取得或可使用的道具"
        unavailable_item_names = [
            item["name"] for item in context_items
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item.get("id") not in set(state.get("inventory", []))
            and item_owner_ids.get(item.get("id")) not in owner_names
        ]
        unavailable_item_text = "、".join(unavailable_item_names) or "无"
        selected_patch = {
            key: value for key, value in selected["statePatch"].items()
            if key not in ("derivedAdditions", "freeTextProgress")
        }
        state_transition = {
            key: {"from": context["parent"]["branchState"].get(key), "to": state.get(key)}
            for key in selected_patch
        }
        parent_arc = context["parent"].get("storyArc") or {}
        arc_instruction = ""
        if parent_arc.get("arcId"):
            arc_instruction = (
                f"当前大方向：{parent_arc['activeGoal']}。当前小方向：{selected['title']}。"
                "本回合只完成这个小方向对应的一章；运行时会依据 StoryPackage 的完成条件决定"
                "接下来继续给出小方向，还是切换到新的大方向；章节标题和方向均由运行时生成。"
            )
        terminal_instruction = (
            render_prompt('core.terminal')
            if terminal_source_branch
            else "本回合尚未到达声明的源分支终点；在当前行动完成处收笔，下一回合方向由运行时提供，正文不列菜单。"
        )
        if module_context:
            return render_prompt('core.module_narrative',
                module_scope_text=f'{module_scope_text}',
                title=f"{selected['title']}",
                summary=f"{selected['summary']}",
                get=f"{module_context.get('actionContract', {}).get('instruction', '')}",
                location_name=f"{location_name(context['package'], context['parent']['branchState'])}",
                current_location_name=f'{current_location_name}',
                perspective_instruction=f'{perspective_instruction}',
                profile_text=f'{profile_text}',
                character_text=f'{character_text}',
                state_json=f"{json.dumps((module_context or {}).get('characterIdentityEvidence', []), ensure_ascii=False)}",
                character_location_text=f'{character_location_text}',
                fact_text=f'{fact_text}',
                state_transition_json=f'{json.dumps(state_transition, ensure_ascii=False)}',
                branch_ledger_text=f'{branch_ledger_text}',
                continuity_text=f'{continuity_text}',
                source_scene_text=f'{source_scene_text}',
                location_text=f'{location_text}',
                available_item_text=f'{available_item_text}',
                state_guardrail_text=f'{state_guardrail_text}',
                protected_character_text=f'{protected_character_text}',
                length_instruction=f"{('篇幅：简短写清本次行动和眼前反应即可，约三至五段，不设最低字数，不为凑字数扩写。' if self.concise else '篇幅：约 16 个自然段，总计 2300 至 2600 个非空白字符。全部发生在本回合场景中，不靠另开场景补字数。')}",
                terminal_instruction=f'{terminal_instruction}',
                repair_instruction=f"{('必须改正上条草稿：' + repair if repair else '')}",
            )
        return render_prompt('core.narrative',
            title=f"{selected['title']}",
            summary=f"{selected['summary']}",
            state_transition_json=f'{json.dumps(state_transition, ensure_ascii=False)}',
            state_json=f'{json.dumps({key: value for (key, value) in state.items() if key != BRANCH_LEDGER_KEY}, ensure_ascii=False)}',
            arc_instruction=f'{arc_instruction}',
            fact_text=f'{fact_text}',
            source_scene_text=f'{source_scene_text}',
            constraint_text=f'{constraint_text}',
            guideline_text=f'{guideline_text}',
            profile_text=f'{profile_text}',
            character_text=f'{character_text}',
            registered_character_text=f'{registered_character_text}',
            character_location_text=f'{character_location_text}',
            protected_character_text=f'{protected_character_text}',
            location_text=f'{location_text}',
            current_location_name=f'{current_location_name}',
            available_item_text=f'{available_item_text}',
            unavailable_item_text=f'{unavailable_item_text}',
            branch_ledger_text=f'{branch_ledger_text}',
            module_scope_text=f'{module_scope_text}',
            continuity_text=f'{continuity_text}',
            perspective_instruction=f'{perspective_instruction}',
            length_instruction=f"{('简短写清本次行动即可，约三至五段，不设最低字数，不为凑字数扩写。' if self.concise else '请规划 2,200 至 2,800 个中文字符，以留出高于 2,000 字硬下限的余量；按非空白字符自行计数，少于 2,200 时必须继续补充新的场景、动作、对话或后果，不能重复句子或概述。')}",
            state_guardrail_text=f'{state_guardrail_text}',
            terminal_instruction=f'{terminal_instruction}',
            repair_instruction=f"{('上次草稿的问题：' + repair + '。请只修复这些问题并保留合理剧情。' if repair else '')}",
        )


class NarrativeReviewer:
    """Advisory review inspired by multi-stage novel pipelines, never state authority."""
    def review(self, context: Dict[str, Any], result: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        return {"decision": "accept", "issues": []}


class LlmNarrativeReviewer(NarrativeReviewer):
    def __init__(self, gateway: OpenAICompatibleGateway) -> None:
        self.gateway = gateway

    def review(self, context: Dict[str, Any], result: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        prompt = render_prompt('core.quality_review',
            state_json=f'{json.dumps({key: value for (key, value) in state.items() if key != BRANCH_LEDGER_KEY}, ensure_ascii=False)}',
            ledger_context=f'{ledger_context(state)}',
            narrative_text=f"{result['narrativeText']}",
        )
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

    def _custom_direction(self, parent: Dict[str, Any], player_direction: str) -> Optional[Dict[str, Any]]:
        if self.package is None:
            return None
        state = parent.get("branchState", {})
        progress = state.get("freeTextProgress")
        if not isinstance(progress, int):
            return None
        patch: Dict[str, Any] = {"freeTextProgress": progress + 1}
        chapter_title: Optional[str] = None
        target_location_id: Optional[str] = None
        locations = sorted(self.package.get("locations", []), key=lambda item: len(str(item.get("name", ""))), reverse=True)
        for location in locations:
            name = location.get("name")
            location_id = location.get("id")
            if not isinstance(name, str) or not isinstance(location_id, str):
                continue
            movement = r"(?:进入|前往|赶到|抵达|走进|去|到)" + re.escape(name)
            match = re.search(movement, player_direction)
            if match:
                field = focal_character_location_field(self.package, state)
                if field and state.get(field) != location_id:
                    patch[field] = location_id
                target_location_id = location_id
                verb = match.group(0)[:-len(name)]
                chapter_title = ("前往" if verb in ("去", "到") else verb) + name
                break
        if target_location_id is not None and CHARACTER_LOCATION_MAP_FIELD in state:
            participants = {
                character["id"]
                for character in self.package.get("characters", [])
                if isinstance(character.get("id"), str)
                and isinstance(character.get("name"), str)
                and character["name"] in player_direction
            }
            if any(term in player_direction for term in ("带路", "同行", "跟随", "一起", "带着", "陪同")):
                participants.update(state.get(CHARACTER_LOCATION_MAP_FIELD, {}))
            if participants:
                patch[CHARACTER_LOCATION_MAP_FIELD] = {
                    character_id: target_location_id for character_id in sorted(participants)
                }
        digest = hashlib.sha1((parent.get("id", "") + "\n" + player_direction.strip()).encode("utf-8")).hexdigest()[:12]
        return {
            "id": "direction_free_" + digest,
            "title": chapter_title or "自定行动：" + player_direction.strip()[:12],
            "summary": player_direction.strip(),
            "statePatch": patch,
            "isFreeText": True,
        }

    def evaluate(self, parent: Dict[str, Any], player_direction: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        normalized = normalize(player_direction)
        if not normalized:
            return {"kind": "clarification_needed", "message": "请用一句话说明希望优先推动哪条剧情方向。"}, None
        forbidden_fact = self._forbidden_world_fact(player_direction)
        if forbidden_fact is not None:
            return {"kind": "rejected", "message": "当前故事不允许以超自然能力直接解决障碍。请在既有世界规则内说明行动。", "citations": [{"kind": "immutable_fact", "ref": forbidden_fact["id"]}]}, None
        # Script-generated packages deliberately keep the model away from the
        # source text. A player's free input therefore defines a new turn;
        # matching a shared place name in a source-derived menu must not turn
        # it into an unchosen canonical continuation.
        if script_generated_package(self.package or {}):
            direction = self._custom_direction(parent, player_direction)
            if direction is not None:
                return {"kind": "accepted", "directionId": direction["id"], "direction": direction, "rationale": "已登记为本回合自定义行动；状态变化由本地脚本控制。"}, None
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
        direction = self._custom_direction(parent, player_direction)
        if direction is not None:
            return {"kind": "accepted", "directionId": direction["id"], "direction": direction, "rationale": "已登记为本回合自定义行动；状态变化由本地脚本控制。"}, None
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
        prompt = render_prompt('legacy.direction_evaluation',
            directions_json=f'{json.dumps(directions, ensure_ascii=False)}',
            player_direction=f'{player_direction}',
        )
        try:
            completion = self.gateway.complete_json([{"role": "system", "content": "你是互动小说方向判定器。只输出 JSON。"}, {"role": "user", "content": prompt}])
        except LlmError as error:
            error.audit = {"operation": "direction_evaluator", "model": self.gateway.model, "promptVersion": "python-v0.1+" + catalog_version(), "requestSummary": player_direction[:300], "rawResponse": None, "error": str(error), "callObservations": error.observations}
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
        audit = {"operation": "direction_evaluator", "model": self.gateway.model, "promptVersion": "python-v0.1+" + catalog_version(), "requestSummary": player_direction[:300], "rawResponse": completion.raw_response, "callObservations": observations}
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

    def start(self, session_id: str, selection: Optional[Dict[str, str]] = None, allow_any_source_character: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        self.store.assert_session_package(session_id, self.package)
        contract = create_contract(self.package, session_id, selection, allow_any_source_character)
        root = entry_node(self.package, contract, allow_any_source_character)
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

    def continue_direction(self, session_id: str, parent_id: str, direction_id: str, player_direction: Optional[str] = None, stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None, on_generation_start: Optional[Callable[[], None]] = None, request_id: Optional[str] = None, selected_direction: Optional[Dict[str, Any]] = None, draft_editor: Optional[Callable[[Dict[str, Any]], str]] = None) -> Dict[str, Any]:
        self.store.assert_session_package(session_id, self.package)
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
        selected = selected_direction or next((item for item in parent["nextDirections"] if item["id"] == direction_id), None)
        if selected is None and callable(getattr(self.planner, 'published_directions', None)):
            selected = next((item for item in self.planner.published_directions(self.package, parent['branchState'])
                             if item['id'] == direction_id), None)
        if selected_direction is not None and selected_direction.get("id") != direction_id:
            raise ValueError("自定义方向 ID 与请求不一致")
        if selected is None:
            raise ValueError("当前分支不存在可选方向: " + direction_id)
        if selected.get("directionLevel") == "arc":
            if not getattr(self.planner, 'interactive_reader', False):
                return self._select_arc(session_id, parent_id, parent, selected, player_direction, request_id, contract)
            phases = phase_directions_for_arc(self.package, parent.get('sourceNodeRef') or contract['entryNodeId'], parent['branchState'], selected['arcId'])
            if not phases:
                raise ValueError('当前方向没有可执行的行动')
            selected = {**phases[0], 'id': direction_id}

        lineage = self.store.lineage(session_id, parent_id)
        if callable(getattr(self.planner, 'prepare_direction', None)):
            selected = self.planner.prepare_direction(self.package, parent, selected, player_direction)
        provisional = copy.deepcopy(parent["branchState"])
        # Route resolution is based on a provisional patch. The actual patch is
        # then checked against source-state invariants.
        provisional.update({key: value for key, value in selected["statePatch"].items() if key != "derivedAdditions"})
        source_node_ref = resolve_source_node(self.package, parent, provisional, selected)
        direction_source = {
            "kind": "player_direction" if player_direction else "direction",
            "ref": selected["id"], "nodeRef": source_node_ref,
        }
        resolved = apply_branch_patch(
            self.package, parent["branchState"], selected["statePatch"], source_node_ref, direction_source,
        )
        rejoin = resolve_rejoin(self.package, parent.get("sourceNodeRef") or contract["entryNodeId"], parent["openThreads"], parent["branchState"], source_node_ref, resolved, selected) if resolved["storyScope"] == "source" else None
        if rejoin and not any(item["canonicalRelation"] == "diverged" for item in lineage):
            raise ValueError("只有已偏离原著的分支可以汇合: " + rejoin["id"])
        canonical = None
        if (
            not getattr(self.planner, 'interactive_reader', False)
            and not script_generated_package(self.package)
            and player_direction
            and player_direction.startswith("选择方向：")
            and all(item["canonicalRelation"] == "on_line" for item in lineage)
            and selected.get("canonicalBeatId")
        ):
            beat = find_beat(self.package, selected["canonicalBeatId"])
            if beat:
                if beat["nodeId"] != source_node_ref:
                    raise ValueError("规范方向场景路线与目标锚点不一致: " + selected["id"])
                expected_state = initial_branch_state(beat)
                expected_state = {key: value for key, value in expected_state.items() if key not in BRANCH_PRIVATE_STATE_KEYS}
                actual_state = {key: value for key, value in resolved.items() if key not in BRANCH_PRIVATE_STATE_KEYS}
                if expected_state != actual_state:
                    raise ValueError("规范方向状态快照与补丁不一致: " + selected["id"])
                canonical = {"narrativeText": beat.get("sourceExcerpt", {}).get("text", beat["narrativeAnchor"]), "summary": beat["summary"], "factDeltas": [{"id": "fact_" + beat["id"], "source": "source", "summary": beat["summary"]}], "openThreads": beat["openThreads"], "nextDirections": [branch_direction(item) for item in beat["nextDirections"]], "storyArc": parent.get("storyArc"), "planning": {"citations": [{"kind": "canonical_node", "ref": beat["nodeId"], "rationale": "规范分支复用原著。"}], "confidence": "high", "stateChangeProposals": []}}
        context = {"package": self.package, "contract": contract, "parent": parent, "lineage": lineage, "playerDirection": player_direction, "characterDetails": confirmed_character_details(self.package, lineage)}
        closing_intent = getattr(self.store, 'closing_intent', None)
        if callable(closing_intent):
            context['closingIntent'] = closing_intent(session_id, parent_id)
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
                    {"kind": "planner", "ref": selected["id"], "nodeRef": source_node_ref},
                )
        if draft_editor is not None:
            original_narrative = result["narrativeText"]
            edited_narrative = draft_editor(copy.deepcopy(result))
            if not isinstance(edited_narrative, str) or not edited_narrative.strip():
                raise ValueError("草稿未确认，未写入分支")
            edited_narrative = edited_narrative.strip()
            # Reused canonical prose is fixed source material rather than a
            # planner output, so it does not inherit the generated-chapter floor.
            minimum = 0 if canonical is not None else getattr(self.planner, "minimum_narrative_characters", 0)
            if isinstance(minimum, int) and minimum > 0 and narrative_character_count(edited_narrative) < minimum:
                raise ValueError(f"确认后的正文少于 {minimum} 个非空白字符")
            result["narrativeText"] = edited_narrative
            result["draftConfirmation"] = {
                "status": "confirmed", "edited": edited_narrative != original_narrative,
                "originalCharacterCount": narrative_character_count(original_narrative),
                "confirmedCharacterCount": narrative_character_count(edited_narrative), "confirmedAt": timestamp(),
            }
        # Allocate the cause ID before validation; it becomes visible only on commit.
        from .reader_consequences import commit_consequences, filter_directions, goal_directions
        branch_id = "branch_" + str(uuid.uuid4())
        resolved = commit_consequences(context, resolved, result, branch_id)
        if callable(getattr(self.store, "on_validating", None)):
            self.store.on_validating()
        if canonical is None or draft_editor is not None:
            guard_narrative(
                result["narrativeText"], resolved, context["characterDetails"],
                self.package["world"].get("narrativeGuidelines"), self.package,
            )
            resolver = getattr(self.planner, "context_resolver", None)
            if resolver is not None:
                scope = resolver.resolve(context, selected, resolved)
                if callable(getattr(self.planner, 'validation_scope', None)):
                    scope = self.planner.validation_scope(context, scope)
                guard_offstage_reports(result["narrativeText"], scope)
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
        updated_goals = goal_directions(resolved)
        if updated_goals is not None:
            result["nextDirections"] = updated_goals
        result["nextDirections"] = filter_directions(result["nextDirections"], self.package, resolved)
        assert_published_directions(self.package, source_node_ref, resolved, result["nextDirections"])
        node = {
            **result, "id": branch_id, "sourceNodeRef": source_node_ref, "branchState": resolved,
            "canonicalRelation": relation, "selectedDirectionId": direction_id,
            "selectedDirection": branch_direction(selected), "playerDirection": player_direction,
            "requestId": request_id, "createdAt": timestamp(),
        }
        stored = self.store.append_branch(session_id, parent_id, node)
        if resolved["storyScope"] == "derived":
            derived = self.store.derived(session_id)
            if derived:
                derived = copy.deepcopy(derived)
                previous_entries = parent["branchState"].get(BRANCH_LEDGER_KEY, {}).get("entries", [])
                current_entries = stored["branchState"][BRANCH_LEDGER_KEY]["entries"]
                derived["branchLedger"] = copy.deepcopy(stored["branchState"][BRANCH_LEDGER_KEY])
                derived["revisions"].append({
                    "branchId": stored["id"],
                    "ledgerEntryIds": [entry["id"] for entry in current_entries[len(previous_entries):]],
                    "addedAt": timestamp(),
                })
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
        draft_editor: Optional[Callable[[Dict[str, Any]], str]] = None,
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
                    # A failed/disconnected generation can leave an accepted
                    # direction without prose. Resume the same decision rather
                    # than inserting a second evaluation or consuming a turn.
                    node = self.continue_direction(
                        session_id, parent_id, existing["directionId"], player_direction,
                        stream, stream_reset, on_generation_start, request_id,
                        existing.get("direction"), draft_editor,
                    )
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
            on_generation_start, request_id, evaluation.get("direction"), draft_editor,
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
        append_branch_ledger(state, [{
            "kind": "event", "operation": "added", "entityId": "event_derivative_entry",
            "summary": "玩家已创建衍生故事并声明后续目标。", "before": None,
            "after": {"goal": goal.strip(), "storyScope": state["storyScope"]},
        }], {"kind": "player", "ref": "direction_begin_derivative", "nodeRef": parent.get("sourceNodeRef") or "source"})
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
        package = {"id": "derived_" + str(uuid.uuid4()), "sessionId": session_id, "sourcePackageRef": {"id": self.package["id"], "version": self.package["version"]}, "forkBranchId": parent_id, "title": self.package["metadata"]["title"] + "·衍生篇", "goal": goal.strip(), "createdAt": timestamp(), "branchLedger": copy.deepcopy(state[BRANCH_LEDGER_KEY]), "revisions": []}
        self.store.save_derived(package)
        return package, self.store.create_derived_entry(session_id, parent_id, entry)
