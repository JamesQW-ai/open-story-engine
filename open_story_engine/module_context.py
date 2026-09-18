"""Bounded prompt context selected from script-generated StoryPackage modules."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


class ModuleContextError(ValueError):
    pass


def _canonical_sha256(content: Dict[str, Any]) -> str:
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ModuleContextResolver:
    """Load only prompt-relevant modules and never permit reader modules."""

    def __init__(self, module_directory: Path, package: Dict[str, Any]) -> None:
        self.module_directory = module_directory
        self.package_ref = {"id": package["id"], "version": package["version"]}
        self.module_index_sha256 = package.get("moduleIndexSha256")
        self.loaded_paths: List[str] = []
        self.last_resolved_paths: List[str] = []
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.index = self._read_index()
        descriptors = self.index.get("modules")
        if not isinstance(descriptors, list):
            raise ModuleContextError("模块索引缺少 modules 列表。")
        self.descriptors: Dict[str, str] = {}
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise ModuleContextError("模块索引包含无效描述符。")
            path, digest = descriptor.get("path"), descriptor.get("sha256")
            self._validate_relative_path(path, allow_reader=True)
            if not isinstance(digest, str) or len(digest) != 64:
                raise ModuleContextError("模块索引包含无效哈希：" + str(path))
            self.descriptors[path] = digest
        required = {"world.json", "chapter-index.json", "beat-index.json", "character-index.json", "location-index.json", "item-index.json"}
        if not required.issubset(self.descriptors):
            raise ModuleContextError("模块索引缺少生成正文所需的基础模块。")
        self.runtime_index = self._read_runtime_index()

    @classmethod
    def for_package(cls, package_path: Path, package: Dict[str, Any]) -> Optional["ModuleContextResolver"]:
        module_directory = package_path.parent / "modules"
        if not module_directory.is_dir():
            return None
        return cls(module_directory, package)

    @staticmethod
    def _validate_relative_path(path: Any, allow_reader: bool = False) -> None:
        if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ModuleContextError("模块索引包含不安全路径。")
        if not allow_reader and path.startswith("reader/"):
            raise ModuleContextError("正文上下文索引不得登记 reader/ 原著模块。")

    def _read_index(self) -> Dict[str, Any]:
        path = self.module_directory / "package-index.json"
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModuleContextError("无法读取模块索引：" + str(error)) from error
        if not isinstance(index, dict) or index.get("schemaVersion") != "story-package-module-index/0.1":
            raise ModuleContextError("模块索引 schemaVersion 不匹配。")
        if index.get("package") != self.package_ref:
            raise ModuleContextError("模块索引未绑定当前 StoryPackage 版本。")
        expected_digest = self.module_index_sha256
        if not isinstance(expected_digest, str) or _canonical_sha256(index) != expected_digest:
            raise ModuleContextError("模块索引哈希与当前 StoryPackage 不匹配。")
        return index

    def _read(self, relative_path: str) -> Dict[str, Any]:
        self._validate_relative_path(relative_path)
        expected = self.descriptors.get(relative_path)
        if expected is None:
            raise ModuleContextError("模块索引未声明上下文模块：" + relative_path)
        if relative_path in self._cache:
            self.last_resolved_paths.append(relative_path)
            return self._cache[relative_path]
        try:
            content = json.loads((self.module_directory / relative_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModuleContextError("无法读取上下文模块 " + relative_path + "：" + str(error)) from error
        if not isinstance(content, dict) or content.get("package") != self.package_ref:
            raise ModuleContextError("上下文模块未绑定当前 StoryPackage 版本：" + relative_path)
        if _canonical_sha256(content) != expected:
            raise ModuleContextError("上下文模块哈希不匹配：" + relative_path)
        self._cache[relative_path] = content
        self.loaded_paths.append(relative_path)
        self.last_resolved_paths.append(relative_path)
        return content

    def _read_runtime_index(self) -> Dict[str, Any]:
        runtime_path = self.index.get("indexes", {}).get("runtime")
        if runtime_path is None:
            # Packages built before runtime-index.json remain readable. They
            # use the same fixed layout, but do not advertise its entrypoint.
            return {}
        if runtime_path != "runtime-index.json":
            raise ModuleContextError("模块索引的运行时索引入口无效。")
        runtime = self._read(runtime_path)
        if runtime.get("schemaVersion") not in (
            "story-package-runtime-index/0.1", "story-package-runtime-index/0.2", "story-package-runtime-index/0.3", "story-package-runtime-index/0.4", "story-package-runtime-index/0.5",
        ):
            raise ModuleContextError("运行时索引 schemaVersion 不匹配。")
        expected_core = {
            "world": "world.json", "state": "state-schema.json", "story": "main-story-graph.json",
            "beats": "beat-index.json", "chapters": "chapter-index.json",
        }
        if runtime.get("schemaVersion") in ("story-package-runtime-index/0.3", "story-package-runtime-index/0.4", "story-package-runtime-index/0.5"):
            expected_core["nodes"] = "node-index.json"
        if runtime.get("schemaVersion") in ("story-package-runtime-index/0.4", "story-package-runtime-index/0.5"):
            expected_core.update({
                "arcModel": "arc-model.json", "arcs": "arc-index.json",
                "entryModel": "entry-model.json", "entries": "entry-index.json",
            })
        expected_entities = {
            "characters": "character-index.json", "locations": "location-index.json",
            "items": "item-index.json", "relationships": "relationship-index.json",
        }
        if runtime.get("coreModules") != expected_core or runtime.get("entityIndexes") != expected_entities:
            raise ModuleContextError("运行时索引声明与模块布局不匹配。")
        return runtime

    def _index_entries(self, name: str, collection: str) -> Dict[str, Dict[str, Any]]:
        index = self._read(self.index["indexes"].get(name, ""))
        entries = index.get(collection)
        if not isinstance(entries, list):
            raise ModuleContextError("模块实体索引无效：" + name)
        return {entry["id"]: entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)}

    @staticmethod
    def _location_ids(state: Dict[str, Any], selected: Dict[str, Any]) -> Set[str]:
        values: Set[str] = set()
        for mapping in (state, selected.get("statePatch", {})):
            if not isinstance(mapping, dict):
                continue
            for key, value in mapping.items():
                if isinstance(key, str) and key.endswith("LocationId") and isinstance(value, str):
                    values.add(value)
        return values

    def _current_chapter(self, state: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
        chapter_index = self._read(self.index["indexes"].get("chapters", ""))
        chapters = chapter_index.get("chapters")
        if not isinstance(chapters, list):
            raise ModuleContextError("章节索引无效。")
        if chapter_index.get("schemaVersion") == "story-package-chapter-index/0.2":
            catalog = {chapter.get("id"): chapter for chapter in chapters if isinstance(chapter, dict) and isinstance(chapter.get("id"), str)}
            progress = state.get("sourceProgress")
            chapter_id = next(
                (entry.get("chapterId") for entry in chapter_index.get("progressLocator", [])
                 if isinstance(entry, dict) and entry.get("sourceProgress") == progress),
                contract.get("entrySourceChapterId"),
            )
            chapter = catalog.get(chapter_id)
            if not isinstance(chapter, dict) or not isinstance(chapter.get("path"), str):
                raise ModuleContextError("章节总清单无法定位当前剧情章节。")
            segment = self._read(chapter["path"])
            current = segment.get("chapter")
            if not isinstance(current, dict) or current.get("id") != chapter_id:
                raise ModuleContextError("章节分段索引无效。")
            return current
        progress = state.get("sourceProgress")
        if isinstance(progress, str):
            for chapter in chapters:
                if isinstance(chapter, dict) and progress in chapter.get("sourceProgressValues", []):
                    return chapter
        entry_id = contract.get("entrySourceChapterId")
        for chapter in chapters:
            if isinstance(chapter, dict) and chapter.get("id") == entry_id:
                return chapter
        raise ModuleContextError("模块索引无法定位当前剧情章节。")

    def _current_beat(self, state: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
        beat_index = self._read(self.index["indexes"].get("beats", ""))
        if beat_index.get("schemaVersion") == "story-package-beat-index/0.2":
            chapter = self._current_chapter(state, contract)
            chapter_id = chapter.get("id")
            segment_path = next(
                (entry.get("path") for entry in beat_index.get("segments", [])
                 if isinstance(entry, dict) and entry.get("chapterId") == chapter_id and isinstance(entry.get("path"), str)),
                None,
            )
            if not isinstance(segment_path, str):
                raise ModuleContextError("剧情节点总清单无法定位当前章节分段。")
            entries = self._read(segment_path).get("beats")
            if not isinstance(entries, list):
                raise ModuleContextError("剧情节点分段索引无效。")
            progress = state.get("sourceProgress")
            current = next((beat for beat in entries if isinstance(beat, dict) and beat.get("sourceProgress") == progress), None)
            if current is None:
                current = next((beat for beat in entries if isinstance(beat, dict)), None)
            if not isinstance(current, dict):
                raise ModuleContextError("剧情节点分段索引无法定位当前节点。")
            return current
        beats = beat_index.get("beats")
        if not isinstance(beats, list):
            raise ModuleContextError("剧情节点索引无效。")
        progress = state.get("sourceProgress")
        if isinstance(progress, str):
            for beat in beats:
                if isinstance(beat, dict) and beat.get("sourceProgress") == progress:
                    return beat
        entry_chapter = contract.get("entrySourceChapterId")
        for beat in beats:
            if isinstance(beat, dict) and beat.get("chapterId") == entry_chapter:
                return beat
        raise ModuleContextError("模块索引无法定位当前剧情节点。")

    def _load_entities(self, entries: Dict[str, Dict[str, Any]], identifiers: Iterable[str], key: str) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for identifier in sorted(set(identifiers)):
            entry = entries.get(identifier)
            if entry is None or not isinstance(entry.get("path"), str):
                continue
            entity = self._read(entry["path"]).get(key)
            if isinstance(entity, dict):
                result.append(entity)
        return result

    def _beat_by_id(self, beat_id: str) -> Optional[Dict[str, Any]]:
        beat_index = self._read(self.index["indexes"].get("beats", ""))
        if beat_index.get("schemaVersion") != "story-package-beat-index/0.2":
            return next((beat for beat in beat_index.get("beats", []) if isinstance(beat, dict) and beat.get("id") == beat_id), None)
        locator = next((entry for entry in beat_index.get("locators", []) if isinstance(entry, dict) and entry.get("id") == beat_id), None)
        if not isinstance(locator, dict):
            return None
        segment_path = next(
            (entry.get("path") for entry in beat_index.get("segments", [])
             if isinstance(entry, dict) and entry.get("chapterId") == locator.get("chapterId") and isinstance(entry.get("path"), str)),
            None,
        )
        if not isinstance(segment_path, str):
            return None
        entries = self._read(segment_path).get("beats")
        return next((beat for beat in entries if isinstance(beat, dict) and beat.get("id") == beat_id), None) if isinstance(entries, list) else None

    @staticmethod
    def _character_detail(character: Dict[str, Any], current_line: int) -> str:
        evidence = character.get("sourceDescriptionEvidence", [])
        eligible = [
            entry for entry in evidence
            if isinstance(entry, dict)
            and isinstance(entry.get("text"), str)
            and isinstance(entry.get("lineRange"), dict)
            and isinstance(entry["lineRange"].get("end"), int)
            and entry["lineRange"]["end"] <= current_line
        ]
        if eligible:
            return eligible[-1]["text"]
        return "该角色在当前已确认剧情中尚未提供更多可用细节。"

    @staticmethod
    def _identity_evidence(characters: List[Dict[str, Any]], cues: List[Dict[str, Any]], current_line: int) -> List[Dict[str, Any]]:
        result = []
        for character in characters:
            name = character.get("name")
            if not isinstance(name, str):
                continue
            for entry in character.get("sourceDescriptionEvidence", []):
                end = entry.get("lineRange", {}).get("end")
                if (not isinstance(end, int) or end > current_line
                        or not re.search(r"(?:写着|名叫|名为|叫作|那是|他是|她是)[：:，,\s]*" + re.escape(name), entry.get("text", ""))):
                    continue
                # Use the already loaded source paragraph when it contains
                # the reveal, preserving the unnamed description it resolves.
                source = next((cue for cue in cues
                               if cue.get("lineRange", {}).get("start", end + 1) <= end
                               <= cue.get("lineRange", {}).get("end", -1) <= current_line
                               and entry["text"] in cue.get("text", "")), entry)
                identity = {"name": name, "text": source["text"], "lineRange": copy.deepcopy(source["lineRange"])}
                if source is entry:
                    # A bounded cue may omit the reveal at the end of the
                    # same source paragraph. Keep the two excerpts separate.
                    cue = next((cue for cue in cues
                                if cue.get("lineRange") == entry["lineRange"]
                                and isinstance(cue.get("text"), str)), None)
                    if cue and cue["text"] != entry["text"]:
                        identity["context"] = cue["text"]
                result.append(identity)
                break
        return result

    @staticmethod
    def _dialogue_context(cues: List[Dict[str, Any]], previous_line: int, names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Keep adjacent source paragraphs and narrowly annotate pronoun speakers."""
        by_id = {cue.get("evidenceParagraphId"): cue for cue in cues}
        result = []
        for cue in cues:
            if not re.search(r"[他她](?:说|问|答|回答|低声)", cue.get("text", "")) or "“" not in cue["text"]:
                continue
            match = re.fullmatch(r"(.*?)(\d+)", str(cue.get("evidenceParagraphId", "")))
            if not match:
                continue
            neighbors = []
            for offset in (-1, 0, 1):
                identifier = match[1] + str(int(match[2]) + offset).zfill(len(match[2]))
                neighbor = by_id.get(identifier)
                if neighbor:
                    neighbors.append({**copy.deepcopy(neighbor),
                                      "phase": "prior" if neighbor["lineRange"]["end"] <= previous_line else "current"})
            if len(neighbors) > 1:
                passage = {"dialogueParagraphId": cue["evidenceParagraphId"], "paragraphs": neighbors}
                previous_id = match[1] + str(int(match[2]) - 1).zfill(len(match[2]))
                previous = by_id.get(previous_id)
                narration = re.sub(r'“[^”]*”|"[^"]*"', '', cue["text"])
                tag = re.search(r'[他她]说', narration)
                if previous and names and tag and not any(name in narration[:tag.start()] for name in names):
                    # Only an immediately adjacent, explicit single-subject
                    # sentence; never pick a name merely mentioned as object.
                    sentences = re.findall(r'[^。！？!?]+[。！？!?]?', previous["text"])
                    tail = sentences[-1].strip() if sentences else ""
                    subjects = [name for name in names if tail.startswith(name)]
                    if (len(subjects) == 1 and not any(mark in tail for mark in '“”"？?')
                            and re.match(r"(?:忽然|终于|缓缓|轻轻|慢慢|又|仍然|仍|已经|没有|没|正|一直|只)*"
                                         r"(?:向(?:前|后|左|右)(?:退|走)|退|走|站|坐|抬|低|点头|摇头|开口|沉默|闭口|转身|看|停|攥|收)",
                                         tail[len(subjects[0]):])
                            and not any(name in tail for name in names if name != subjects[0])):
                        passage["speakerName"] = subjects[0]
                        passage["speakerBasis"] = {"kind": "adjacent_named_subject", "text": tail,
                                                   "evidenceParagraphId": previous_id}
                result.append(passage)
        return result

    @staticmethod
    def _branch_excerpt(narrative: str, limit: int = 1200) -> str:
        # Never start a carried-over passage halfway through a quotation.
        paragraphs = re.split(r"\n\s*\n", narrative.strip())
        result = ""
        for paragraph in reversed(paragraphs):
            candidate = paragraph + ("\n\n" + result if result else "")
            if len(candidate) > limit:
                break
            result = candidate
        return result

    def resolve(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        self.last_resolved_paths = []
        contract = context.get("contract", {})
        if not isinstance(contract, dict):
            contract = {}
        if contract.get("openingContext") and context.get("parent", {}).get("kind") == "source_entry":
            return self._opening_context(contract, context["parent"], state)
        current = self._current_beat(state, contract)
        if not isinstance(current.get("path"), str):
            raise ModuleContextError("当前剧情节点缺少模块路径。")
        current_module = self._read(current["path"])
        current_beat = current_module.get("beat")
        if not isinstance(current_beat, dict):
            raise ModuleContextError("当前剧情节点模块无效。")
        # A divergent turn may use the current beat and two indexed predecessors
        # only. It can never load a following beat or a whole source chapter.
        related_modules = [current_module]
        for beat_id in current.get("previousBeatIds", []):
            previous = self._beat_by_id(beat_id) if isinstance(beat_id, str) else None
            if isinstance(previous, dict) and isinstance(previous.get("path"), str):
                related_modules.append(self._read(previous["path"]))

        character_ids: Set[str] = set()
        location_ids = self._location_ids(state, selected)
        item_ids: Set[str] = set()
        for module in related_modules:
            beat = module.get("beat", {})
            if not isinstance(beat, dict):
                continue
            refs = beat.get("contextRefs", {})
            if not isinstance(refs, dict):
                continue
            character_ids.update(item for item in refs.get("characterIds", []) if isinstance(item, str))
            location_ids.update(item for item in refs.get("locationIds", []) if isinstance(item, str))
            item_ids.update(item for item in refs.get("itemIds", []) if isinstance(item, str))
        character_index = self._index_entries("characters", "characters")
        selected_text = "\n".join(
            value for value in (selected.get("title"), selected.get("summary")) if isinstance(value, str)
        )
        character_ids.update(
            character_id for character_id, entry in character_index.items()
            if isinstance(entry.get("name"), str) and entry["name"] in selected_text
        )
        persona = contract.get("persona", {})
        if isinstance(persona, dict) and isinstance(persona.get("sourceCharacterId"), str):
            character_ids.add(persona["sourceCharacterId"])
        world_module = self._read("world.json")
        focal_id = world_module.get("world", {}).get("narrativeGuidelines", {}).get("focalCharacterId")
        if isinstance(focal_id, str):
            character_ids.add(focal_id)

        characters = self._load_entities(character_index, character_ids, "character")
        locations = self._load_entities(self._index_entries("locations", "locations"), location_ids, "location")
        items = self._load_entities(self._index_entries("items", "items"), item_ids, "item")
        allowed_timeline = set(contract.get("canonicalTimelineRefs", []))
        timeline = [
            item["description"] for module in related_modules for item in module.get("timeline", [])
            if isinstance(item, dict) and item.get("id") in allowed_timeline and isinstance(item.get("description"), str)
        ]
        branch_history: List[str] = []
        for node in context.get("lineage", [])[-2:]:
            summary = node.get("summary")
            if not summary:
                outcome = node.get("readerOutcome") or {}
                action = outcome.get("action") or {}
                summary = action.get("summary") if isinstance(action, dict) else None
            if isinstance(summary, str) and summary.strip():
                branch_history.append(summary.strip()[:700])
        branch_history = list(dict.fromkeys(branch_history))
        current_summary = str(current_beat.get("summary", ""))
        current_line = current_beat.get("sourceEvidence", {}).get("lineRange", {}).get("end", 0)
        if not isinstance(current_line, int):
            current_line = 0
        relevant_facts: List[Dict[str, Any]] = []
        seen_fact_ids: Set[str] = set()
        for module in related_modules:
            for fact in module.get("facts", []):
                if not isinstance(fact, dict) or not isinstance(fact.get("id"), str):
                    continue
                evidence_end = fact.get("lineRange", {}).get("end")
                if isinstance(evidence_end, int) and evidence_end > current_line:
                    continue
                if fact["id"] not in seen_fact_ids:
                    seen_fact_ids.add(fact["id"])
                    relevant_facts.append(copy.deepcopy(fact))
        prompt_world = copy.deepcopy(world_module["world"])
        # Facts with no source chapter are package-wide fallback constraints.
        prompt_world["immutableFacts"] = relevant_facts or [
            copy.deepcopy(fact) for fact in prompt_world.get("immutableFacts", [])
            if not isinstance(fact, dict) or "sourceChapterId" not in fact
        ]
        previous_line = 0
        parent_state = context.get("parent", {}).get("branchState")
        if isinstance(parent_state, dict):
            parent_beat = self._current_beat(parent_state, contract)
            parent_module = self._read(parent_beat["path"])
            previous_line = parent_module.get("beat", {}).get("sourceEvidence", {}).get("lineRange", {}).get("end", 0)
        if not isinstance(previous_line, int):
            previous_line = 0
        current_cues, previous_cues = [], []
        for cue in current_beat.get("narrativeBrief", []):
            cue_end = cue.get("lineRange", {}).get("end")
            target = previous_cues if isinstance(cue_end, int) and cue_end <= previous_line else current_cues
            target.append(copy.deepcopy(cue))
        return {
            "world": prompt_world,
            "currentChapter": copy.deepcopy(current_module["chapter"]),
            "currentBeat": {"id": current_beat["id"], "summary": current_summary},
            "narrativeBrief": current_cues,
            "priorNarrativeBrief": previous_cues,
            "previousSourceLine": previous_line,
            "actionContract": copy.deepcopy(current_beat.get("actionContract", {})),
            "modulePaths": sorted(set(self.last_resolved_paths)),
            "previousBeatSummaries": [
                module["beat"]["summary"] for module in related_modules[1:]
                if isinstance(module.get("beat"), dict) and isinstance(module["beat"].get("summary"), str)
            ],
            "characters": characters,
            "locations": locations,
            "items": items,
            "characterDetails": [
                {"name": item["name"], "detail": self._character_detail(item, current_line)}
                for item in characters if isinstance(item.get("name"), str)
            ],
            "characterIdentityEvidence": self._identity_evidence(characters, previous_cues + current_cues, current_line),
            "sourceDialogueContext": self._dialogue_context(previous_cues + current_cues, previous_line,
                                                            [item["name"] for item in characters if isinstance(item.get("name"), str)]),
            "continuityText": "\n".join([
                "当前章节摘要：" + (current_summary or "无"),
                "入口允许的前史摘要：" + ("；".join(timeline) if timeline else "无"),
                "已确认分支承接：" + ("\n\n".join(branch_history) if branch_history else "无"),
                "当前相关物品（不代表已取得或可用）：" + ("；".join(item["name"] for item in items) if items else "无"),
            ]),
        }

    def _opening_context(self, contract, parent, state):
        """The first turn must not inherit whole-book cards or another POV's history."""
        entry = self._read("entries/" + contract["entryPointId"] + ".json")["entryPoint"]
        opening = entry["openingContext"]
        if opening != contract["openingContext"]:
            raise ModuleContextError("会话开局知识与官方入口不匹配")
        world = copy.deepcopy(self._read("world.json")["world"])
        world["immutableFacts"] = [f for f in world.get("immutableFacts", []) if "sourceChapterId" not in f]
        player = contract["persona"]
        place_ids = self._location_ids(state, {})
        locations = self._load_entities(self._index_entries("locations", "locations"), place_ids, "location")
        item_ids = set(state.get("itemOwnerCharacterIds", {})) | set(state.get("itemLocationIds", {}))
        items = self._load_entities(self._index_entries("items", "items"), item_ids, "item")
        return {
            "world": world, "currentChapter": {"id": entry["sourceChapterId"], "title": entry["chapterTitle"]},
            "currentBeat": {"id": entry["beatId"], "summary": entry["openingSummary"]},
            "narrativeBrief": [], "priorNarrativeBrief": [], "previousBeatSummaries": [],
            "previousSourceLine": opening["sourceCutoffLine"], "actionContract": {},
            "openingContext": copy.deepcopy(opening), "continuityContract": copy.deepcopy(contract["continuityContract"]),
            "characters": [{"id": player["sourceCharacterId"], "name": player["name"], "description": opening["identity"]}],
            "characterDetails": [{"name": player["name"], "detail": opening["identity"]}],
            "characterIdentityEvidence": [], "sourceDialogueContext": [], "locations": locations, "items": items,
            "modulePaths": sorted(set(self.last_resolved_paths)),
            "continuityText": "\n".join(["角色知识边界：" + json.dumps(opening, ensure_ascii=False),
                                         "已发生开场：" + parent["narrativeText"],
                                         "当前分支状态：" + json.dumps(state, ensure_ascii=False)]),
        }
