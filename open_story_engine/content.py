"""StoryPackage loading and conservative structural validation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Union


class StoryPackageError(ValueError):
    pass


NEW_CHARACTER_PROFILE_FIELD_IDS = frozenset({"name", "gender", "age", "occupation", "sourceRelationship", "background"})


class _SegmentedIndexEntries:
    """Resolve catalogued index segments only when one of their entries is used."""

    def __init__(self, index: Dict[str, Any], collection: str, segment_key: str, loader: Any) -> None:
        self._index = index
        self._collection = collection
        self._loader = loader
        raw_segments = index.get("segments")
        raw_locators = index.get("locators")
        if not isinstance(raw_segments, list) or not isinstance(raw_locators, list):
            raise StoryPackageError("分段索引缺少总清单。")
        self._segments: Dict[str, str] = {}
        for segment in raw_segments:
            if not isinstance(segment, dict) or not isinstance(segment.get(segment_key), str) or not isinstance(segment.get("path"), str):
                raise StoryPackageError("分段索引总清单无效。")
            key = segment[segment_key]
            if key in self._segments:
                raise StoryPackageError("分段索引总清单包含重复键。")
            self._segments[key] = segment["path"]
        self._locators: List[Dict[str, Any]] = []
        self._locator_by_id: Dict[str, Dict[str, Any]] = {}
        for locator in raw_locators:
            if not isinstance(locator, dict) or not isinstance(locator.get("id"), str) or not isinstance(locator.get(segment_key), str):
                raise StoryPackageError("分段索引定位记录无效。")
            if locator[segment_key] not in self._segments or locator["id"] in self._locator_by_id:
                raise StoryPackageError("分段索引定位记录引用无效。")
            self._locators.append(locator)
            self._locator_by_id[locator["id"]] = locator
        self._segment_cache: Dict[str, List[Dict[str, Any]]] = {}

    def _segment(self, key: str) -> List[Dict[str, Any]]:
        if key not in self._segment_cache:
            module = self._loader(self._segments[key])
            entries = module.get(self._collection)
            if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                raise StoryPackageError("分段索引内容无效。")
            self._segment_cache[key] = entries
        return self._segment_cache[key]

    def get_by_id(self, identifier: str) -> Optional[Dict[str, Any]]:
        locator = self._locator_by_id.get(identifier)
        if locator is None:
            return None
        return next((entry for entry in self._segment(locator[next(key for key in locator if key.endswith("Id") and key != "id")]) if entry.get("id") == identifier), None)

    def has_id(self, identifier: Any) -> bool:
        return isinstance(identifier, str) and identifier in self._locator_by_id

    def get_by_source_progress(self, source_progress: Any) -> Optional[Dict[str, Any]]:
        locator = next((entry for entry in self._locators if entry.get("sourceProgress") == source_progress), None)
        return self.get_by_id(locator["id"]) if locator else None

    def __iter__(self):
        for locator in self._locators:
            entry = self.get_by_id(locator["id"])
            if entry is not None:
                yield entry

    def __len__(self) -> int:
        return len(self._locators)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        if not isinstance(index, int):
            raise TypeError("分段索引只支持整数索引")
        if index < 0:
            index += len(self._locators)
        if index < 0 or index >= len(self._locators):
            raise IndexError(index)
        entry = self.get_by_id(self._locators[index]["id"])
        if entry is None:
            raise StoryPackageError("分段索引定位记录与分段内容不一致。")
        return entry


class _SegmentedEntryIndexEntries:
    """Load entry menu cards by selected role, while retaining ID lookup for a confirmed choice."""

    def __init__(self, index: Dict[str, Any], loader: Any) -> None:
        selectors = index.get("selectors")
        locators = index.get("locators")
        if not isinstance(selectors, dict) or not isinstance(locators, list):
            raise StoryPackageError("进入节点分段索引无效。")
        self._loader = loader
        self._paths: Dict[str, str] = {}
        for selector in selectors.get("sourceCharacters", []):
            if not isinstance(selector, dict) or not isinstance(selector.get("id"), str) or not isinstance(selector.get("path"), str):
                raise StoryPackageError("原著角色进入索引无效。")
            self._paths["source:" + selector["id"]] = selector["path"]
        new_character = selectors.get("newCharacter")
        if not isinstance(new_character, dict) or not isinstance(new_character.get("path"), str):
            raise StoryPackageError("新建角色进入索引无效。")
        self._paths["new"] = new_character["path"]
        self._locator_by_id = {
            entry["id"]: entry for entry in locators
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("path"), str)
        }
        if len(self._locator_by_id) != len(locators):
            raise StoryPackageError("进入节点定位记录无效。")
        self._cache: Dict[str, List[Dict[str, Any]]] = {}

    def _entries(self, key: str) -> List[Dict[str, Any]]:
        if key not in self._cache:
            module = self._loader(self._paths[key])
            entries = module.get("entryPoints")
            if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                raise StoryPackageError("进入节点分段内容无效。")
            self._cache[key] = entries
        return self._cache[key]

    def get_by_id(self, identifier: str) -> Optional[Dict[str, Any]]:
        locator = self._locator_by_id.get(identifier)
        if locator is None:
            return None
        module = self._loader(locator["path"])
        entries = module.get("entryPoints")
        return next((entry for entry in entries if isinstance(entry, dict) and entry.get("id") == identifier), None) if isinstance(entries, list) else None

    def has_id(self, identifier: Any) -> bool:
        return isinstance(identifier, str) and identifier in self._locator_by_id

    def for_source_character(self, character_id: str) -> List[Dict[str, Any]]:
        return list(self._entries("source:" + character_id)) if "source:" + character_id in self._paths else []

    def for_new_character(self) -> List[Dict[str, Any]]:
        return list(self._entries("new"))

    def __iter__(self):
        for identifier in self._locator_by_id:
            entry = self.get_by_id(identifier)
            if entry is not None:
                yield entry

    def __len__(self) -> int:
        return len(self._locator_by_id)


def load_story_package(path: Union[str, Path]) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as source:
        package = json.load(source)
    validate_story_package(package)
    return package


class _LazyBeatSequence:
    """Read hash-verified beat modules only when a runtime operation reaches them."""

    def __init__(self, entries: Any, loader: Any) -> None:
        self._entries = entries
        self._loader = loader
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.loaded_ids: List[str] = []
        self._entry_by_id = {} if hasattr(entries, "get_by_id") else {entry["id"]: entry for entry in entries}
        self._entry_by_progress = {} if hasattr(entries, "get_by_source_progress") else {
            entry["sourceProgress"]: entry for entry in entries if isinstance(entry.get("sourceProgress"), str)
        }

    def get_by_id(self, beat_id: str) -> Optional[Dict[str, Any]]:
        entry = self._entries.get_by_id(beat_id) if hasattr(self._entries, "get_by_id") else self._entry_by_id.get(beat_id)
        return self._load(entry) if entry else None

    def get_by_source_progress(self, source_progress: Any) -> Optional[Dict[str, Any]]:
        entry = self._entries.get_by_source_progress(source_progress) if hasattr(self._entries, "get_by_source_progress") else self._entry_by_progress.get(source_progress)
        return self._load(entry) if entry else None

    def _load(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        beat_id = entry["id"]
        if beat_id not in self._cache:
            module = self._loader(entry["path"])
            beat = module.get("beat")
            if not isinstance(beat, dict) or beat.get("id") != beat_id:
                raise StoryPackageError("剧情节点模块与索引不一致: " + beat_id)
            self._cache[beat_id] = copy.deepcopy(beat)
            self.loaded_ids.append(beat_id)
        return self._cache[beat_id]

    def __iter__(self):
        for entry in self._entries:
            yield self._load(entry)

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        if not isinstance(index, int):
            raise TypeError("剧情节点只支持整数索引")
        if index < 0:
            index += len(self._entries)
        if index < 0 or index >= len(self._entries):
            raise IndexError(index)
        for offset, beat in enumerate(self):
            if offset == index:
                return beat
        raise IndexError(index)


class _LazyNodeSequence:
    """Read hash-verified scene modules only when runtime needs that scene."""

    def __init__(self, entries: Any, loader: Any) -> None:
        self._entries = entries
        self._loader = loader
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.loaded_ids: List[str] = []
        self._entry_by_id = {} if hasattr(entries, "get_by_id") else {entry["id"]: entry for entry in entries}

    def get_by_id(self, node_id: str) -> Optional[Dict[str, Any]]:
        entry = self._entry_by_id.get(node_id)
        return self._load(entry) if entry else None

    def _load(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        node_id = entry["id"]
        if node_id not in self._cache:
            module = self._loader(entry["path"])
            node = module.get("node")
            if not isinstance(node, dict) or node.get("id") != node_id:
                raise StoryPackageError("场景模块与索引不一致: " + node_id)
            self._cache[node_id] = copy.deepcopy(node)
            self.loaded_ids.append(node_id)
        return self._cache[node_id]

    def __iter__(self):
        for entry in self._entries:
            yield self._load(entry)

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        if not isinstance(index, int):
            raise TypeError("场景只支持整数索引")
        if index < 0:
            index += len(self._entries)
        if index < 0 or index >= len(self._entries):
            raise IndexError(index)
        return self._load(self._entries[index])


class _LazyArcSequence:
    """Read hash-verified macro-plot modules only for a selected direction."""

    def __init__(self, entries: Any, loader: Any) -> None:
        self._entries = entries
        self._loader = loader
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.loaded_ids: List[str] = []
        self._entry_by_id = {} if hasattr(entries, "get_by_id") else {entry["id"]: entry for entry in entries}

    def get_by_id(self, arc_id: str) -> Optional[Dict[str, Any]]:
        entry = self._entries.get_by_id(arc_id) if hasattr(self._entries, "get_by_id") else self._entry_by_id.get(arc_id)
        return self._load(entry) if entry else None

    def _load(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        arc_id = entry["id"]
        if arc_id not in self._cache:
            module = self._loader(entry["path"])
            arc = module.get("arc")
            if not isinstance(arc, dict) or arc.get("id") != arc_id:
                raise StoryPackageError("大方向模块与索引不一致: " + arc_id)
            self._cache[arc_id] = copy.deepcopy(arc)
            self.loaded_ids.append(arc_id)
        return self._cache[arc_id]

    def __iter__(self):
        for entry in self._entries:
            yield self._load(entry)

    def __len__(self) -> int:
        return len(self._entries)


class _LazyEntryPointSequence:
    """Use entry cards for menus and load an entry module only after selection."""

    def __init__(self, entries: Any, loader: Any) -> None:
        self._entries = entries
        self._loader = loader
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.loaded_ids: List[str] = []
        self._entry_by_id = {} if hasattr(entries, "get_by_id") else {entry["id"]: entry for entry in entries}

    def get_by_id(self, entry_id: str) -> Optional[Dict[str, Any]]:
        entry = self._entries.get_by_id(entry_id) if hasattr(self._entries, "get_by_id") else self._entry_by_id.get(entry_id)
        return self._load(entry) if entry else None

    def for_source_character(self, character_id: str) -> List[Dict[str, Any]]:
        if hasattr(self._entries, "for_source_character"):
            return [self._card(entry) for entry in self._entries.for_source_character(character_id)]
        return [self._card(entry) for entry in self._entries if character_id in entry.get("sourceCharacterIds", [])]

    def for_new_character(self) -> List[Dict[str, Any]]:
        if hasattr(self._entries, "for_new_character"):
            return [self._card(entry) for entry in self._entries.for_new_character()]
        return [self._card(entry) for entry in self._entries if entry.get("availableToNewCharacter")]

    @staticmethod
    def _card(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {key: copy.deepcopy(value) for key, value in entry.items() if key != "path"}

    def _load(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        entry_id = entry["id"]
        if entry_id not in self._cache:
            module = self._loader(entry["path"])
            loaded = module.get("entryPoint")
            if not isinstance(loaded, dict) or loaded.get("id") != entry_id:
                raise StoryPackageError("进入节点模块与索引不一致: " + entry_id)
            self._cache[entry_id] = copy.deepcopy(loaded)
            self.loaded_ids.append(entry_id)
        return self._cache[entry_id]

    def __iter__(self):
        for entry in self._entries:
            yield self._load(entry)

    def __len__(self) -> int:
        return len(self._entries)


def load_runtime_story_package(path: Union[str, Path], lazy: bool = False) -> Dict[str, Any]:
    """Load a module-backed runtime projection without reading source excerpts.

    Script-generated packages provide a hash-bound module tree. The projection
    reconstructs the runtime contract from those modules and deliberately omits
    every NarrativeBeat ``sourceExcerpt``. Historical packages keep the original
    single-file loader path.
    """
    package_path = Path(path)
    module_directory = package_path.parent / "modules"
    index_path = module_directory / "package-index.json"
    if not index_path.is_file():
        return load_story_package(package_path)
    index = _read_runtime_json(index_path, "模块索引")
    if index.get("schemaVersion") != "story-package-module-index/0.1":
        raise StoryPackageError("模块索引 schemaVersion 不匹配。")
    package_ref = index.get("package")
    if not isinstance(package_ref, dict) or not all(
        isinstance(package_ref.get(key), str) and package_ref[key]
        for key in ("id", "version")
    ):
        raise StoryPackageError("模块索引缺少 StoryPackage 标识。")
    descriptors = index.get("modules")
    if not isinstance(descriptors, list):
        raise StoryPackageError("模块索引缺少 modules 列表。")
    module_hashes: Dict[str, str] = {}
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise StoryPackageError("模块索引包含无效描述符。")
        relative_path, digest = descriptor.get("path"), descriptor.get("sha256")
        _validate_module_path(relative_path)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise StoryPackageError("模块索引包含无效 SHA-256。")
        if relative_path in module_hashes:
            raise StoryPackageError("模块索引包含重复路径。")
        module_hashes[relative_path] = digest

    def read_module(relative_path: str) -> Dict[str, Any]:
        _validate_module_path(relative_path)
        expected = module_hashes.get(relative_path)
        if expected is None:
            raise StoryPackageError("模块索引未声明运行时模块: " + relative_path)
        module = _read_runtime_json(module_directory / relative_path, "运行时模块 " + relative_path)
        if module.get("package") != package_ref:
            raise StoryPackageError("运行时模块未绑定当前 StoryPackage: " + relative_path)
        if _canonical_json_sha256(module) != expected:
            raise StoryPackageError("运行时模块 SHA-256 不匹配: " + relative_path)
        return module

    indexes = index.get("indexes")
    if not isinstance(indexes, dict):
        raise StoryPackageError("模块索引缺少 indexes。")
    if indexes.get("runtime") != "runtime-index.json":
        # Older modular packages remain readable through their frozen root.
        return load_story_package(package_path)
    runtime = read_module("runtime-index.json")
    if runtime.get("schemaVersion") == "story-package-runtime-index/0.5":
        return _load_segmented_runtime_projection(read_module, index, package_ref, runtime, lazy)
    if runtime.get("schemaVersion") in (
        "story-package-runtime-index/0.1", "story-package-runtime-index/0.2", "story-package-runtime-index/0.3",
    ):
        return load_story_package(package_path)
    if runtime.get("schemaVersion") != "story-package-runtime-index/0.4":
        raise StoryPackageError("运行时索引 schemaVersion 不匹配。")
    expected_core = {
        "world": "world.json", "state": "state-schema.json", "story": "main-story-graph.json", "nodes": "node-index.json",
        "arcModel": "arc-model.json", "arcs": "arc-index.json", "entryModel": "entry-model.json", "entries": "entry-index.json",
        "beats": "beat-index.json", "chapters": "chapter-index.json",
    }
    expected_entities = {
        "characters": "character-index.json", "locations": "location-index.json",
        "items": "item-index.json", "relationships": "relationship-index.json",
    }
    if (
        runtime.get("coreModules") != expected_core
        or runtime.get("entityIndexes") != expected_entities
    ):
        raise StoryPackageError("运行时索引声明与受支持的模块布局不匹配。")
    world = read_module(expected_core["world"])
    state = read_module(expected_core["state"])
    graph = read_module(expected_core["story"])
    node_index = read_module(expected_core["nodes"])
    arc_model_module = read_module(expected_core["arcModel"])
    arc_index = read_module(expected_core["arcs"])
    entry_model_module = read_module(expected_core["entryModel"])
    entry_index = read_module(expected_core["entries"])
    node_entries = node_index.get("nodes")
    if not isinstance(node_entries, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str)
        for entry in node_entries
    ):
        raise StoryPackageError("场景索引无效。")
    node_ids = [entry["id"] for entry in node_entries]
    if not node_ids or len(node_ids) != len(set(node_ids)):
        raise StoryPackageError("场景索引必须包含唯一非空 ID。")
    arc_model = arc_model_module.get("arcModel")
    arc_entries = arc_index.get("arcs")
    if not isinstance(arc_model, dict) or not isinstance(arc_entries, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str)
        for entry in arc_entries
    ):
        raise StoryPackageError("大方向模块索引无效。")
    arc_ids = [entry["id"] for entry in arc_entries]
    if len(arc_ids) != len(set(arc_ids)) or any(arc_id not in arc_ids for arc_id in arc_model.get("entryArcIds", [])):
        raise StoryPackageError("大方向模型引用了不存在的入口。")
    entry_model = entry_model_module.get("entryModel")
    entry_entries = entry_index.get("entryPoints")
    if not isinstance(entry_model, dict) or not isinstance(entry_entries, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str)
        for entry in entry_entries
    ):
        raise StoryPackageError("进入模型索引无效。")
    entry_ids = [entry["id"] for entry in entry_entries]
    if len(entry_ids) != len(set(entry_ids)) or entry_model.get("defaultEntryPointId") not in entry_ids:
        raise StoryPackageError("进入模型引用了不存在的入口。")
    beat_index = read_module(expected_core["beats"])
    beat_entries = beat_index.get("beats")
    if not isinstance(beat_entries, list):
        raise StoryPackageError("剧情节点索引无效。")
    if any(entry.get("nodeId") not in node_ids for entry in beat_entries if isinstance(entry, dict)):
        raise StoryPackageError("剧情节点索引引用了不存在的场景。")
    timeline: List[Dict[str, Any]] = []
    for entry in beat_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise StoryPackageError("剧情节点索引包含无效路径。")
        module_timeline = entry.get("timeline", [])
        if not isinstance(module_timeline, list):
            raise StoryPackageError("剧情节点模块时间线无效: " + str(entry.get("id")))
        timeline.extend(copy.deepcopy(item) for item in module_timeline if isinstance(item, dict))
    if len({item.get("id") for item in timeline}) != len(timeline):
        raise StoryPackageError("剧情节点模块包含重复时间线。")
    timeline.sort(key=lambda item: item.get("order", 0))

    def read_entities(kind: str, singular: str) -> List[Dict[str, Any]]:
        entity_index = read_module(expected_entities[kind])
        entries = entity_index.get(kind)
        if not isinstance(entries, list):
            raise StoryPackageError("实体索引无效: " + kind)
        if lazy:
            return [{key: copy.deepcopy(value) for key, value in entry.items() if key != "path"} for entry in entries if isinstance(entry, dict)]
        entities: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise StoryPackageError("实体索引包含无效路径: " + kind)
            entity = read_module(entry["path"]).get(singular)
            if not isinstance(entity, dict) or entity.get("id") != entry.get("id"):
                raise StoryPackageError("实体模块与索引不一致: " + str(entry.get("id")))
            entities.append(copy.deepcopy(entity))
        return entities

    narrative_graph = graph.get("narrativeGraph")
    story = graph.get("story")
    if not isinstance(story, dict) or not isinstance(narrative_graph, dict):
        raise StoryPackageError("主剧情图模块无效。")
    if story.get("startNodeId") not in node_ids or state.get("initialState", {}).get("currentNodeId") not in node_ids:
        raise StoryPackageError("运行时图谱引用了不存在的起始场景。")
    beats: Any = _LazyBeatSequence(beat_entries, read_module) if lazy else []
    if not lazy:
        beats = list(_LazyBeatSequence(beat_entries, read_module))
    nodes: Any = _LazyNodeSequence(node_entries, read_module) if lazy else []
    if not lazy:
        nodes = list(_LazyNodeSequence(node_entries, read_module))
    arcs: Any = _LazyArcSequence(arc_entries, read_module) if lazy else []
    if not lazy:
        arcs = list(_LazyArcSequence(arc_entries, read_module))
    entry_points: Any = _LazyEntryPointSequence(entry_entries, read_module) if lazy else []
    if not lazy:
        entry_points = list(_LazyEntryPointSequence(entry_entries, read_module))
    projection = {
        "schemaVersion": "1.0", "id": package_ref["id"], "version": package_ref["version"],
        "metadata": copy.deepcopy(world.get("metadata")),
        "sourceAnalysis": copy.deepcopy(index.get("sourceAnalysis")),
        "world": copy.deepcopy(world.get("world")), "rules": copy.deepcopy(world.get("rules")),
        "initialState": copy.deepcopy(state.get("initialState")), "stateModel": copy.deepcopy(state.get("stateModel")),
        "story": {
            **copy.deepcopy(story), "nodes": nodes,
            "arcModel": {**copy.deepcopy(arc_model), "arcs": arcs},
            "entryModel": {**copy.deepcopy(entry_model), "entryPoints": entry_points},
            "narrativeGraph": {**copy.deepcopy(narrative_graph), "beats": beats},
        },
        "directions": copy.deepcopy(graph.get("directions")), "defaultDirectionId": graph.get("defaultDirectionId"),
        "characters": read_entities("characters", "character"),
        "locations": read_entities("locations", "location"),
        "items": read_entities("items", "item"),
        "relationships": read_entities("relationships", "relationship"),
        "timeline": timeline, "moduleIndexSha256": _canonical_json_sha256(index),
    }
    if lazy:
        _validate_runtime_projection(projection)
    else:
        validate_story_package(projection)
    return projection


def _load_segmented_runtime_projection(
    read_module: Any, index: Dict[str, Any], package_ref: Dict[str, str], runtime: Dict[str, Any], lazy: bool,
) -> Dict[str, Any]:
    """Build the runtime projection from root catalogs without eagerly reading their segments."""
    expected_core = {
        "world": "world.json", "state": "state-schema.json", "story": "main-story-graph.json", "nodes": "node-index.json",
        "arcModel": "arc-model.json", "arcs": "arc-index.json", "entryModel": "entry-model.json", "entries": "entry-index.json",
        "beats": "beat-index.json", "chapters": "chapter-index.json",
    }
    expected_entities = {
        "characters": "character-index.json", "locations": "location-index.json",
        "items": "item-index.json", "relationships": "relationship-index.json",
    }
    if runtime.get("coreModules") != expected_core or runtime.get("entityIndexes") != expected_entities:
        raise StoryPackageError("运行时索引声明与受支持的分段模块布局不匹配。")
    world = read_module("world.json")
    state = read_module("state-schema.json")
    graph = read_module("main-story-graph.json")
    node_index = read_module("node-index.json")
    arc_model_module = read_module("arc-model.json")
    arc_index = read_module("arc-index.json")
    entry_model_module = read_module("entry-model.json")
    entry_index = read_module("entry-index.json")
    beat_index = read_module("beat-index.json")
    chapter_index = read_module("chapter-index.json")
    node_entries = node_index.get("nodes")
    if not isinstance(node_entries, list) or not node_entries:
        raise StoryPackageError("场景索引无效。")
    node_ids = [entry.get("id") for entry in node_entries if isinstance(entry, dict)]
    if len(node_ids) != len(node_entries) or any(not isinstance(node_id, str) for node_id in node_ids) or len(set(node_ids)) != len(node_ids):
        raise StoryPackageError("场景索引必须包含唯一非空 ID。")
    chapter_catalog = chapter_index.get("chapters")
    if not isinstance(chapter_catalog, list) or not chapter_catalog or any(
        not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str)
        for entry in chapter_catalog
    ):
        raise StoryPackageError("章节总清单无效。")
    beat_entries = _SegmentedIndexEntries(beat_index, "beats", "chapterId", read_module)
    arc_entries = _SegmentedIndexEntries(arc_index, "arcs", "nodeId", read_module)
    entry_entries = _SegmentedEntryIndexEntries(entry_index, read_module)
    arc_model = arc_model_module.get("arcModel")
    entry_model = entry_model_module.get("entryModel")
    if not isinstance(arc_model, dict) or not isinstance(entry_model, dict):
        raise StoryPackageError("剧情方向或进入模型无效。")
    if any(not arc_entries.has_id(arc_id) for arc_id in arc_model.get("entryArcIds", [])):
        raise StoryPackageError("大方向模型引用了不存在的入口。")
    if not entry_entries.has_id(entry_model.get("defaultEntryPointId")):
        raise StoryPackageError("进入模型引用了不存在的入口。")
    narrative_graph = graph.get("narrativeGraph")
    story = graph.get("story")
    if not isinstance(story, dict) or not isinstance(narrative_graph, dict):
        raise StoryPackageError("主剧情图模块无效。")
    if story.get("startNodeId") not in node_ids or state.get("initialState", {}).get("currentNodeId") not in node_ids:
        raise StoryPackageError("运行时图谱引用了不存在的起始场景。")
    if not beat_entries.has_id(narrative_graph.get("startBeatId")):
        raise StoryPackageError("叙事图引用了不存在的起始剧情节点。")

    def read_entities(kind: str, singular: str) -> List[Dict[str, Any]]:
        entity_index = read_module(expected_entities[kind])
        entries = entity_index.get(kind)
        if not isinstance(entries, list):
            raise StoryPackageError("实体索引无效: " + kind)
        if lazy:
            return [{key: copy.deepcopy(value) for key, value in entry.items() if key != "path"} for entry in entries if isinstance(entry, dict)]
        result = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise StoryPackageError("实体索引包含无效路径: " + kind)
            entity = read_module(entry["path"]).get(singular)
            if not isinstance(entity, dict) or entity.get("id") != entry.get("id"):
                raise StoryPackageError("实体模块与索引不一致: " + str(entry.get("id")))
            result.append(copy.deepcopy(entity))
        return result

    beats: Any = _LazyBeatSequence(beat_entries, read_module) if lazy else list(_LazyBeatSequence(beat_entries, read_module))
    nodes: Any = _LazyNodeSequence(node_entries, read_module) if lazy else list(_LazyNodeSequence(node_entries, read_module))
    arcs: Any = _LazyArcSequence(arc_entries, read_module) if lazy else list(_LazyArcSequence(arc_entries, read_module))
    entry_points: Any = _LazyEntryPointSequence(entry_entries, read_module) if lazy else list(_LazyEntryPointSequence(entry_entries, read_module))
    timeline = [] if lazy else [copy.deepcopy(item) for beat in beat_entries for item in beat.get("timeline", []) if isinstance(item, dict)]
    timeline.sort(key=lambda item: item.get("order", 0))
    projection = {
        "schemaVersion": "1.0", "id": package_ref["id"], "version": package_ref["version"],
        "metadata": copy.deepcopy(world.get("metadata")), "sourceAnalysis": copy.deepcopy(index.get("sourceAnalysis")),
        "world": copy.deepcopy(world.get("world")), "rules": copy.deepcopy(world.get("rules")),
        "initialState": copy.deepcopy(state.get("initialState")), "stateModel": copy.deepcopy(state.get("stateModel")),
        "story": {
            **copy.deepcopy(story), "nodes": nodes,
            "arcModel": {**copy.deepcopy(arc_model), "arcs": arcs},
            "entryModel": {**copy.deepcopy(entry_model), "entryPoints": entry_points},
            "narrativeGraph": {**copy.deepcopy(narrative_graph), "beats": beats},
        },
        "directions": copy.deepcopy(graph.get("directions")), "defaultDirectionId": graph.get("defaultDirectionId"),
        "characters": read_entities("characters", "character"), "locations": read_entities("locations", "location"),
        "items": read_entities("items", "item"), "relationships": read_entities("relationships", "relationship"),
        "timeline": timeline, "moduleIndexSha256": _canonical_json_sha256(index),
    }
    if lazy:
        _validate_runtime_projection(projection)
    else:
        validate_story_package(projection)
    return projection


def _read_runtime_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StoryPackageError("无法读取" + label + "：" + str(error)) from error
    if not isinstance(content, dict):
        raise StoryPackageError(label + "必须是对象。")
    return content


def _validate_module_path(path: Any) -> None:
    if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
        raise StoryPackageError("模块索引包含不安全路径。")


def _canonical_json_sha256(content: Dict[str, Any]) -> str:
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_runtime_projection(package: Dict[str, Any]) -> None:
    """Check the non-lazy core while offline audit owns full graph validation."""
    required = ("id", "version", "metadata", "world", "rules", "initialState", "stateModel", "story")
    if any(key not in package for key in required):
        raise StoryPackageError("运行期投影缺少必要字段。")
    graph = package["story"].get("narrativeGraph")
    if not isinstance(graph, dict) or not isinstance(graph.get("startBeatId"), str):
        raise StoryPackageError("运行期投影缺少叙事图起点。")
    if not all(isinstance(package.get(name), list) for name in ("characters", "locations", "items", "timeline")):
        raise StoryPackageError("运行期投影实体或时间线索引无效。")


def validate_story_package(package: Dict[str, Any]) -> None:
    """Validate cross references used by the Python runtime.

    The source schema remains the StoryPackage JSON contract. This intentionally
    avoids accepting malformed content merely because Python is dynamically typed.
    """
    module_index_sha256 = package.get("moduleIndexSha256")
    if module_index_sha256 is not None and (
        not isinstance(module_index_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", module_index_sha256)
    ):
        raise StoryPackageError("moduleIndexSha256 必须是 64 位小写 SHA-256。")
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
    validate_entry_model(package, beats, ids)


def validate_entry_model(package: Dict[str, Any], beats: List[Dict[str, Any]], ids: Dict[str, Set[str]]) -> None:
    """Validate optional, package-owned role and plot-node entry choices."""
    model = package.get("story", {}).get("entryModel")
    if model is None:
        return
    if isinstance(model, dict) and model.get("policy") == "official_unknown_reader/1":
        from .official_openings import validate_official_openings
        validate_official_openings(package)
    if not isinstance(model, dict):
        raise StoryPackageError("story.entryModel 必须是对象")
    entries = model.get("entryPoints")
    source_character_ids = model.get("sourceCharacterIds")
    if not isinstance(entries, list) or not entries:
        raise StoryPackageError("story.entryModel.entryPoints 必须是非空数组")
    if not isinstance(source_character_ids, list) or not source_character_ids:
        raise StoryPackageError("story.entryModel.sourceCharacterIds 必须是非空数组")
    if (
        any(not isinstance(item, str) or item not in ids["角色"] for item in source_character_ids)
        or len(source_character_ids) != len(set(source_character_ids))
    ):
        raise StoryPackageError("story.entryModel.sourceCharacterIds 必须引用唯一的已登记角色")
    entry_ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    if (
        len(entry_ids) != len(entries)
        or any(not isinstance(item, str) or not item for item in entry_ids)
        or len(entry_ids) != len(set(entry_ids))
    ):
        raise StoryPackageError("story.entryModel.entryPoints 必须包含唯一非空 id")
    if model.get("defaultEntryPointId") not in entry_ids:
        raise StoryPackageError("story.entryModel.defaultEntryPointId 必须引用已声明进入节点")
    new_character = model.get("newCharacter")
    if not isinstance(new_character, dict) or not isinstance(new_character.get("enabled"), bool):
        raise StoryPackageError("story.entryModel.newCharacter 必须声明 enabled")
    profile_fields = new_character.get("profileFields")
    if profile_fields is not None:
        if not isinstance(profile_fields, list) or not profile_fields:
            raise StoryPackageError("newCharacter.profileFields 必须是非空数组")
        field_ids = [field.get("id") for field in profile_fields if isinstance(field, dict)]
        if len(field_ids) != len(profile_fields) or any(not isinstance(field_id, str) or not field_id for field_id in field_ids):
            raise StoryPackageError("newCharacter.profileFields 必须包含非空 id")
        if len(field_ids) != len(set(field_ids)) or not NEW_CHARACTER_PROFILE_FIELD_IDS.issubset(field_ids):
            raise StoryPackageError("newCharacter.profileFields 必须包含 name、gender、age、occupation、sourceRelationship、background")
        for field in profile_fields:
            if not isinstance(field.get("label"), str) or not field["label"].strip() or field.get("type") not in ("text", "integer"):
                raise StoryPackageError("newCharacter.profileFields 必须声明 label 与支持的 type")
            if field["type"] == "text":
                minimum = field.get("minLength", 1)
                maximum = field.get("maxLength", 500)
                if not isinstance(minimum, int) or not isinstance(maximum, int) or minimum < 1 or maximum < minimum:
                    raise StoryPackageError("文本角色档案字段的长度范围无效")
            else:
                minimum = field.get("minimum")
                maximum = field.get("maximum")
                if not isinstance(minimum, int) or not isinstance(maximum, int) or maximum < minimum:
                    raise StoryPackageError("整数角色档案字段必须声明有效范围")
    beat_by_id = {beat["id"]: beat for beat in beats}
    timeline_ids = {item.get("id") for item in package.get("timeline", [])}
    for entry in entries:
        if not all(isinstance(entry.get(key), str) and entry[key].strip() for key in ("id", "title", "summary", "chapterTitle", "nodeId", "beatId")):
            raise StoryPackageError("story.entryModel.entryPoints 必须声明展示信息和节点引用")
        source_chapter_id = entry.get("sourceChapterId")
        if source_chapter_id is not None and (not isinstance(source_chapter_id, str) or not source_chapter_id.strip()):
            raise StoryPackageError("story.entryModel.entryPoints.sourceChapterId 必须是非空字符串")
        beat = beat_by_id.get(entry["beatId"])
        if beat is None or entry["nodeId"] not in ids["场景"] or beat["nodeId"] != entry["nodeId"]:
            raise StoryPackageError("story.entryModel.entryPoints 的叙事锚点和场景必须对应")
        related = entry.get("sourceCharacterIds", [])
        if not isinstance(related, list) or any(item not in source_character_ids for item in related) or len(related) != len(set(related)):
            raise StoryPackageError("story.entryModel.entryPoints.sourceCharacterIds 必须引用可选原著角色")
        timeline_refs = entry.get("timelineRefs", [])
        if not isinstance(timeline_refs, list) or any(item not in timeline_ids for item in timeline_refs) or len(timeline_refs) != len(set(timeline_refs)):
            raise StoryPackageError("story.entryModel.entryPoints.timelineRefs 必须引用唯一时间线摘要")
        if not related and not entry.get("availableToNewCharacter", False):
            raise StoryPackageError("每个进入节点至少要向一个原著角色或新建角色开放")
        if not isinstance(entry.get("availableToNewCharacter", False), bool):
            raise StoryPackageError("story.entryModel.entryPoints.availableToNewCharacter 必须是布尔值")
        if entry.get("availableToNewCharacter") and not isinstance(entry.get("newCharacterNarrative"), str):
            raise StoryPackageError("允许新建角色的进入节点必须声明 newCharacterNarrative")
        source_narratives = entry.get("sourceCharacterNarratives", {})
        if not isinstance(source_narratives, dict) or any(
            character_id not in related or not isinstance(narrative, str) or not narrative.strip()
            for character_id, narrative in source_narratives.items()
        ):
            raise StoryPackageError("sourceCharacterNarratives 必须为该入口原著角色声明非空开场")
        canonical_character_id = package.get("initialState", {}).get("player", {}).get("characterId")
        if any(character_id != canonical_character_id and character_id not in source_narratives for character_id in related):
            raise StoryPackageError("非默认原著角色进入节点必须声明 sourceCharacterNarratives")


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


def story_node_by_id(package: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """Resolve one scene without enumerating a lazily loaded node collection."""
    nodes = package.get("story", {}).get("nodes", [])
    indexed = getattr(nodes, "get_by_id", None)
    if callable(indexed):
        return indexed(node_id)
    return next((node for node in nodes if node.get("id") == node_id), None)


def entity_name(package: Dict[str, Any], entity_id: str) -> Optional[str]:
    for collection in (package["items"], package["locations"], package["characters"]):
        for entity in collection:
            if entity["id"] == entity_id:
                return entity["name"]
    return None


def package_path_from_root(root: Optional[Path] = None, version: Optional[str] = None) -> Path:
    project_root = root or Path(__file__).resolve().parents[1]
    package_id = os.environ.get("STORY_PACKAGE_ID", "taixu-relics-part1").strip()
    selected_version = version or os.environ.get("STORY_PACKAGE_VERSION", "0.1.3")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", package_id):
        raise StoryPackageError("STORY_PACKAGE_ID 必须是小写 kebab-case 内容包标识")
    if not re.fullmatch(r"\d+\.\d+\.\d+", selected_version):
        raise StoryPackageError("STORY_PACKAGE_VERSION 必须是 x.y.z 形式的内容版本")
    return project_root / "content" / "packages" / package_id / selected_version / "package.json"


def reader_path_from_package_path(package_path: Path) -> Path:
    """Return the local-only chapter reader paired with a version directory."""
    return package_path.with_name("reader.json")
