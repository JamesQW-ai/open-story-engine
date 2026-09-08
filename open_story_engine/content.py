"""StoryPackage loading and conservative structural validation."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Union


class StoryPackageError(ValueError):
    pass


def load_story_package(path: Union[str, Path]) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as source:
        package = json.load(source)
    validate_story_package(package)
    return package


def validate_story_package(package: Dict[str, Any]) -> None:
    """Validate cross references used by the Python runtime.

    The source schema remains the StoryPackage JSON contract. This intentionally
    avoids accepting malformed content merely because Python is dynamically typed.
    """
    required = ["id", "version", "metadata", "world", "characters", "locations", "items", "story", "rules", "initialState"]
    missing = [key for key in required if key not in package]
    if missing:
        raise StoryPackageError("故事包缺少字段: " + "、".join(missing))
    if package.get("schemaVersion") != "1.0":
        raise StoryPackageError("仅支持 StoryPackage schemaVersion 1.0")
    story = package["story"]
    graph = story.get("narrativeGraph", {})
    groups = {
        "角色": package["characters"],
        "地点": package["locations"],
        "物品": package["items"],
        "场景": story.get("nodes", []),
        "叙事锚点": graph.get("beats", []),
    }
    ids: Dict[str, Set[str]] = {}
    for label, entries in groups.items():
        values = [entry.get("id") for entry in entries]
        if not values or any(not isinstance(value, str) or not value for value in values):
            raise StoryPackageError(f"{label}必须包含非空 ID")
        if len(values) != len(set(values)):
            raise StoryPackageError(f"{label} ID 重复")
        ids[label] = set(values)
    if story.get("startNodeId") not in ids["场景"]:
        raise StoryPackageError("起始场景不存在")
    if graph.get("startBeatId") not in ids["叙事锚点"]:
        raise StoryPackageError("叙事图起始锚点不存在")
    initial = package["initialState"]
    if initial.get("currentNodeId") not in ids["场景"]:
        raise StoryPackageError("初始状态场景不存在")
    if initial.get("currentLocationId") not in ids["地点"]:
        raise StoryPackageError("初始状态地点不存在")
    for location in package["locations"]:
        for exit_id in location.get("exits", []):
            if exit_id not in ids["地点"]:
                raise StoryPackageError(f"地点出口不存在: {exit_id}")
    bindings = package.get("world", {}).get("narrativeGuidelines", {}).get("characterLocationStateFields")
    if bindings is not None:
        if not isinstance(bindings, dict):
            raise StoryPackageError("characterLocationStateFields 必须是对象")
        for character_id, state_field in bindings.items():
            if character_id not in ids["角色"] or not isinstance(state_field, str) or not state_field.strip():
                raise StoryPackageError("characterLocationStateFields 必须引用已登记角色和非空状态字段")
    for beat in graph.get("beats", []):
        if beat.get("nodeId") not in ids["场景"]:
            raise StoryPackageError(f"叙事锚点引用的场景不存在: {beat.get('id')}")
        direction_ids = [direction.get("id") for direction in beat.get("nextDirections", [])]
        if len(direction_ids) != len(set(direction_ids)):
            raise StoryPackageError(f"叙事锚点存在重复方向: {beat.get('id')}")
        beat_ids = {item.get("id") for item in graph.get("beats", [])}
        for direction in beat.get("nextDirections", []):
            followup = direction.get("followupBeatId")
            if followup is not None and (not isinstance(followup, str) or followup not in beat_ids):
                raise StoryPackageError(f"方向引用的后续叙事锚点不存在: {direction.get('id')}")
    beats = graph.get("beats", [])
    validate_state_model(package, beats)
    validate_arc_model(package, beats)


def validate_arc_model(package: Dict[str, Any], beats: List[Dict[str, Any]]) -> None:
    """Validate optional macro-plot declarations without coupling to a demo story."""
    model = package.get("story", {}).get("arcModel")
    if model is None:
        return
    if not isinstance(model, dict):
        raise StoryPackageError("story.arcModel 必须是对象")
    arcs = model.get("arcs")
    entry_arc_ids = model.get("entryArcIds")
    if not isinstance(arcs, list) or not arcs or not isinstance(entry_arc_ids, list) or not entry_arc_ids:
        raise StoryPackageError("story.arcModel 必须声明 arcs 和 entryArcIds")
    arc_ids = [arc.get("id") for arc in arcs if isinstance(arc, dict)]
    if len(arc_ids) != len(arcs) or any(not isinstance(arc_id, str) or not arc_id for arc_id in arc_ids) or len(arc_ids) != len(set(arc_ids)):
        raise StoryPackageError("story.arcModel.arcs 必须包含唯一非空 id")
    state_fields = set().union(*(set(beat.get("branchState", {})) for beat in beats))
    state_fields.update(package.get("stateModel", {}).get("runtimeStateFields", []))
    direction_ids = [direction.get("id") for beat in beats for direction in beat.get("nextDirections", [])]
    if any(not isinstance(direction_id, str) or not direction_id for direction_id in direction_ids) or len(direction_ids) != len(set(direction_ids)):
        raise StoryPackageError("story.arcModel 需要全局唯一的剧情方向 id")
    owned_phase_ids: Set[str] = set()
    for arc in arcs:
        if not all(isinstance(arc.get(key), str) and arc[key].strip() for key in ("id", "title", "summary")):
            raise StoryPackageError("story.arcModel.arcs 必须声明 id、title、summary")
        validate_state_constraints(arc.get("completionWhen"), state_fields, "story.arcModel.arcs.completionWhen")
        if arc.get("availableWhen") is not None:
            validate_state_constraints(arc["availableWhen"], state_fields, "story.arcModel.arcs.availableWhen")
        phase_ids = arc.get("phaseDirectionIds")
        if not isinstance(phase_ids, list) or not phase_ids or any(not isinstance(item, str) or item not in direction_ids for item in phase_ids):
            raise StoryPackageError("story.arcModel.arcs.phaseDirectionIds 必须引用已声明剧情方向")
        if len(phase_ids) != len(set(phase_ids)) or owned_phase_ids.intersection(phase_ids):
            raise StoryPackageError("一个小方向只能属于一个大方向")
        owned_phase_ids.update(phase_ids)
        next_arc_ids = arc.get("nextArcIds", [])
        if not isinstance(next_arc_ids, list) or any(not isinstance(item, str) or item not in arc_ids for item in next_arc_ids) or len(next_arc_ids) != len(set(next_arc_ids)):
            raise StoryPackageError("story.arcModel.arcs.nextArcIds 必须引用已声明大方向")
    if any(not isinstance(arc_id, str) or arc_id not in arc_ids for arc_id in entry_arc_ids) or len(entry_arc_ids) != len(set(entry_arc_ids)):
        raise StoryPackageError("story.arcModel.entryArcIds 必须引用已声明大方向")


def validate_state_model(package: Dict[str, Any], beats: List[Dict[str, Any]]) -> None:
    """Validate optional declarative co-creation state rules."""
    model = package.get("stateModel")
    if model is None:
        return
    if not isinstance(model, dict):
        raise StoryPackageError("stateModel 必须是对象")
    state_fields = set().union(*(set(beat.get("branchState", {})) for beat in beats))
    runtime_fields = model.get("runtimeStateFields", [])
    if not isinstance(runtime_fields, list) or any(not isinstance(field, str) or not field for field in runtime_fields):
        raise StoryPackageError("stateModel.runtimeStateFields 必须是非空状态字段名数组")
    state_fields.update(runtime_fields)
    location_fields = model.get("locationReferenceFields", [])
    if not isinstance(location_fields, list) or any(not isinstance(field, str) or field not in state_fields for field in location_fields):
        raise StoryPackageError("stateModel.locationReferenceFields 必须引用叙事状态字段")
    immutable = model.get("immutableFields", [])
    if not isinstance(immutable, list) or any(not isinstance(field, str) or field not in state_fields for field in immutable):
        raise StoryPackageError("stateModel.immutableFields 必须引用叙事状态字段")
    monotonic = model.get("monotonicEnums", {})
    if not isinstance(monotonic, dict):
        raise StoryPackageError("stateModel.monotonicEnums 必须是对象")
    for field, values in monotonic.items():
        if field not in state_fields or not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values) or len(values) != len(set(values)):
            raise StoryPackageError("stateModel.monotonicEnums 必须为已声明状态字段提供唯一字符串序列")
    for rule in model.get("transitionRules", []):
        if not isinstance(rule, dict) or rule.get("field") not in state_fields or rule.get("change") != "increment" or not isinstance(rule.get("amount"), int) or rule["amount"] < 1 or not isinstance(rule.get("message"), str):
            raise StoryPackageError("stateModel.transitionRules 声明无效")
        validate_state_constraints(rule.get("when"), state_fields, "stateModel.transitionRules.when")
    for invariant in model.get("invariants", []):
        if not isinstance(invariant, dict) or not isinstance(invariant.get("id"), str) or not isinstance(invariant.get("message"), str):
            raise StoryPackageError("stateModel.invariants 声明无效")
        validate_state_constraints(invariant.get("when"), state_fields, "stateModel.invariants.when")
        validate_state_constraints(invariant.get("require"), state_fields, "stateModel.invariants.require")
    derivative_entry = model.get("derivativeEntry")
    if derivative_entry is not None:
        if not isinstance(derivative_entry, dict) or not all(
            isinstance(derivative_entry.get(key), str) and derivative_entry[key].strip()
            for key in ("message", "narrativeText", "summary", "currentPhase", "chapterTitle")
        ):
            raise StoryPackageError("stateModel.derivativeEntry 声明无效")
        validate_state_constraints(derivative_entry.get("requiredState"), state_fields, "stateModel.derivativeEntry.requiredState")
        validate_state_patch(derivative_entry.get("statePatch"), state_fields, "stateModel.derivativeEntry.statePatch")
        if not isinstance(derivative_entry.get("openThreads"), list) or any(not isinstance(item, str) or not item.strip() for item in derivative_entry["openThreads"]):
            raise StoryPackageError("stateModel.derivativeEntry.openThreads 必须是非空字符串数组")
        next_direction = derivative_entry.get("nextDirection")
        if not isinstance(next_direction, dict) or not all(isinstance(next_direction.get(key), str) and next_direction[key].strip() for key in ("id", "title", "summary")):
            raise StoryPackageError("stateModel.derivativeEntry.nextDirection 声明无效")
        validate_state_patch(next_direction.get("statePatch"), state_fields, "stateModel.derivativeEntry.nextDirection.statePatch")
    for assertion in model.get("narrativeAssertions", []):
        if not isinstance(assertion, dict) or not isinstance(assertion.get("id"), str) or not isinstance(assertion.get("instruction"), str) or not isinstance(assertion.get("message"), str):
            raise StoryPackageError("stateModel.narrativeAssertions 声明无效")
        validate_state_constraints(assertion.get("when"), state_fields, "stateModel.narrativeAssertions.when")
        patterns = assertion.get("forbiddenPatterns")
        if not isinstance(patterns, list) or not patterns or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
            raise StoryPackageError("stateModel.narrativeAssertions.forbiddenPatterns 必须是非空字符串数组")
        try:
            for pattern in patterns:
                re.compile(pattern)
        except re.error as error:
            raise StoryPackageError("stateModel.narrativeAssertions 包含无效正则: " + str(error)) from error
    mock_followups = model.get("mockFollowups", [])
    if not isinstance(mock_followups, list):
        raise StoryPackageError("stateModel.mockFollowups 必须是数组")
    for template in mock_followups:
        if not isinstance(template, dict) or not isinstance(template.get("nextDirections"), list) or not template["nextDirections"]:
            raise StoryPackageError("stateModel.mockFollowups 声明无效")
        validate_state_constraints(template.get("when"), state_fields, "stateModel.mockFollowups.when")
        for direction in template["nextDirections"]:
            if not isinstance(direction, dict) or not all(isinstance(direction.get(key), str) and direction[key].strip() for key in ("id", "title", "summary")):
                raise StoryPackageError("stateModel.mockFollowups.nextDirections 声明无效")
            patch = direction.get("statePatch")
            if not isinstance(patch, dict) or not patch or any(field not in state_fields and field != "derivedAdditions" for field in patch):
                raise StoryPackageError("stateModel.mockFollowups.nextDirections.statePatch 必须只修改已声明状态字段")


def validate_state_constraints(value: Any, state_fields: Set[str], label: str) -> None:
    if not isinstance(value, dict) or not value:
        raise StoryPackageError(label + " 必须是非空对象")
    for field, expected in value.items():
        if field not in state_fields:
            raise StoryPackageError(label + " 引用了不存在的状态字段: " + field)
        if isinstance(expected, dict):
            allowed = {"equals", "oneOf", "sameAs"}
            if set(expected) - allowed or not expected:
                raise StoryPackageError(label + " 包含不支持的条件运算符")
            if "oneOf" in expected and (not isinstance(expected["oneOf"], list) or not expected["oneOf"]):
                raise StoryPackageError(label + ".oneOf 必须是非空数组")
            if "sameAs" in expected and expected["sameAs"] not in state_fields:
                raise StoryPackageError(label + ".sameAs 必须引用状态字段")


def validate_state_patch(value: Any, state_fields: Set[str], label: str) -> None:
    if not isinstance(value, dict) or not value or any(field not in state_fields for field in value):
        raise StoryPackageError(label + " 必须只修改已声明状态字段")


def clone_initial_state(package: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(package["initialState"])


def by_id(entries: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {entry["id"]: entry for entry in entries}


def entity_name(package: Dict[str, Any], entity_id: str) -> Optional[str]:
    for collection in (package["items"], package["locations"], package["characters"]):
        for entity in collection:
            if entity["id"] == entity_id:
                return entity["name"]
    return None


def package_path_from_root(root: Optional[Path] = None, version: Optional[str] = None) -> Path:
    project_root = root or Path(__file__).resolve().parents[1]
    package_id = os.environ.get("STORY_PACKAGE_ID", "rainy-waiting-room").strip()
    selected_version = version or os.environ.get("STORY_PACKAGE_VERSION", "0.1.1")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", package_id):
        raise StoryPackageError("STORY_PACKAGE_ID 必须是小写 kebab-case 内容包标识")
    if not re.fullmatch(r"\d+\.\d+\.\d+", selected_version):
        raise StoryPackageError("STORY_PACKAGE_VERSION 必须是 x.y.z 形式的内容版本")
    return project_root / "content" / "packages" / package_id / (selected_version + ".json")
