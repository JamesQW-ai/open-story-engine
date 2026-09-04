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
    state.setdefault("derivedCharacterReveals", [])
    return state


def apply_branch_patch(package: Dict[str, Any], current: Dict[str, Any], patch: Dict[str, Any], source_node_ref: str) -> Dict[str, Any]:
    if not patch:
        raise ValueError("剧情方向必须声明至少一个状态变化")
    unsupported = set(patch) - set(current) - {"derivedAdditions"}
    if unsupported:
        raise ValueError("剧情方向包含不受支持的状态字段: " + "、".join(sorted(unsupported)))
    next_state = copy.deepcopy(current)
    additions = patch.get("derivedAdditions", {})
    for key, value in patch.items():
        if key != "derivedAdditions" and value is not None:
            next_state[key] = value
    for collection, label in (("locations", "地点"), ("characters", "人物")):
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
    excerpts: List[str] = []
    for node in lineage[-3:]:
        narrative = node.get("narrativeText", "").strip()
        if narrative:
            excerpts.append(narrative[-3600:])
    return "\n".join([
        "已确认道具归属：" + "；".join(facts),
        "来源片段（只可延续，不能改写其中已出现的人物、地点、道具归属或因果）：",
        "\n\n".join(excerpts),
    ])


def narration_outside_dialogue(text: str) -> str:
    return re.sub(r"[“\"][^”\"]*[”\"]", "", text)


def guard_narrative(text: str, state: Dict[str, Any], character_details: List[Dict[str, str]]) -> None:
    normalized = re.sub(r"\s+", "", text)
    narration = re.sub(r"\s+", "", narration_outside_dialogue(text))
    if re.search(r"(?:^|[。！？；])你(?:[在正又也还将把向从的]|[\u4e00-\u9fff])", narration):
        raise ValueError("剧情正文视角错误：叙事必须使用第三人称许川，不能把玩家写成“你”。")
    if state["trainStatus"] == "pending_release" and any(word in normalized for word in ("列车已经离站", "末班列车离开", "列车驶离", "列车发车")):
        raise ValueError("剧情正文与已确认状态矛盾：列车仍在等待放行，正文不能描写列车已经进站、离站或发车。")
    if state["signalRoomStatus"] == "locked" and (
        "唐栖走出信号室" in normalized
        or re.search(r"唐栖.{0,24}(?:从|经由).{0,16}(?:信号室|通风口).{0,12}(?:爬出|钻出|走出|逃出|离开)", normalized)
        or re.search(r"(?:设备间.{0,420}唐栖|唐栖.{0,420}设备间)", normalized)
    ):
        raise ValueError("剧情正文与已确认状态矛盾：信号室仍锁闭，唐栖不能自行离开或出现在设备间。")
    if state["tangStatus"] != "rescued" and any(word in normalized for word in ("唐栖已经获救", "唐栖回到候车厅")):
        raise ValueError("剧情正文与已确认状态矛盾：唐栖尚未获救。")
    if state["evidenceStatus"] == "unsecured" and any(word in normalized for word in ("证据已经保全", "录音已经取得", "录音笔", "证据有了", "拿到证据", "录下来了")):
        raise ValueError("剧情正文与已确认状态矛盾：证据尚未取得。")
    # Reject only direct known-detail inversions. New appearances stay allowed;
    # identity may be intentionally withheld until later chapters.
    for detail in character_details:
        if "深蓝" in detail["detail"] and (detail["name"] + "穿着浅色") in normalized:
            raise ValueError(f"剧情正文与已确认人物细节矛盾：{detail['name']} 的衣着描述不一致。")


