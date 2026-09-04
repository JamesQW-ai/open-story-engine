"""Authoritative state transitions shared by ordinary play and co-creation."""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List


def matches_condition(state: Dict[str, Any], condition: str) -> bool:
    if condition.startswith("has:"):
        return condition[4:] in state.get("inventory", [])
    if condition.startswith("not_has:"):
        return condition[8:] not in state.get("inventory", [])
    if condition.startswith("!"):
        return not bool(state.get("flags", {}).get(condition[1:]))
    if ">=" in condition:
        field, raw = condition.split(">=", 1)
        return state.get("counters", {}).get(field, 0) >= int(raw)
    if "<=" in condition:
        field, raw = condition.split("<=", 1)
        return state.get("counters", {}).get(field, 0) <= int(raw)
    if "=" in condition:
        field, raw = condition.split("=", 1)
        if field in state.get("flags", {}):
            return state["flags"][field] == (raw == "true")
        if field in state.get("counters", {}):
            return state["counters"][field] == int(raw)
    return bool(state.get("flags", {}).get(condition))


def apply_effects(state: Dict[str, Any], effects: Iterable[str]) -> None:
    for effect in effects:
        if effect.startswith("add:"):
            item_id = effect[4:]
            if item_id not in state["inventory"]:
                state["inventory"].append(item_id)
        elif effect.startswith("move:"):
            state["currentLocationId"] = effect[5:]
        elif effect.startswith("increment:"):
            key = effect[10:]
            if key not in state.get("counters", {}):
                raise ValueError(f"未知计数器: {key}")
            state["counters"][key] += 1
        elif effect.startswith("set:relationship."):
            pair = effect[len("set:relationship."):].split("=", 1)
            if len(pair) != 2:
                raise ValueError(f"无效关系效果: {effect}")
            state["relationships"][pair[0]] = int(pair[1])
        elif effect.startswith("set:"):
            pair = effect[4:].split("=", 1)
            key = pair[0]
            value = pair[1] if len(pair) == 2 else None
            if value is None:
                if key not in state.get("flags", {}):
                    raise ValueError(f"未知旗标: {key}")
                state["flags"][key] = True
            elif key in state.get("counters", {}) and value.lstrip("-").isdigit():
                state["counters"][key] = int(value)
            elif key in state.get("flags", {}) and value in ("true", "false"):
                state["flags"][key] = value == "true"
            else:
                raise ValueError(f"不支持的状态效果: {effect}")
        else:
            raise ValueError(f"不支持的状态效果: {effect}")


def resolve_turn(package: Dict[str, Any], state: Dict[str, Any], action: Dict[str, str], roll: int) -> Dict[str, Any]:
    next_state = copy.deepcopy(state)
    existing = find_ending(package, next_state)
    if existing:
        return {"outcome": "terminal", "action": action, "state": next_state, "appliedEffects": [], "endingId": existing["id"], "blockReason": "当前会话已达到结局，不能继续执行普通行动"}
    candidates = [
        candidate for candidate in package["rules"]["resolutions"]
        if candidate["nodeId"] == next_state["currentNodeId"]
        and candidate["actionType"] == action["actionType"]
        and candidate["targetId"] == action["targetId"]
    ]
    resolution = next((item for item in candidates if all(matches_condition(next_state, guard) for guard in item.get("guards", []))), None)
    if resolution is None:
        return {"outcome": "blocked", "action": action, "state": next_state, "appliedEffects": [], "blockReason": "行动前置条件未满足" if candidates else "当前节点没有可匹配的规则行动"}
    check = package["rules"]["check"]
    if roll < check["randomRange"]["min"] or roll > check["randomRange"]["max"]:
        raise ValueError(f"随机源返回了范围外的值: {roll}")
    score = next_state["player"]["attributes"][resolution["attribute"]] + roll
    success_at = check["successAt"] + resolution["difficulty"] - 1
    partial_at = check["partialSuccessAt"] + resolution["difficulty"] - 1
    outcome = "success" if score >= success_at else "partial_success" if score >= partial_at else "failure"
    effects = resolution["onSuccess"] if outcome == "success" else resolution["onPartial"] if outcome == "partial_success" else resolution["onFailure"]
    apply_effects(next_state, effects)
    for terminal_rule in package["rules"].get("terminalRules", []):
        if all(matches_condition(next_state, condition) for condition in terminal_rule["when"]):
            apply_effects(next_state, terminal_rule["set"])
    node = next((node for node in package["story"]["nodes"] if node["id"] == next_state["currentNodeId"]), None)
    transition = next((item for item in (node or {}).get("transitions", []) if all(matches_condition(next_state, condition) for condition in item["when"])), None)
    if transition:
        next_state["currentNodeId"] = transition["toNodeId"]
    ending = find_ending(package, next_state)
    return {"outcome": outcome, "action": action, "state": next_state, "resolutionId": resolution["id"], "roll": roll, "score": score, "appliedEffects": effects, "endingId": ending and ending["id"]}


def find_ending(package: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any] | None:
    return next((ending for ending in package["story"]["endings"] if all(matches_condition(state, condition) for condition in ending["when"])), None)


def create_patch(before: Any, after: Any, path: List[str] | None = None) -> List[Dict[str, Any]]:
    path = path or []
    if isinstance(before, dict) and isinstance(after, dict):
        result: List[Dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            if key not in after:
                result.append({"op": "remove", "path": path + [key]})
            elif key not in before:
                result.append({"op": "set", "path": path + [key], "value": copy.deepcopy(after[key])})
            else:
                result.extend(create_patch(before[key], after[key], path + [key]))
        return result
    if before != after:
        return [{"op": "set", "path": path, "value": copy.deepcopy(after)}]
    return []


def apply_patch(state: Dict[str, Any], operations: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    result = copy.deepcopy(state)
    for operation in operations:
        target: Any = result
        path = operation["path"]
        for segment in path[:-1]:
            target = target[segment]
        if operation["op"] == "remove":
            del target[path[-1]]
        else:
            target[path[-1]] = copy.deepcopy(operation["value"])
    return result
