"""Source-bound editorial openings, compiled before reader/module generation."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


POLICY = "official_unknown_reader/1"


def validate_official_openings(package):
    model = package["story"].get("entryModel", {})
    if model.get("policy") != POLICY:
        return
    entries = model["entryPoints"]
    characters = {c["id"]: c for c in package["characters"]}
    locations = {p["id"] for p in package["locations"]}
    items = {p["id"] for p in package["items"]}
    approved = model["sourceCharacterIds"]
    if model.get("newCharacter", {}).get("enabled") or len(entries) != len(approved):
        raise ValueError("官方开局必须一人一入口，且禁止自由新建角色")
    for character_id in approved:
        character = characters[character_id]
        candidates = [e for e in entries if e.get("sourceCharacterIds") == [character_id]]
        if len(candidates) != 1 or candidates[0]["id"] != character.get("defaultEntryPointId"):
            raise ValueError("角色默认入口与审核开局不一致")
        entry = candidates[0]
        context, state = entry["openingContext"], entry["openingState"]
        if set(state) != {"playerLocationId", "characterLocationIds", "itemOwnerCharacterIds", "itemLocationIds", "characterOutcomeStates"}:
            raise ValueError("审核开局不得覆盖进度、身份或其他运行时字段")
        if state["characterOutcomeStates"] != {}:
            raise ValueError("P0 人物后果账本必须保持空的预留状态")
        if entry.get("availableToNewCharacter") or not entry.get("sourceCharacterNarratives", {}).get(character_id):
            raise ValueError("官方入口缺少角色正文或意外开放自创")
        if state["playerLocationId"] not in locations or state["characterLocationIds"].get(character_id) != state["playerLocationId"]:
            raise ValueError("开局人物位置不一致")
        if any(c not in characters or p not in locations for c, p in state["characterLocationIds"].items()):
            raise ValueError("开局位置引用无效")
        if any(i not in items or c not in characters for i, c in state["itemOwnerCharacterIds"].items()):
            raise ValueError("开局物品归属引用无效")
        if any(i not in items or p not in locations for i, p in state["itemLocationIds"].items()):
            raise ValueError("开局地面物品引用无效")
        if set(state["itemOwnerCharacterIds"]) & set(state["itemLocationIds"]):
            raise ValueError("同一物品不能既在人物身上又在地面")
        if context["sourceSha256"] != package["sourceAnalysis"]["sha256"]:
            raise ValueError("开局前史未绑定母本")
        if not all(context.get(key) for key in ("knownFacts", "unknownBoundaries", "relationships", "evidence")):
            raise ValueError("开局缺少知识、关系或原文证据")


def apply_opening_review(package, source_path: Path, review):
    source = source_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if (review.get("schemaVersion") != "official-opening-review/1"
            or review.get("sourceSha256") != digest
            or package["sourceAnalysis"]["sha256"] != digest):
        raise ValueError("开局审核配置必须绑定当前冻结母本")
    package = copy.deepcopy(package)
    from .source import inspect_standard_novel
    chapters = {c["id"]: c for c in inspect_standard_novel(source_path)["chapters"]}
    characters = {c["id"]: c for c in package["characters"]}
    entries = {e["id"]: e for e in package["story"]["entryModel"]["entryPoints"]}
    for card in review["cast"]:
        target = characters[card["sourceCharacterId"]]
        if target["name"] != card["name"]:
            raise ValueError("审核角色 ID 与人名不一致")
        target.update({k: v for k, v in card.items() if k not in ("sourceCharacterId", "name")})
        target["menuDescription"] = card["identitySummary"]
    for collection in ("locations", "items"):
        existing = {e["id"] for e in package[collection]}
        for entity in review[collection]:
            if entity["id"] in existing or not entity.get("sourceDescriptions"):
                raise ValueError("审核实体重复或缺少原文证据")
            if any(quote not in source for quote in entity["sourceDescriptions"]):
                raise ValueError("审核实体证据不在母本中")
            package[collection].append(copy.deepcopy(entity))
            existing.add(entity["id"])
    official_entries = []
    for opening in review["openings"]:
        character_id = opening["characterId"]
        entry = copy.deepcopy(entries[opening["baseEntryPointId"]])
        # An exact paragraph cutoff distinguishes the opening from a broad beat.
        cutoff_quote = opening["cutoffQuote"]
        if source.count(cutoff_quote) != 1:
            raise ValueError("开局截止原文必须唯一匹配")
        cutoff = source.index(cutoff_quote) + len(cutoff_quote)
        bounds = chapters[entry["sourceChapterId"]]["characterRange"]
        if not bounds["start"] <= cutoff <= bounds["end"]:
            raise ValueError("开局截止点与锚点不在同一章")
        evidence = opening["context"]["evidence"]
        if any(quote not in source[:cutoff] for quote in evidence):
            raise ValueError("开局证据越过截止点或不在母本中")
        entry.update({
            "id": opening["id"], "title": opening["title"], "summary": opening["summary"],
            "sourceCharacterIds": [character_id], "availableToNewCharacter": False,
            "timelineRefs": [], "sourceCharacterNarratives": {character_id: opening["narrative"]},
            "openingSummary": opening["summary"], "openingThreads": opening["threads"],
            "openingActions": opening["actions"], "openingClues": opening["context"]["knownFacts"],
            "openingState": copy.deepcopy(opening["state"]),
            "sourceCharacterLocationIds": {character_id: opening["state"]["playerLocationId"]},
            "openingContext": {**copy.deepcopy(opening["context"]), "sourceSha256": digest,
                               "sourceCutoff": cutoff, "sourceCutoffLine": source[:cutoff].count("\n") + 1},
        })
        entry.pop("newCharacterNarrative", None)
        characters[character_id]["defaultEntryPointId"] = entry["id"]
        official_entries.append(entry)
    package["story"]["entryModel"] = {
        "policy": POLICY, "sourceCharacterIds": [o["characterId"] for o in review["openings"]],
        "defaultEntryPointId": official_entries[0]["id"], "entryPoints": official_entries,
        "newCharacter": {"enabled": False},
    }
    package["metadata"].update(review["metadata"])
    package["metadata"]["openingReviewSha256"] = hashlib.sha256(
        json.dumps(review, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    package["stateModel"]["runtimeStateFields"] += ["itemLocationIds", "characterOutcomeStates"]
    # Reserve the outcome ledger without advertising unimplemented permanent outcomes.
    package["stateModel"]["immutableFields"].append("characterOutcomeStates")
    package["story"]["entryModel"]["continuityContract"] = {
        "locked": "世界铁律及截至入场点已发生的历史；仅当角色有依据时才成为其知识。",
        "mutable": "入场后的未来、选择及其后果；原著后续只是候选路线。",
        "outcomeStateField": "characterOutcomeStates",
        "outcomeRecordSchema": {"status": ["alive", "dead", "departed"], "permanence": ["temporary", "permanent"],
                                "causeBranchId": "string", "evidence": "string"},
        "outcomeUpdatesEnabled": False,
        "implementationBoundary": "P0 仅持久化预留空账本，P3 实现并校验永久变化。",
    }
    validate_official_openings(package)
    return package