def guard_source_character_names(text: str, package: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Keep untracked named characters out of source-package continuations."""
    if state["storyScope"] != "source":
        return
    known = {character["name"] for character in package["characters"]}
    generic_suffixes = ("人", "工", "员", "者", "客", "长", "们")
    # These are pronouns, adverbs, prepositions, or structural particles, not
    # plausible starts of a Chinese personal name. Keeping them out prevents
    # phrases such as "他低头" and "又看了看" from becoming fake characters.
    non_name_prefixes = (
        "他", "她", "它", "这", "那", "其", "谁", "某", "一", "每", "各", "什么",
        "又", "再", "还", "已", "正", "就", "才", "也", "都", "仍", "不", "没",
        "被", "把", "将", "从", "在", "向", "对", "让", "使",
    )
    pattern = re.compile(r"(?:^|[。！？\n，、和与])([\u4e00-\u9fff]{2,3})(?=(?:说|问|答|喊|看|点|走|站|蹲|跟|回|抬|低|握|把|让|扶|托|转|停|沉默|开口|脸色|目光|声音|抱紧|快步))")
    for match in pattern.finditer(text):
        candidate = match.group(1)
        if candidate in known or any(candidate.startswith(name) for name in known):
            continue
        if not candidate.startswith(non_name_prefixes) and not candidate.endswith(generic_suffixes):
            raise ValueError("剧情正文引入了未登记的新人物姓名: " + candidate)


def normalize_narrative_text(text: str) -> str:
    """Recover paragraph breaks double-escaped by incompatible model gateways."""
    return text.replace("\\\\r\\\\n", "\n").replace("\\\\n", "\n").replace("\\\\r", "\n")


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
        parent = context["parent"]
        title = selected["title"]
        state = resolved_state
        location = location_name(context["package"], state)
        narrative = (
            f"{location}里的动静没有因为许川作出选择而停下来。{title}并非一句口号；他先看清眼前仍能利用的线索，再把自己的决定告诉身边的人。"
            f"\n\n姜序没有立刻表示赞同。他把目光投向潮湿的走廊尽头，提醒许川每一次绕路都会改变水位、证据和列车放行压力之间的先后。许川没有把这些提醒当成推脱，只在短暂的沉默后确认，眼下最重要的是让局面向前推进，而不是假装已经掌握了全部答案。"
            f"\n\n行动开始后，站内传来一阵被雨声压低的广播杂音。唐栖的处境、陈砚的反应和那份尚未完全厘清的记录仍互相牵制。许川意识到下一步必须落在一个具体问题上：谁能提供帮助、哪一件东西还能使用，或者该如何在压力再次抬头前保住已经得到的进展。"
        )
        next_directions = self._next(context, selected, resolved_state)
        result = plan_result(context, selected, narrative, title, next_directions, "medium")
        if stream:
            stream(narrative)
        return result, None

    def _next(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
        package = context["package"]
        beat_id = selected.get("canonicalBeatId")
        # Divergent source choices deliberately have no canonical beat. Their
        # follow-up choices still come from declared StoryPackage beats, never
        # from an unconstrained mock invention.
        divergent_targets = {
            "direction_rescue_first": "beat_tunnel_without_proof",
            "direction_lower_water_without_proof": "beat_valve_without_proof",
            "direction_return_for_records": "beat_evidence_secured",
        }
        beat_id = beat_id or divergent_targets.get(selected["id"])
        beat = next((item for item in package["story"]["narrativeGraph"]["beats"] if item["id"] == beat_id), None)
        if beat:
            return [branch_direction(direction) for direction in beat["nextDirections"]]
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
    def __init__(self, gateway: OpenAICompatibleGateway) -> None:
        self.gateway = gateway

    def plan(self, context: Dict[str, Any], selected: Dict[str, Any], resolved_state: Dict[str, Any], stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None, repair: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        raw_responses: List[str] = []
        observations: List[Dict[str, Any]] = []
        repair_instruction = repair
        last_error = "LLM Planner 未知错误"
        for attempt in range(1, 3):
            observer = NarrativeFieldStream(stream)
            prompt = self._prompt(context, selected, resolved_state, repair_instruction)
            try:
                completion = self.gateway.complete_json(
                    [{"role": "system", "content": "你是中文互动小说叙事规划器。只输出一个 JSON 对象。"}, {"role": "user", "content": prompt}],
                    observer.feed if stream else None,
                    stream_reset,
                )
                raw_responses.append(completion.raw_response)
                observations.extend(completion.observations)
                result = parse_json_content(completion.content)
                required = ("narrativeText", "summary", "factDeltas", "openThreads", "nextDirections", "storyArc", "planning")
                missing = [field for field in required if field not in result]
                if missing or not isinstance(result.get("narrativeText"), str) or not result["narrativeText"].strip():
                    raise LlmError("LLM 剧情结果缺少必要字段: " + "、".join(missing), "model_output_rejected")
                result["narrativeText"] = normalize_narrative_text(result["narrativeText"])
                result["storyArc"] = validate_story_arc(result["storyArc"])
                result["nextDirections"] = [validate_direction(item) for item in result["nextDirections"]]
                guard_narrative(result["narrativeText"], resolved_state, context["characterDetails"])
                guard_source_character_names(result["narrativeText"], context["package"], resolved_state)
                if not selected.get("rejoinTargetId"):
                    source_node_ref = resolve_source_node(context["package"], context["parent"], resolved_state, selected)
                    assert_published_directions(context["package"], source_node_ref, resolved_state, result["nextDirections"])
                if completion.used_transport_fallback and stream:
                    # A normal JSON response has no SSE fragments to display. It
                    # remains uncommitted until the service validates everything.
                    stream(result["narrativeText"])
                return result, {"operation": "branch_planner", "model": self.gateway.model, "promptVersion": "python-v0.2", "requestSummary": selected["title"], "rawResponse": "\n\n--- retry ---\n\n".join(raw_responses), "callObservations": observations}
            except LlmError as error:
                last_error = str(error)
                observations.append({"attempt": attempt, "outcome": "failed", "failureKind": error.code})
            except ValueError as error:
                last_error = str(error)
                observations.append({"attempt": attempt, "outcome": "failed", "failureKind": "model_output_rejected"})
            if attempt == 1:
                if stream_reset:
                    stream_reset("validation_retry")
                repair_instruction = last_error
        error = LlmError("LLM Planner 未生成可用剧情：" + last_error, "model_output_rejected")
        error.audit = {"operation": "branch_planner", "model": self.gateway.model, "promptVersion": "python-v0.3", "requestSummary": selected["title"], "rawResponse": "\n\n--- retry ---\n\n".join(raw_responses), "error": last_error, "callObservations": observations}
        raise error

    def _prompt(self, context: Dict[str, Any], selected: Dict[str, Any], state: Dict[str, Any], repair: Optional[str]) -> str:
        fact_text = "\n".join("- " + fact["text"] for fact in context["package"]["world"]["immutableFacts"])
        character_text = "\n".join("- " + item["name"] + "：" + item["detail"] for item in context["characterDetails"]) or "- 暂无额外已确认外观细节"
        continuity_text = source_continuity_context(context["package"], context.get("lineage", [context["parent"]]), state)
        schema = '{"narrativeText":"", "summary":"", "factDeltas":[{"id":"","source":"derived","summary":""}], "openThreads":[""], "nextDirections":[{"id":"","title":"","summary":"","statePatch":{}}], "storyArc":{"activeGoal":"","currentPhase":"","goalDisposition":"continued","chapter":{"title":"","status":"continuing"}}, "planning":{"citations":[{"kind":"branch_node","ref":"' + context["parent"]["id"] + '","rationale":""}],"confidence":"medium","stateChangeProposals":[]}}'
        return f"""根据已确认状态继续中文互动小说。不能改写 StoryPackage，不能凭空让未获得的证据、救援或列车状态发生。

玩家本回合选择：{selected['title']}。{selected['summary']}
已确认状态：{json.dumps(state, ensure_ascii=False)}
原著不可变事实：\n{fact_text}
已确认人物细节（新增人物可以暂不透露身份）：\n{character_text}
上一节点摘要：{context['parent']['summary']}
受控连续性上下文：\n{continuity_text}

写作要求：以第三人称限知、连贯的场景推进来写，主角一律称“许川”或“他”，绝不可使用“你”指代主角；把选择造成的阻碍、人物反应和新的具体问题写出来。通常一章为约 1800-2800 个中文字符，但可因有效悬念、场景转换或章节节奏自然变短或变长；不得为凑字数重复，不设机械字数门槛。不要替玩家完成后续选择。公开方向必须是下一步可执行的单一行动，statePatch 必须只写真实可推进的状态变化，且每个状态字段必须使用已确认状态中的枚举值。源故事范围内只能使用已确认角色姓名；需要出现未知角色时只可写“陌生人”“来人”等匿名称谓，不可命名、不可新增身份或事实。不得改写来源片段中已有的道具归属、人物位置或已发生的因果，除非本回合 statePatch 明确推进了该事实。信号室锁闭且唐栖未获救时，唐栖只能在锁闭信号室内与外部互动，不能出现在设备间、通风口外或其他新地点；证据未取得时，不能写唐栖或任何人已拿到录音笔、录音或可用证据。正文使用真实段落换行，不能输出字面量 \\n 或 \\r\\n。`storyArc.chapter.title` 必须给本回合一个简洁中文章节名。
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
        prompt = f"""审阅下列互动小说草稿。你不是状态权威，不能提议新增事实；只判断是否应重写。检查：因果连贯、人物动机、细节延续、场景感、是否把未完成的玩家选择写成已完成、是否存在有意义的下一步。允许新人物暂不揭示身份。\n已确认状态：{json.dumps(state, ensure_ascii=False)}\n上文摘要：{context['parent']['summary']}\n草稿：{result['narrativeText']}\n只输出 {{\"decision\":\"accept\"或\"revise\",\"issues\":[\"不超过三条具体问题\"]}}。"""
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

    def continue_direction(self, session_id: str, parent_id: str, direction_id: str, player_direction: Optional[str] = None, stream: Optional[Callable[[str], None]] = None, stream_reset: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
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
                result, audit = self.planner.plan(context, selected, resolved, stream, stream_reset)
            except LlmError as error:
                failure_audit = getattr(error, "audit", None)
                if failure_audit:
                    self.store.save_audit(session_id, failure_audit)
                raise
            relation = "rejoined" if rejoin else "diverged"
            guard_narrative(result["narrativeText"], resolved, context["characterDetails"])
            review = self.reviewer.review(context, result, resolved)
            if review.get("decision") == "revise":
                if stream_reset:
                    stream_reset("validation_retry")
                result, retry_audit = self.planner.plan(context, selected, resolved, stream, stream_reset, "；".join(str(item) for item in review.get("issues", [])[:3]))
                audit = retry_audit or audit
                guard_narrative(result["narrativeText"], resolved, context["characterDetails"])
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
        node = self.continue_direction(session_id, parent_id, evaluation["directionId"], player_direction, stream, stream_reset)
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
        entry = {"id": "branch_" + str(uuid.uuid4()), "kind": "derived_entry", "parentId": parent_id, "sourceNodeRef": parent.get("sourceNodeRef"), "branchState": state, "selectedDirectionId": "direction_begin_derivative", "playerDirection": goal.strip(), "narrativeText": "临潮站的灯还亮着，但这一夜已经收束。许川和唐栖没有替以后下结论；玩家写下的目标会成为独立衍生故事的起点。新的地点、人物与身份揭示只会在后续回合逐步进入，不会改写原始故事包。", "summary": "玩家从已完成分支创建独立衍生故事包。", "factDeltas": [{"id": "fact_derivative_entry", "source": "user", "summary": "衍生目标：" + goal.strip()}], "openThreads": ["衍生故事的第一步"], "nextDirections": [derived_direction("direction_derivative_start", "开始衍生篇", "以设定的后续目标展开第一章。", {"derivativeStage": "active", "derivedTurn": 1})], "canonicalRelation": "diverged", "storyArc": {"activeGoal": goal.strip(), "currentPhase": "确定衍生故事的第一步", "goalDisposition": "started", "chapter": {"title": "衍生篇·起点", "status": "continuing"}}, "planning": {"citations": [{"kind": "branch_node", "ref": parent_id, "rationale": "衍生故事继承分叉结果。"}], "confidence": "high", "stateChangeProposals": []}, "createdAt": timestamp()}
        package = {"id": "derived_" + str(uuid.uuid4()), "sessionId": session_id, "sourcePackageRef": {"id": self.package["id"], "version": self.package["version"]}, "forkBranchId": parent_id, "title": self.package["metadata"]["title"] + "·衍生篇", "goal": goal.strip(), "createdAt": timestamp(), "revisions": []}
        self.store.save_derived(package)
        return package, self.store.create_derived_entry(session_id, parent_id, entry)
