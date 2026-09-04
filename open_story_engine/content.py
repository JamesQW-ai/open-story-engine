"""StoryPackage loading and conservative structural validation."""

from __future__ import annotations

import copy
import json
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
    for beat in graph.get("beats", []):
        if beat.get("nodeId") not in ids["场景"]:
            raise StoryPackageError(f"叙事锚点引用的场景不存在: {beat.get('id')}")
        direction_ids = [direction.get("id") for direction in beat.get("nextDirections", [])]
        if len(direction_ids) != len(set(direction_ids)):
            raise StoryPackageError(f"叙事锚点存在重复方向: {beat.get('id')}")


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


def package_path_from_root(root: Optional[Path] = None) -> Path:
    project_root = root or Path(__file__).resolve().parents[1]
    return project_root / "content" / "packages" / "rainy-waiting-room" / "0.1.0.json"
