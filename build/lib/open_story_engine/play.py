"""Deterministic ordinary-play service and narration."""

from __future__ import annotations

import os
import random
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .content import entity_name
from .state import find_ending, matches_condition, resolve_turn
from .storage import SessionStore


ACTION_TERMS = {
    "investigate": ["寻找", "找", "查", "检查", "调查", "查看", "翻", "观察", "搜"],
    "negotiate": ["询问", "问", "交谈", "沟通", "请求", "拜托", "劝", "说服", "告诉", "说明"],
    "risk": ["进入", "进去", "冲", "闯", "绕过", "冒险", "打开", "撬", "拉", "拧", "涉水", "救"],
}
def parse_action(package: Dict[str, Any], player_input: str) -> Dict[str, Any]:
    normalized = player_input.strip()
    focal_name = focal_character_name(package)
    if not normalized:
        return {"kind": "clarification", "message": f"{focal_name}还没有采取行动。请用一句话说明他想做什么。"}
    if len(normalized) > 160:
        return {"kind": "clarification", "message": "这段行动描述过长。请保留眼下最想尝试的一件事。"}
    candidates: List[Tuple[int, Dict[str, Any]]] = []
    enabled = {(item["actionType"], item["targetId"]) for item in package["rules"]["resolutions"]}
    for action_type, target_id in enabled:
        target = entity_name(package, target_id) or target_id
        terms = entity_terms(package, target_id, target)
        target_score = 10 if any(term in normalized for term in terms) else 0
        action_score = 2 if any(term in normalized for term in ACTION_TERMS[action_type]) else 0
        if target_score:
            candidates.append((target_score + action_score, {"actionType": action_type, "targetId": target_id, "approach": normalized}))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates or (len(candidates) > 1 and candidates[0][0] == candidates[1][0]):
        return {"kind": "clarification", "message": f"{focal_name}还无法判断要把注意力放在哪里。请把行动对象说得更明确一些。"}
    return {"kind": "parsed", "intent": candidates[0][1]}


def select_progression(package: Dict[str, Any], state: Dict[str, Any], previous: Optional[Dict[str, Any]], ending_id: Optional[str]) -> Dict[str, Any]:
    graph = package["story"]["narrativeGraph"]
    beats = {beat["id"]: beat for beat in graph["beats"]}
    current = beats.get((previous or {}).get("beatId"), beats[graph["startBeatId"]])
    ending_beat_id = graph.get("endingBeatIds", {}).get(ending_id) if ending_id else None
    transition = None
    beat = beats.get(ending_beat_id) if ending_beat_id else None
    if beat is None:
        transition = next((edge for edge in graph["edges"] if edge["fromBeatId"] == current["id"] and all(matches_condition(state, condition) for condition in edge["when"])), None)
        beat = beats[transition["toBeatId"]] if transition else current
    return {"beat": beat, "transition": transition, "changed": beat["id"] != current["id"], "continuity": {"beatId": beat["id"], "summary": beat["summary"], "openThreads": beat["openThreads"], "currentNodeId": state["currentNodeId"], "currentLocationId": state["currentLocationId"]}}


def narrate(package: Dict[str, Any], intent: Dict[str, Any], resolution: Dict[str, Any], progression: Dict[str, Any]) -> str:
    target = entity_name(package, intent["targetId"]) or intent["targetId"]
    focal_name = focal_character_name(package)
    if resolution["outcome"] in ("blocked", "terminal"):
        return f"{focal_name}的注意力落在{target}上，但{resolution.get('blockReason', '眼前的处境还不允许他这样做')}。{progression['continuity']['summary']}"
    bridge = (progression.get("transition") or {}).get("transitionText", f"{focal_name}朝{target}迈出一步。")
    if progression["changed"]:
        return bridge + progression["beat"]["narrativeAnchor"]
    sentences: List[str] = []
    for effect in resolution["appliedEffects"]:
        if effect.startswith("add:"):
            name = entity_name(package, effect[4:])
            if name:
                sentences.append("他确认拿到了" + name + "。")
        elif effect.startswith("increment:"):
            sentences.append("局势的压力又累积了一层。")
    outcome = "事情勉强有了进展，代价也随着雨水一起逼近。" if resolution["outcome"] == "partial_success" else "这一步没能打开局面。" if resolution["outcome"] == "failure" else ""
    return bridge + outcome + "".join(sentences or ["局面仍需要下一处突破口。"])


def focal_character_name(package: Dict[str, Any]) -> str:
    player_id = package.get("initialState", {}).get("player", {}).get("characterId")
    return entity_name(package, player_id) or "焦点人物"


def entity_terms(package: Dict[str, Any], entity_id: str, fallback: str) -> List[str]:
    for collection in ("items", "locations", "characters"):
        entity = next((item for item in package.get(collection, []) if item.get("id") == entity_id), None)
        if entity is None:
            continue
        aliases = entity.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(item, str) for item in aliases):
            aliases = []
        role = entity.get("role")
        return list(dict.fromkeys([fallback, *aliases, role] if isinstance(role, str) else [fallback, *aliases]))
    return [fallback]


class PlayerTurnService:
    def __init__(self, package: Dict[str, Any], store: SessionStore, fixed_roll: Optional[int] = None) -> None:
        self.package, self.store, self.fixed_roll = package, store, fixed_roll

    def play(self, session_id: str, player_input: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        parsed = parse_action(self.package, player_input)
        if parsed["kind"] != "parsed":
            return parsed
        session = self.store.get_session(session_id)
        roll = self.fixed_roll if self.fixed_roll is not None else random.randint(self.package["rules"]["check"]["randomRange"]["min"], self.package["rules"]["check"]["randomRange"]["max"])
        resolution = resolve_turn(self.package, session["currentState"], parsed["intent"], roll)
        if resolution["outcome"] in ("blocked", "terminal"):
            progression = select_progression(self.package, session["currentState"], self.store.latest_narration(session_id) and self.store.latest_narration(session_id)["continuity"], resolution.get("endingId"))
            return {"kind": "resolved", "event": None, "narration": narrate(self.package, parsed["intent"], resolution, progression), "resolution": resolution, "directions": progression["beat"]["nextDirections"]}
        outcome = self.store.commit_turn(session_id, request_id or str(uuid.uuid4()), session["currentState"], session["stateVersion"], player_input, parsed["intent"], resolution)
        event = outcome["event"]
        previous = self.store.latest_narration(session_id)
        progression = select_progression(self.package, resolution["state"], previous and previous["continuity"], resolution.get("endingId"))
        narration = narrate(self.package, parsed["intent"], resolution, progression)
        self.store.save_narration(session_id, event["sequence"], narration, progression["continuity"])
        return {"kind": "resolved", "event": event, "narration": narration, "resolution": resolution, "directions": progression["beat"]["nextDirections"]}
