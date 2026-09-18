"""Append-only, branch-owned facts for derivative story continuity."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, Iterable, List


BRANCH_LEDGER_KEY = "branchLedger"
LEDGER_SCHEMA_VERSION = "branch-state-ledger/0.1"
LEDGER_KINDS = frozenset({"location", "character", "item", "relationship", "clue", "event"})
LEDGER_OPERATIONS = frozenset({"inherited", "added", "changed"})


def empty_branch_ledger() -> Dict[str, Any]:
    return {"schemaVersion": LEDGER_SCHEMA_VERSION, "entries": []}


def ensure_branch_ledger(state: Dict[str, Any]) -> None:
    """Attach an empty ledger to a newly-created runtime state only."""
    state.setdefault(BRANCH_LEDGER_KEY, empty_branch_ledger())
    validate_branch_ledger(state[BRANCH_LEDGER_KEY])


def _is_json_value(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return True


def _validate_source(value: Any) -> None:
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), str) and value[key].strip() for key in ("kind", "ref")
    ):
        raise ValueError("分支状态账本条目必须包含来源 kind 和 ref")
    if "nodeRef" in value and (not isinstance(value["nodeRef"], str) or not value["nodeRef"].strip()):
        raise ValueError("分支状态账本来源 nodeRef 必须是非空字符串")


def validate_branch_ledger(value: Any) -> None:
    if not isinstance(value, dict) or value.get("schemaVersion") != LEDGER_SCHEMA_VERSION:
        raise ValueError("分支状态账本 schemaVersion 无效")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise ValueError("分支状态账本 entries 必须是数组")
    previous_sequence = 0
    ids = set()
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) and entry[key].strip()
            for key in ("id", "kind", "operation", "entityId", "summary")
        ):
            raise ValueError("分支状态账本条目缺少必要字段")
        if entry["kind"] not in LEDGER_KINDS or entry["operation"] not in LEDGER_OPERATIONS:
            raise ValueError("分支状态账本条目的类型或操作无效")
        if not isinstance(entry.get("sequence"), int) or entry["sequence"] != previous_sequence + 1:
            raise ValueError("分支状态账本 sequence 必须从 1 连续递增")
        if entry["id"] in ids:
            raise ValueError("分支状态账本条目 ID 重复")
        if not _is_json_value(entry.get("before")) or not _is_json_value(entry.get("after")):
            raise ValueError("分支状态账本条目的前后状态不可序列化")
        _validate_source(entry.get("source"))
        previous_sequence = entry["sequence"]
        ids.add(entry["id"])


def append_branch_ledger(
    state: Dict[str, Any], changes: Iterable[Dict[str, Any]], source: Dict[str, str],
) -> None:
    """Append locally validated changes without ever mutating a StoryPackage."""
    ensure_branch_ledger(state)
    _validate_source(source)
    ledger = copy.deepcopy(state[BRANCH_LEDGER_KEY])
    entries: List[Dict[str, Any]] = ledger["entries"]
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("分支状态账本变更必须是对象")
        kind = change.get("kind")
        operation = change.get("operation")
        entity_id = change.get("entityId")
        summary = change.get("summary")
        if kind not in LEDGER_KINDS or operation not in LEDGER_OPERATIONS:
            raise ValueError("分支状态账本变更的类型或操作无效")
        if not isinstance(entity_id, str) or not entity_id.strip() or not isinstance(summary, str) or not summary.strip():
            raise ValueError("分支状态账本变更必须包含 entityId 和 summary")
        before = copy.deepcopy(change.get("before"))
        after = copy.deepcopy(change.get("after"))
        if not _is_json_value(before) or not _is_json_value(after):
            raise ValueError("分支状态账本变更的前后状态不可序列化")
        sequence = len(entries) + 1
        fingerprint = json.dumps(
            {"sequence": sequence, "kind": kind, "operation": operation, "entityId": entity_id, "before": before, "after": after, "source": source},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        entries.append({
            "id": "ledger_" + str(sequence).zfill(6) + "_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12],
            "sequence": sequence, "kind": kind, "operation": operation, "entityId": entity_id.strip(), "summary": summary.strip(),
            "before": before, "after": after, "source": copy.deepcopy(source),
        })
    validate_branch_ledger(ledger)
    state[BRANCH_LEDGER_KEY] = ledger


def ledger_context(state: Dict[str, Any], limit: int = 24) -> str:
    """Return a bounded, structured prompt projection of authoritative facts."""
    ledger = state.get(BRANCH_LEDGER_KEY, empty_branch_ledger())
    validate_branch_ledger(ledger)
    if limit < 1:
        raise ValueError("分支状态账本上下文 limit 必须大于 0")
    latest: Dict[tuple[str, str], Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    for entry in ledger["entries"]:
        if entry["kind"] == "event":
            events.append(entry)
        else:
            latest[(entry["kind"], entry["entityId"])] = entry
    selected = sorted(latest.values(), key=lambda item: item["sequence"])[-limit:]
    selected.extend(events[-min(8, limit):])
    selected.sort(key=lambda item: item["sequence"])
    projection = [{
        "kind": entry["kind"], "operation": entry["operation"], "entityId": entry["entityId"],
        "summary": entry["summary"], "after": entry["after"], "source": entry["source"],
    } for entry in selected]
    return json.dumps({"schemaVersion": LEDGER_SCHEMA_VERSION, "confirmed": projection}, ensure_ascii=False, separators=(",", ":"))
