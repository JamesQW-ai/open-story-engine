"""Immutable, auditable context snapshots for player narrative turns.

This module deliberately contains no model calls.  A bundle is a projection of
authoritative StoryPackage and branch data; planners, writers, reviewers and
repairs can consume projections of the same snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Dict, Iterable, Mapping, Optional


SCHEMA_VERSION = "narrative-context/0.3"
_PROJECTIONS = {"result_contract", "chapter", "fact_extract", "grounding_review", "repair"}
_AUTHORITIES = {"authoritative", "confirmed_evidence", "style_only"}
_VALIDITIES = {"confirmed", "unknown", "rejected"}
_VISIBILITIES = {"player_known", "character_known", "public_world_fact", "author_truth"}
_MEMORY_STATUSES = {"candidate", "confirmed", "rejected", "unknown"}
_MEMORY_KINDS = {"event", "clue", "relationship", "goal_progress", "public_fact"}
_PLAYER_VISIBLE = {"player_known", "public_world_fact"}
_SELECTED_DIRECTION_LIMITS = {"id": 200, "title": 200, "summary": 1000}
_CONTINUITY_WINDOW_LIMIT = 2
_STATE_VISIBILITY_SOURCES = {"explicit", "runtime_default_migration"}
STATE_VISIBILITY_MODES = {"audit_fallback", "formal_required"}
CONTEXT_PRIORITY = (
    "hardSpec",
    "confirmed_dynamic_memory",
    "shortTermMemory.turnIntent",
    "shortTermMemory.continuityWindow",
    "unconfirmed_evidence",
    "styleGuide",
)
CONTEXT_FACTS_MAX_ITEMS = 10
CONTEXT_FACTS_MAX_CHARS = 3200
CONTEXT_SCOPE_MAX_ITEMS = 8
CONTEXT_SCOPE_ITEM_MAX_CHARS = 900
CONTEXT_SCOPE_MAX_CHARS = 6400
CONTEXT_FACT_STOPWORDS = frozenset({
    "没有", "不能", "不得", "存在", "问题", "事实", "当前", "本回", "已经", "仍然",
    "确认", "进行", "相关", "方向", "行动", "回应", "位置", "人物", "场景", "记录",
    "留下", "眼前", "继续", "等待", "原地", "状态", "已知", "可用", "完成", "发生",
    "需要", "以及", "这是", "那个", "双方", "部分", "东西", "一条", "一处", "时候",
    "开始", "随后", "因此", "可以", "不知", "未知",
})
_EVIDENCE_KINDS = {
    "public_fact", "current_beat", "confirmed_event", "recent_prose",
    "style_sample", "soft_evidence", "continuity_summary", "source_dialogue",
}
_HASH_EXCLUDED_KEYS = {
    "contextId", "contextSha256", "parentContextId", "recordedAt", "createdAt",
    "updatedAt", "timestamp", "timestamps", "requestId", "traceId", "runId",
}


class ContextBundleError(ValueError):
    """Raised when a context snapshot cannot be safely consumed."""


def context_fact_terms(query: str) -> set[str]:
    """Extract conservative lexical keys for bounded hard-fact selection."""
    if not isinstance(query, str):
        return set()
    terms = set(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]{2}", query))
    terms.update(re.findall(r"[A-Za-z0-9_]{3,}", query))
    return {term for term in terms if term not in CONTEXT_FACT_STOPWORDS}


def select_bounded_context_facts(
    facts: Iterable[Mapping[str, Any]],
    *,
    query: str = "",
    explicit_ids: Iterable[str] = (),
    max_items: int = CONTEXT_FACTS_MAX_ITEMS,
    max_chars: int = CONTEXT_FACTS_MAX_CHARS,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    """Select relevant hard facts without allowing prompt-size accumulation."""
    fact_items = [dict(item) for item in facts if isinstance(item, Mapping)]
    explicit = {value for value in explicit_ids if isinstance(value, str) and value.strip()}
    terms = context_fact_terms(query)
    scored = []
    for index, item in enumerate(fact_items):
        fact_id = item.get("id")
        text = item.get("text")
        if not isinstance(fact_id, str) or not fact_id.strip() or not isinstance(text, str) or not text.strip():
            continue
        overlap = sum(text.count(term) for term in terms)
        if overlap <= 0 and fact_id not in explicit:
            continue
        basis = "explicit_source_ref" if fact_id in explicit and overlap <= 0 else "term_overlap"
        scored.append((-max(overlap, 1), index, fact_id, item, basis))
    scored.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    chosen, chosen_chars = [], 0
    for _, index, fact_id, item, basis in scored:
        if len(chosen) >= max_items:
            break
        text = item["text"].strip()
        if chosen_chars + len(text) > max_chars:
            continue
        chosen.append((index, fact_id, item, basis))
        chosen_chars += len(text)
    chosen.sort(key=lambda entry: entry[0])
    selected = [item for _, _, item, _ in chosen]
    audit = {
        "selectedFactIds": [fact_id for _, fact_id, _, _ in chosen],
        "selectedChars": chosen_chars,
        "omittedFactCount": max(0, len(fact_items) - len(chosen)),
        "maxItems": max_items,
        "maxChars": max_chars,
        "selectedFacts": [
            {"id": fact_id, "selectionBasis": basis}
            for _, fact_id, _, basis in chosen
        ],
    }
    return selected, chosen_chars, audit


def focused_recent_lineage(lineage, query):
    """Replace vague summaries with bounded verbatim passages for this action.

    Only the current ancestry's last two turns are eligible. A character's
    quoted account is evidence of what was said, not verified world history.
    """
    # Overlapping Chinese bigrams avoid losing a match merely because the
    # same phrase begins at a different offset in the action and paragraph.
    terms = context_fact_terms(query)
    terms.update(term for term in re.findall(r'(?=([\u3400-\u4dbf\u4e00-\u9fff]{2}))', query)
                 if term not in CONTEXT_FACT_STOPWORDS)
    remaining = 1400
    selected = []
    nodes = [node for node in lineage[-2:] if isinstance(node, dict)]
    header = '已展示原文节选；人物自述、推断和承诺不等于已核实事实或已完成行动：\n'
    for position, node in enumerate(reversed(nodes)):
        # Reserve space for both turns: the latest mistaken account must not
        # consume the earlier evidence needed to notice the contradiction.
        allowance = remaining // (len(nodes) - position)
        paragraphs = [(i, p.strip()) for i, p in enumerate(
            str(node.get('narrativeText', '')).split('\n\n')) if p.strip()]
        scores = {i: sum(term in p for term in terms) for i, p in paragraphs}
        ranked = sorted(paragraphs, key=lambda pair: (-scores[pair[0]], -pair[0]))
        anchors = [i for i, p in ranked if scores[i] > 0][:2]
        if not anchors and paragraphs:
            anchors = [paragraphs[-1][0]]
        passages = {}
        used = len(header)

        def include(index, paragraph):
            nonlocal used
            cost = len(f'P{index + 1}: {paragraph}\n')
            if index not in passages and len(paragraph) <= 650 and used + cost <= allowance:
                passages[index] = paragraph
                used += cost

        # The cause of an irreversible result outranks incidental keyword
        # matches (e.g. "I admit what I just did" does not repeat "killed").
        # Keep its actual prose within the existing per-turn budget.
        update = node.get('consequenceUpdate') or {}
        consequences = [item for item in update.get('outcomes', [])
                        if item.get('status') == 'dead' or
                        item.get('status') == 'departed' and item.get('permanence') == 'permanent']
        consequences += [item for item in update.get('stateChanges', [])
                         if item.get('attribute') == 'destroyedPermanently' and item.get('value') is True]
        evidence = [item['evidence'] for item in consequences
                    if isinstance(item.get('evidence'), str) and item['evidence'].strip()
                    and item['evidence'] in node.get('narrativeText', '')]
        # A referenced span can start with identification/setup. Prefer its
        # concluding result and immediate cause over that setup.
        for index, paragraph in reversed(paragraphs):
            if any(paragraph in quote or quote in paragraph for quote in evidence):
                include(index, paragraph)
        for index, paragraph in ranked:
            if index in anchors:
                include(index, paragraph)
        # Adjacent qualifications and replies belong with the selected anchor;
        # include them before unrelated high-frequency mentions of the topic.
        neighbors = [(i, p) for i, p in paragraphs
                     if i not in anchors and (scores[i] > 0 or '“' in p)
                     and any(abs(i - anchor) == 1 for anchor in passages)]
        neighbors.sort(key=lambda pair: (-scores[pair[0]], pair[0]))
        for index, paragraph in neighbors:
            include(index, paragraph)
        if passages:
            content = header + '\n'.join(f'P{i + 1}: {p}' for i, p in sorted(passages.items()))
        else:
            summary = node.get('summary', '')
            content = (summary if isinstance(summary, str) and summary.strip() and len(summary) <= allowance
                       else '本回合原文未选入预算。')
        remaining -= len(content)
        selected.append({**node, 'summary': content})
    return list(reversed(selected))


class _FrozenList(list):
    """JSON-compatible list that rejects in-place mutation."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("上下文快照不可变。")

    __setitem__ = __delitem__ = __iadd__ = __imul__ = append = clear = extend = insert = pop = remove = reverse = sort = _immutable


class _FrozenDict(dict):
    """JSON-compatible dict that rejects in-place mutation."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("上下文快照不可变。")

    __setitem__ = __delitem__ = __ior__ = clear = pop = popitem = setdefault = update = _immutable


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _json_value(value: Any) -> Any:
    """Return a JSON-safe deep copy without allowing caller mutation."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as error:
        raise ContextBundleError("上下文包含不可序列化字段。") from error


def _source_sort_key(item: Mapping[str, Any]) -> tuple:
    authority_order = {"authoritative": 0, "confirmed_evidence": 1, "style_only": 2}
    return (
        authority_order.get(str(item.get("authority")), 9),
        str(item.get("branchId", "")),
        str(item.get("sourceId", "")),
    )


def _normalise_for_hash(data: Mapping[str, Any]) -> Dict[str, Any]:
    normalised = _json_value(data)
    for key in _HASH_EXCLUDED_KEYS:
        normalised.pop(key, None)
    for key in ("audit", "provenance"):
        value = normalised.get(key)
        if isinstance(value, dict):
            normalised[key] = {
                item_key: item_value
                for item_key, item_value in value.items()
                if item_key not in _HASH_EXCLUDED_KEYS
            }
    evidence = normalised.get("allowedEvidence")
    if isinstance(evidence, list):
        normalised["allowedEvidence"] = sorted(
            (item for item in evidence if isinstance(item, dict)),
            key=_source_sort_key,
        )
    provenance = normalised.get("provenance")
    if isinstance(provenance, dict):
        for key in ("modulePaths", "sourceIds", "excludedSources"):
            values = provenance.get(key)
            if isinstance(values, list):
                provenance[key] = sorted(values, key=str)
    memories = normalised.get("dynamicMemory")
    if isinstance(memories, list):
        for item in memories:
            if isinstance(item, dict) and isinstance(item.get("sourceIds"), list):
                item["sourceIds"] = sorted(item["sourceIds"], key=str)
        normalised["dynamicMemory"] = sorted(
            (item for item in memories if isinstance(item, dict)),
            key=lambda item: (int(item.get("sequence", 0)), str(item.get("memoryId", ""))),
        )
    return normalised


def context_priority(layer: str) -> int:
    """Return the lower-is-stronger conflict priority for a context layer."""
    try:
        return CONTEXT_PRIORITY.index(layer)
    except ValueError as error:
        raise ContextBundleError("未知上下文优先级：" + str(layer)) from error


def canonical_sha256(data: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _normalise_for_hash(data),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_mapping(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ContextBundleError(f"{name} 必须是对象。")
    return value


def _validate_evidence(item: Mapping[str, Any], branch_id: str) -> None:
    required = ("sourceId", "kind", "visibility", "branchId", "location", "content", "authority", "validity")
    missing = [key for key in required if key not in item]
    if missing:
        raise ContextBundleError("证据项缺少字段：" + ",".join(missing))
    for key in ("sourceId", "kind", "visibility", "branchId", "location", "authority", "validity"):
        if not isinstance(item[key], str) or not item[key].strip():
            raise ContextBundleError(f"证据项字段 {key} 必须是非空字符串。")
    if item["authority"] not in _AUTHORITIES:
        raise ContextBundleError("证据项 authority 无效：" + str(item["authority"]))
    if item["visibility"] not in _VISIBILITIES:
        raise ContextBundleError("证据项 visibility 无效：" + str(item["visibility"]))
    if item["kind"] not in _EVIDENCE_KINDS:
        raise ContextBundleError("证据项 kind 无效：" + str(item["kind"]))
    if item["validity"] not in _VALIDITIES:
        raise ContextBundleError("证据项 validity 无效：" + str(item["validity"]))
    evidence_branch = item["branchId"]
    if evidence_branch not in (branch_id, "package", "global"):
        raise ContextBundleError(
            f"证据项跨越当前分支：{item['sourceId']} ({evidence_branch} != {branch_id})"
        )
    if item["validity"] == "rejected":
        raise ContextBundleError("rejected 证据不得进入 allowedEvidence：" + item["sourceId"])


def _validate_memory(item: Mapping[str, Any], branch_id: str) -> None:
    required = (
        "memoryId", "kind", "sourceIds", "branchId", "visibility", "authority",
        "validity", "status", "sequence", "content",
    )
    missing = [key for key in required if key not in item]
    if missing:
        raise ContextBundleError("dynamicMemory 项缺少字段：" + ",".join(missing))
    for key in ("memoryId", "kind", "branchId", "visibility", "authority", "validity", "status"):
        if not isinstance(item[key], str) or not item[key].strip():
            raise ContextBundleError(f"dynamicMemory 字段 {key} 必须是非空字符串。")
    if not isinstance(item["sourceIds"], list) or not item["sourceIds"]:
        raise ContextBundleError("dynamicMemory.sourceIds 必须是非空字符串列表。")
    if any(not isinstance(source_id, str) or not source_id.strip() for source_id in item["sourceIds"]):
        raise ContextBundleError("dynamicMemory.sourceIds 必须是非空字符串列表。")
    if len(item["sourceIds"]) != len(set(item["sourceIds"])):
        raise ContextBundleError("dynamicMemory.sourceIds 不能重复。")
    if not isinstance(item["sequence"], int) or isinstance(item["sequence"], bool) or item["sequence"] < 0:
        raise ContextBundleError("dynamicMemory.sequence 必须是非负整数。")
    if item["kind"] not in _MEMORY_KINDS:
        raise ContextBundleError("dynamicMemory kind 无效：" + str(item["kind"]))
    if item["visibility"] not in _VISIBILITIES:
        raise ContextBundleError("dynamicMemory visibility 无效：" + str(item["visibility"]))
    if item["authority"] not in _AUTHORITIES:
        raise ContextBundleError("dynamicMemory authority 无效：" + str(item["authority"]))
    if item["validity"] not in _VALIDITIES:
        raise ContextBundleError("dynamicMemory validity 无效：" + str(item["validity"]))
    if item["status"] not in _MEMORY_STATUSES:
        raise ContextBundleError("dynamicMemory status 无效：" + str(item["status"]))
    expected_validity = {
        "candidate": "unknown",
        "confirmed": "confirmed",
        "rejected": "rejected",
        "unknown": "unknown",
    }[item["status"]]
    if item["validity"] != expected_validity:
        raise ContextBundleError(
            f"dynamicMemory status={item['status']} 与 validity={item['validity']} 不匹配。"
        )
    if item["status"] == "candidate" and item["authority"] != "confirmed_evidence":
        raise ContextBundleError("candidate 动态记忆必须来自 confirmed_evidence。")
    if item["status"] == "confirmed" and item["authority"] == "style_only":
        raise ContextBundleError("style_only 不得晋升为 confirmed 动态记忆。")
    memory_branch = item["branchId"]
    if memory_branch not in (branch_id, "package", "global"):
        raise ContextBundleError(
            f"dynamicMemory 跨越当前分支：{item['memoryId']} ({memory_branch} != {branch_id})"
        )
    if item["status"] == "candidate" and memory_branch != branch_id:
        raise ContextBundleError("candidate 动态记忆不得跨分支继承：" + item["memoryId"])
    parent_memory_id = item.get("parentMemoryId")
    inheritance_reason = item.get("inheritanceReason")
    if parent_memory_id is not None:
        if not isinstance(parent_memory_id, str) or not parent_memory_id.strip():
            raise ContextBundleError("dynamicMemory.parentMemoryId 必须是非空字符串。")
        if parent_memory_id == item["memoryId"]:
            raise ContextBundleError("dynamicMemory.parentMemoryId 不能指向自身。")
        if not isinstance(inheritance_reason, str) or not inheritance_reason.strip():
            raise ContextBundleError("dynamicMemory.parentMemoryId 必须同时提供 inheritanceReason。")
    elif inheritance_reason is not None:
        raise ContextBundleError("dynamicMemory.inheritanceReason 只能与 parentMemoryId 一起提供。")
    conflict_with = item.get("conflictWith")
    if conflict_with is not None:
        if (not isinstance(conflict_with, list)
                or not conflict_with
                or any(not isinstance(memory_id, str) or not memory_id.strip() for memory_id in conflict_with)
                or len(conflict_with) != len(set(conflict_with))
                or item["memoryId"] in conflict_with):
            raise ContextBundleError("dynamicMemory.conflictWith 必须是无重复且不包含自身的记忆 ID 列表。")


def _memory_payload(item: Mapping[str, Any], mutable_fields: set[str]) -> str:
    # JSON types are part of a fact: Python equality treats True and 1 as equal.
    return json.dumps({key: value for key, value in item.items() if key not in mutable_fields},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_dynamic_memory_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    branch_id: Optional[str] = None,
) -> None:
    """Validate one append-only dynamic-memory transition.

    This pure guard does not persist snapshots or establish the existence or
    semantic sufficiency of conflict/evidence references. Callers must run the
    normal bundle and evidence validation too.
    """
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ContextBundleError("dynamicMemory 状态迁移必须使用对象快照。")
    previous_branch = previous.get("branchId")
    current_branch = current.get("branchId")
    if not isinstance(previous_branch, str) or not previous_branch.strip():
        raise ContextBundleError("dynamicMemory.previous.branchId 必须是非空字符串。")
    if not isinstance(current_branch, str) or not current_branch.strip():
        raise ContextBundleError("dynamicMemory.current.branchId 必须是非空字符串。")
    _validate_memory(previous, previous_branch)
    _validate_memory(current, current_branch)
    if branch_id is not None and current_branch != branch_id:
        raise ContextBundleError("dynamicMemory.current.branchId 不匹配目标分支。")
    if current["sequence"] <= previous["sequence"]:
        raise ContextBundleError("dynamicMemory 状态迁移必须增加 sequence。")

    parent_memory_id = current.get("parentMemoryId")
    if parent_memory_id is not None and (
            current["memoryId"] != previous["memoryId"] or current_branch != previous_branch):
        if (previous["status"] != "confirmed"
                or current["status"] != "confirmed"
                or current["memoryId"] == previous["memoryId"]
                or parent_memory_id != previous["memoryId"]
                or current_branch == previous_branch):
            raise ContextBundleError(
                "dynamicMemory 分支继承必须从 confirmed 快照创建新的跨分支 memoryId。"
            )
        inheritance_fields = {"memoryId", "branchId", "sequence", "parentMemoryId", "inheritanceReason"}
        if _memory_payload(previous, inheritance_fields) != _memory_payload(current, inheritance_fields):
            raise ContextBundleError("dynamicMemory 继承不得改写事实、来源、可见性或其他证据字段。")
        return

    if current["memoryId"] != previous["memoryId"] or current_branch != previous_branch:
        raise ContextBundleError("dynamicMemory 状态迁移必须保留 memoryId 和 branchId。")
    allowed = {
        "candidate": {"confirmed", "unknown", "rejected"},
        "unknown": {"candidate", "confirmed", "rejected"},
        "confirmed": {"rejected"},
        "rejected": set(),
    }
    if current["status"] not in allowed[previous["status"]]:
        raise ContextBundleError(
            f"dynamicMemory 不允许从 {previous['status']} 迁移到 {current['status']}。"
        )
    transition_fields = {"sequence", "status", "validity", "conflictWith"}
    if _memory_payload(previous, transition_fields) != _memory_payload(current, transition_fields):
        raise ContextBundleError("dynamicMemory 状态迁移不得改写事实、来源、可见性或其他证据字段。")
    if previous["status"] == "confirmed" and current["status"] == "rejected":
        conflict_with = current.get("conflictWith")
        if not isinstance(conflict_with, list) or not conflict_with:
            raise ContextBundleError("confirmed 动态记忆降为 rejected 必须记录 conflictWith。")


def _validate_package(package: Mapping[str, Any]) -> None:
    for key in ("id", "version"):
        if not isinstance(package.get(key), str) or not package[key].strip():
            raise ContextBundleError(f"package.{key} 必须是非空字符串。")


def _state_leaves(value: Any, path: str = "") -> Dict[str, Any]:
    if isinstance(value, dict):
        leaves = {}
        for key, child in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            leaves.update(_state_leaves(child, path + "/" + escaped))
        return leaves
    if isinstance(value, list):
        leaves = {}
        for index, child in enumerate(value):
            leaves.update(_state_leaves(child, path + "/" + str(index)))
        return leaves
    return {path: value}


def _validate_state_visibility(state: Mapping[str, Any], visibility: Mapping[str, Any]) -> None:
    leaves = _state_leaves(state)
    for path, audience in visibility.items():
        if path not in leaves:
            raise ContextBundleError("stateVisibility 必须指向现有叶子字段：" + path)
        if not isinstance(audience, str) or audience not in _VISIBILITIES:
            raise ContextBundleError("stateVisibility 可见性无效：" + path)


def _player_state(state: Mapping[str, Any], visibility: Mapping[str, Any]) -> Dict[str, Any]:
    omitted = object()

    def select(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                escaped = key.replace("~", "~0").replace("/", "~1")
                selected = select(child, path + "/" + escaped)
                if selected is not omitted:
                    result[key] = selected
            return result if result else omitted
        if isinstance(value, list):
            selected = [select(child, path + "/" + str(i)) for i, child in enumerate(value)]
            # Preserve indices; a partially hidden array is omitted as a whole.
            return selected if selected and all(child is not omitted for child in selected) else omitted
        return value if visibility.get(path) in _PLAYER_VISIBLE else omitted

    result = select(state, "")
    return {} if result is omitted else result


def _validate_memory_sources(memories: Iterable[Mapping[str, Any]],
                             evidence: Iterable[Mapping[str, Any]]) -> None:
    sources = {item["sourceId"]: item for item in evidence}
    fact_kinds = {"public_fact", "confirmed_event", "current_beat"}
    for memory in memories:
        for source_id in memory["sourceIds"]:
            source = sources.get(source_id)
            if source is None:
                raise ContextBundleError("dynamicMemory.sourceIds 无法追溯到完整证据：" + source_id)
            if source["kind"] not in fact_kinds:
                raise ContextBundleError("动态记忆来源类型不能支持事实：" + source_id)
            if source["authority"] == "style_only" or (
                memory["authority"] == "authoritative" and source["authority"] != "authoritative"
            ):
                raise ContextBundleError("动态记忆来源权威级别不足：" + source_id)
            if memory["status"] == "confirmed" and source["validity"] != "confirmed":
                raise ContextBundleError("confirmed 动态记忆需要 confirmed 来源：" + source_id)
            # Visibility is not a total order. No implicit declassification.
            if source["visibility"] != memory["visibility"]:
                raise ContextBundleError("动态记忆与来源可见性不一致：" + source_id)
            if source["visibility"] == "character_known":
                character_id = source.get("characterId")
                if not isinstance(character_id, str) or not character_id.strip() or character_id != memory.get("characterId"):
                    raise ContextBundleError("角色认知来源必须绑定同一 characterId：" + source_id)


def _validate_provenance(provenance: Mapping[str, Any]) -> None:
    for key in ("modulePaths", "sourceIds", "excludedSources"):
        value = provenance.get(key)
        if value is not None and (not isinstance(value, list)
                                  or any(not isinstance(item, str) or not item.strip() for item in value)
                                  or len(value) != len(set(value))):
            raise ContextBundleError(f"provenance.{key} 必须是无重复字符串列表。")
    source = provenance.get("stateVisibilitySource")
    if source is not None and source not in _STATE_VISIBILITY_SOURCES:
        raise ContextBundleError("provenance.stateVisibilitySource 无效。")
    mode = provenance.get("stateVisibilityMode")
    if mode is not None and mode not in STATE_VISIBILITY_MODES:
        raise ContextBundleError("provenance.stateVisibilityMode 无效。")


def _validate_continuity_window(items: Iterable[Mapping[str, Any]], branch_id: str) -> None:
    entries = list(items)
    if len(entries) > _CONTINUITY_WINDOW_LIMIT:
        raise ContextBundleError("continuityWindow 最多保留 2 条已确认回合承接。")
    seen_sources = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ContextBundleError(f"continuityWindow[{index}] 必须是对象。")
        required = ("sourceId", "branchId", "status", "content")
        missing = [key for key in required if key not in item]
        if missing:
            raise ContextBundleError(
                "continuityWindow 项缺少字段：" + ",".join(missing)
            )
        for key in ("sourceId", "branchId", "status"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise ContextBundleError(f"continuityWindow.{key} 必须是非空字符串。")
        if not isinstance(item["content"], str) or not item["content"].strip():
            raise ContextBundleError("continuityWindow.content 必须是非空字符串。")
        if item["branchId"] != branch_id:
            raise ContextBundleError(
                f"continuityWindow 跨越当前分支：{item['sourceId']} ({item['branchId']} != {branch_id})"
            )
        if item["status"] != "confirmed":
            raise ContextBundleError("continuityWindow 只能包含 confirmed 承接。")
        if item["sourceId"] in seen_sources:
            raise ContextBundleError("continuityWindow 的 sourceId 不能重复：" + item["sourceId"])
        seen_sources.add(item["sourceId"])


def _validate_turn_intent(turn_intent: Mapping[str, Any]) -> None:
    selected = turn_intent.get("selectedDirection")
    if selected is None:
        return
    if not isinstance(selected, dict):
        raise ContextBundleError("turnIntent.selectedDirection 必须是对象。")
    for key, maximum in _SELECTED_DIRECTION_LIMITS.items():
        if key not in selected:
            continue
        value = selected[key]
        if not isinstance(value, str) or not value.strip():
            raise ContextBundleError(f"turnIntent.selectedDirection.{key} 必须是非空字符串。")
        if len(value) > maximum:
            raise ContextBundleError(
                f"turnIntent.selectedDirection.{key} 超过 {maximum} 字符上限。"
            )


def _validate_bundle_shape(data: Mapping[str, Any]) -> None:
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise ContextBundleError("bundle schemaVersion 不匹配。")
    for key in ("contextId", "package", "branch", "hardConstraints", "turnIntent",
                "authoritativeState", "stateVisibility", "allowedEvidence", "continuityWindow", "styleGuide",
                "outputContract", "provenance", "budget", "dynamicMemory"):
        if key not in data:
            raise ContextBundleError("bundle 缺少字段：" + key)
    if not isinstance(data["contextId"], str) or not data["contextId"].strip():
        raise ContextBundleError("contextId 必须是非空字符串。")
    for key in ("package", "branch", "hardConstraints", "turnIntent", "authoritativeState",
                "styleGuide", "stateVisibility", "outputContract", "provenance", "budget"):
        _require_mapping(data[key], key)
    _validate_turn_intent(data["turnIntent"])
    _validate_package(data["package"])
    _validate_state_visibility(data["authoritativeState"], data["stateVisibility"])
    branch = data["branch"]
    branch_id = branch.get("branchId") or branch.get("parentBranchId")
    if not isinstance(branch_id, str) or not branch_id.strip():
        raise ContextBundleError("branch 必须提供 branchId 或 parentBranchId。")
    for key in ("branchId", "parentBranchId", "sessionId", "lineageHead"):
        if key in branch and (not isinstance(branch[key], str) or not branch[key].strip()):
            raise ContextBundleError(f"branch.{key} 必须是非空字符串。")
    if (not isinstance(data["allowedEvidence"], list)
            or not isinstance(data["continuityWindow"], list)
            or not isinstance(data["dynamicMemory"], list)):
        raise ContextBundleError("allowedEvidence、continuityWindow 和 dynamicMemory 必须是列表。")
    seen = set()
    for item in data["allowedEvidence"]:
        if not isinstance(item, dict):
            raise ContextBundleError("allowedEvidence 必须只包含对象。")
        if item.get("sourceId") in seen:
            raise ContextBundleError("allowedEvidence 的 sourceId 不能重复：" + str(item.get("sourceId")))
        seen.add(item.get("sourceId"))
        _validate_evidence(item, branch_id)
    memory_ids = set()
    for item in data["dynamicMemory"]:
        if not isinstance(item, dict):
            raise ContextBundleError("dynamicMemory 必须只包含对象。")
        if item.get("memoryId") in memory_ids:
            raise ContextBundleError("dynamicMemory 的 memoryId 不能重复：" + str(item.get("memoryId")))
        memory_ids.add(item.get("memoryId"))
        _validate_memory(item, branch_id)
    _validate_continuity_window(data["continuityWindow"], branch_id)
    _validate_provenance(data["provenance"])
    _validate_memory_sources(data["dynamicMemory"], data["allowedEvidence"])


@dataclass(frozen=True)
class ContextBundle:
    """A validated, hashable context snapshot for one narrative turn."""

    schema_version: str
    context_id: str
    package: Dict[str, Any]
    branch: Dict[str, Any]
    hard_constraints: Dict[str, Any]
    turn_intent: Dict[str, Any]
    authoritative_state: Dict[str, Any]
    state_visibility: Dict[str, str]
    allowed_evidence: tuple[Dict[str, Any], ...]
    continuity_window: tuple[Dict[str, Any], ...]
    dynamic_memory: tuple[Dict[str, Any], ...]
    style_guide: Dict[str, Any]
    output_contract: Dict[str, Any]
    provenance: Dict[str, Any]
    budget: Dict[str, Any]
    context_sha256: str
    parent_context_id: Optional[str] = None

    @classmethod
    def create(
        cls,
        *,
        context_id: str,
        package: Mapping[str, Any],
        branch: Mapping[str, Any],
        hard_constraints: Mapping[str, Any],
        turn_intent: Mapping[str, Any],
        authoritative_state: Mapping[str, Any],
        state_visibility: Optional[Mapping[str, str]] = None,
        allowed_evidence: Iterable[Mapping[str, Any]],
        continuity_window: Iterable[Mapping[str, Any]] = (),
        dynamic_memory: Iterable[Mapping[str, Any]] = (),
        style_guide: Optional[Mapping[str, Any]] = None,
        output_contract: Optional[Mapping[str, Any]] = None,
        provenance: Optional[Mapping[str, Any]] = None,
        budget: Optional[Mapping[str, Any]] = None,
        parent_context_id: Optional[str] = None,
    ) -> "ContextBundle":
        if not isinstance(context_id, str) or not context_id.strip():
            raise ContextBundleError("contextId 必须是非空字符串。")
        branch_data = _require_mapping(_json_value(branch), "branch")
        branch_id = branch_data.get("branchId") or branch_data.get("parentBranchId")
        if not isinstance(branch_id, str) or not branch_id.strip():
            raise ContextBundleError("branch 必须提供 branchId 或 parentBranchId。")
        evidence = tuple(_json_value(item) for item in allowed_evidence)
        continuity = tuple(_json_value(item) for item in continuity_window)
        memories = tuple(_json_value(item) for item in dynamic_memory)
        package_data = _require_mapping(_json_value(package), "package")
        _validate_package(package_data)
        provenance_data = _require_mapping(_json_value(provenance or {}), "provenance")
        provenance_data.setdefault("stateVisibilitySource", "explicit")
        _validate_provenance(provenance_data)
        if parent_context_id is not None and (not isinstance(parent_context_id, str) or not parent_context_id.strip()):
            raise ContextBundleError("parentContextId 必须是非空字符串。")
        for item in evidence:
            _validate_evidence(item, branch_id)
        if len({item.get("sourceId") for item in evidence}) != len(evidence):
            raise ContextBundleError("allowedEvidence 的 sourceId 不能重复。")
        for item in memories:
            _validate_memory(item, branch_id)
        if len({item.get("memoryId") for item in memories}) != len(memories):
            raise ContextBundleError("dynamicMemory 的 memoryId 不能重复。")
        known_source_ids = {str(item.get("sourceId")) for item in evidence}
        for item in memories:
            unknown_sources = [source_id for source_id in item["sourceIds"] if source_id not in known_source_ids]
            if unknown_sources:
                raise ContextBundleError(
                    "dynamicMemory.sourceIds 无法追溯：" + ",".join(unknown_sources)
                )
        _validate_memory_sources(memories, evidence)
        _validate_continuity_window(continuity, branch_id)
        state_data = _require_mapping(_json_value(authoritative_state), "authoritativeState")
        state_visibility_data = _require_mapping(_json_value(state_visibility or {}), "stateVisibility")
        _validate_state_visibility(state_data, state_visibility_data)
        bundle_data: Dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "contextId": context_id,
            "package": package_data,
            "branch": branch_data,
            "hardConstraints": _require_mapping(_json_value(hard_constraints), "hardConstraints"),
            "turnIntent": _require_mapping(_json_value(turn_intent), "turnIntent"),
            "authoritativeState": state_data,
            "stateVisibility": state_visibility_data,
            "allowedEvidence": list(evidence),
            "continuityWindow": list(continuity),
            "dynamicMemory": list(memories),
            "styleGuide": _require_mapping(_json_value(style_guide or {}), "styleGuide"),
            "outputContract": _require_mapping(_json_value(output_contract or {}), "outputContract"),
            "provenance": provenance_data,
            "budget": _require_mapping(_json_value(budget or {}), "budget"),
        }
        _validate_turn_intent(bundle_data["turnIntent"])
        if parent_context_id is not None:
            bundle_data["parentContextId"] = parent_context_id
        # Keep direct construction and rehydration on the same schema boundary.
        # Callers must not be able to create an object that only fails later at
        # validate_bundle().
        _validate_bundle_shape(bundle_data)
        digest = canonical_sha256(bundle_data)
        return cls(
            schema_version=SCHEMA_VERSION,
            context_id=context_id,
            package=_freeze(bundle_data["package"]),
            branch=_freeze(bundle_data["branch"]),
            hard_constraints=_freeze(bundle_data["hardConstraints"]),
            turn_intent=_freeze(bundle_data["turnIntent"]),
            authoritative_state=_freeze(bundle_data["authoritativeState"]),
            state_visibility=_freeze(bundle_data["stateVisibility"]),
            allowed_evidence=_freeze(tuple(bundle_data["allowedEvidence"])),
            continuity_window=_freeze(tuple(bundle_data["continuityWindow"])),
            dynamic_memory=_freeze(tuple(bundle_data["dynamicMemory"])),
            style_guide=_freeze(bundle_data["styleGuide"]),
            output_contract=_freeze(bundle_data["outputContract"]),
            provenance=_freeze(bundle_data["provenance"]),
            budget=_freeze(bundle_data["budget"]),
            context_sha256=digest,
            parent_context_id=parent_context_id,
        )

    def as_dict(self, *, include_hash: bool = True) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "contextId": self.context_id,
            "package": _thaw(self.package),
            "branch": _thaw(self.branch),
            "hardConstraints": _thaw(self.hard_constraints),
            "turnIntent": _thaw(self.turn_intent),
            "authoritativeState": _thaw(self.authoritative_state),
            "stateVisibility": _thaw(self.state_visibility),
            "allowedEvidence": _thaw(self.allowed_evidence),
            "continuityWindow": _thaw(self.continuity_window),
            "dynamicMemory": _thaw(self.dynamic_memory),
            "styleGuide": _thaw(self.style_guide),
            "outputContract": _thaw(self.output_contract),
            "provenance": _thaw(self.provenance),
            "budget": _thaw(self.budget),
        }
        if self.parent_context_id is not None:
            value["parentContextId"] = self.parent_context_id
        if include_hash:
            value["contextSha256"] = self.context_sha256
        return value

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextBundle":
        """Rehydrate and revalidate a serialized bundle at a model boundary."""
        if not isinstance(data, dict):
            raise ContextBundleError("bundle 必须是对象。")
        _validate_bundle_shape(data)
        bundle = cls.create(
            context_id=data["contextId"], package=data["package"], branch=data["branch"],
            hard_constraints=data["hardConstraints"], turn_intent=data["turnIntent"],
            authoritative_state=data["authoritativeState"], allowed_evidence=data["allowedEvidence"],
            state_visibility=data["stateVisibility"],
            continuity_window=data["continuityWindow"], style_guide=data["styleGuide"],
            output_contract=data["outputContract"], provenance=data["provenance"],
            budget=data["budget"], dynamic_memory=data["dynamicMemory"],
            parent_context_id=data.get("parentContextId"),
        )
        if data.get("contextSha256") != bundle.context_sha256:
            raise ContextBundleError("contextSha256 与 bundle 内容不匹配。")
        return bundle

    def project(self, stage: str) -> Dict[str, Any]:
        if stage not in _PROJECTIONS:
            raise ContextBundleError("未知上下文投影：" + stage)
        base = {
            "schemaVersion": self.schema_version,
            "contextId": self.context_id,
            "contextSha256": self.context_sha256,
            "stage": stage,
            "package": _thaw(self.package),
            "branch": _thaw(self.branch),
            "hardConstraints": _thaw(self.hard_constraints),
            "turnIntent": _thaw(self.turn_intent),
            "authoritativeState": _player_state(self.authoritative_state, self.state_visibility)
            if stage in {"result_contract", "chapter", "repair"}
            else _thaw(self.authoritative_state),
            "outputContract": _thaw(self.output_contract),
        }
        if self.parent_context_id is not None:
            base["parentContextId"] = self.parent_context_id
        # Runtime memory proofs remain in the full bundle and review stages.
        # Writer/repair receive their short fact once, via dynamicMemory.
        memory_sources = {
            source_id for item in self.dynamic_memory
            if item.get("status") == "confirmed" and item.get("visibility") in _PLAYER_VISIBLE
            for source_id in item.get("sourceIds", [])
            if source_id.startswith(("memory:outcome:", "memory:item:", "memory:goal:", "memory:thread:"))
        }
        # Opening excerpts document earlier source events; the curated opening
        # fields describe the entry snapshot. Mixing both as writer facts can
        # restore an obsolete item position. Keep excerpts in the frozen bundle
        # for extraction/review, but supply only the curated opening to generation.
        generation_evidence = [item for item in self.allowed_evidence
                               if item.get("location") != "module.openingContext.evidence"]
        if stage == "result_contract":
            base["allowedEvidence"] = _thaw([
                item for item in generation_evidence
                if item.get("authority") != "style_only"
                and item.get("visibility") in _PLAYER_VISIBLE
            ])
        elif stage == "chapter":
            continuity_sources = {item['sourceId'] for item in self.continuity_window}
            base.update({
                "allowedEvidence": _thaw([
                    item for item in generation_evidence
                    if item.get("authority") != "style_only"
                    and item.get("visibility") in _PLAYER_VISIBLE
                    and item.get("sourceId") not in memory_sources
                    and item.get("sourceId") not in continuity_sources
                ]),
                "continuityWindow": _thaw(self.continuity_window),
                "dynamicMemory": _thaw([
                    item for item in self.dynamic_memory
                    if item.get("status") == "confirmed"
                    and item.get("visibility") in _PLAYER_VISIBLE
                ]),
                "styleGuide": _thaw(self.style_guide),
            })
        elif stage == "fact_extract":
            base["allowedEvidence"] = _thaw(self.allowed_evidence)
            base["provenance"] = _thaw(self.provenance)
        elif stage == "grounding_review":
            base["allowedEvidence"] = _thaw(self.allowed_evidence)
            base["dynamicMemory"] = _thaw(self.dynamic_memory)
            base["provenance"] = _thaw(self.provenance)
        elif stage == "repair":
            base["allowedEvidence"] = _thaw([
                item for item in generation_evidence
                if item.get("authority") != "style_only"
                and item.get("visibility") in _PLAYER_VISIBLE
                and item.get("sourceId") not in memory_sources
            ])
            base["continuityWindow"] = _thaw(self.continuity_window)
            base["dynamicMemory"] = _thaw([
                item for item in self.dynamic_memory
                if item.get("status") == "confirmed"
                and item.get("visibility") in _PLAYER_VISIBLE
            ])
        return base


def validate_bundle(bundle: ContextBundle) -> None:
    """Re-check a bundle at a model boundary."""
    if not isinstance(bundle, ContextBundle):
        raise ContextBundleError("必须提供 ContextBundle。")
    _validate_bundle_shape(bundle.as_dict(include_hash=False))
    expected = canonical_sha256(bundle.as_dict(include_hash=False))
    if expected != bundle.context_sha256:
        raise ContextBundleError("contextSha256 与 bundle 内容不匹配。")


class ContextBundleBuilder:
    """Build a deterministic bundle from an already bounded module context.

    The builder does not load modules itself unless a resolver is supplied.
    Callers provide the branch identity and the already expanded leaf
    visibility map; package-level declaration loading remains at the audit
    entrypoint so an incomplete state projection fails closed.
    """

    def __init__(self, resolver: Any = None) -> None:
        self.resolver = resolver

    @staticmethod
    def default_state_visibility(state: Mapping[str, Any]) -> Dict[str, str]:
        """Return the conservative player-state whitelist used by the runtime.

        StoryPackage state fields without an explicit visibility declaration
        remain hidden. The player may see their own identity and location;
        other character locations, source progress and internal ledgers stay
        out of player projections.
        """
        visibility: Dict[str, str] = {}
        for field in ("playerCharacterId", "playerLocationId"):
            if field in state and not isinstance(state[field], (dict, list)):
                visibility["/" + field] = "player_known"
        player_id = state.get("playerCharacterId")
        locations = state.get("characterLocationIds")
        if isinstance(player_id, str) and isinstance(locations, dict) and player_id in locations:
            escaped = player_id.replace("~", "~0").replace("/", "~1")
            visibility["/characterLocationIds/" + escaped] = "player_known"
        return visibility

    @classmethod
    def project_selected_state_patch(
        cls, selected: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], list[str]]:
        """Remove state mutations that are committed by consequence handling.

        The caller must invoke this only after the complete selected patch has
        passed the authoritative state transition. The returned selection is
        the smaller patch that this bundle can represent.
        """
        projected = _json_value(selected)
        if not isinstance(projected, dict):
            raise ContextBundleError("selected 必须是对象。")
        patch = projected.get("statePatch", {})
        if patch is None:
            patch = {}
        if not isinstance(patch, dict):
            return projected, []
        excluded_fields = {
            "branchLedger", "freeTextProgress", "characterOutcomeStates", "goalLedger",
            "threadLedger", "readerEntityStates", "derivedAdditions",
        }
        excluded = sorted(set(patch).intersection(excluded_fields))
        projected["statePatch"] = {
            key: value for key, value in patch.items() if key not in excluded_fields
        }
        return projected, excluded

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @classmethod
    def _entity_context(cls, module_context: Mapping[str, Any]) -> Dict[str, list[Dict[str, Any]]]:
        """Keep resolver entities bounded to stable identity and known detail."""
        character_details = {
            cls._text(item.get("name")): cls._text(item.get("detail"))
            for item in module_context.get("characterDetails", [])
            if isinstance(item, dict) and cls._text(item.get("name")) and cls._text(item.get("detail"))
        }
        result: Dict[str, list[Dict[str, Any]]] = {}
        for field in ("characters", "locations", "items"):
            raw = module_context.get(field, [])
            if not isinstance(raw, list):
                raise ContextBundleError(f"module_context.{field} 必须是数组。")
            entities: list[Dict[str, Any]] = []
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    raise ContextBundleError(f"module_context.{field}[{index}] 必须是对象。")
                entity_id = cls._text(item.get("id"))
                name = cls._text(item.get("name"))
                if not entity_id or not name:
                    raise ContextBundleError(f"module_context.{field}[{index}] 缺少可追溯的 id/name。")
                projected = {"id": entity_id, "name": name}
                if field == "characters" and name in character_details:
                    projected["detail"] = character_details[name]
                entities.append(projected)
            result[field] = entities
        return result

    @classmethod
    def _immutable_facts(cls, module_context: Mapping[str, Any]) -> list[Dict[str, Any]]:
        """Project package hard facts into stable, addressable constraints."""
        world = module_context.get("world", {})
        raw = world.get("immutableFacts", []) if isinstance(world, Mapping) else []
        if not isinstance(raw, list):
            raise ContextBundleError("module_context.world.immutableFacts 必须是数组。")
        facts: list[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(raw):
            if isinstance(item, str) and item.strip():
                text = item.strip()
                fact_id = "world-fact-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                if fact_id in seen_ids:
                    raise ContextBundleError("module_context.world.immutableFacts 存在重复 id：" + fact_id)
                seen_ids.add(fact_id)
                facts.append({"id": fact_id, "text": text, "authority": "hard_constraint"})
                continue
            if not isinstance(item, Mapping):
                continue
            fact_id = cls._text(item.get("id"))
            text = cls._text(item.get("text") or item.get("content"))
            if not fact_id or not text:
                continue
            if fact_id in seen_ids:
                raise ContextBundleError("module_context.world.immutableFacts 存在重复 id：" + fact_id)
            seen_ids.add(fact_id)
            projected = {"id": fact_id, "text": text, "authority": "hard_constraint"}
            for key in ("sourceChapterId", "sourceProgress", "lineRange"):
                if key in item:
                    projected[key] = _json_value(item[key])
            facts.append(projected)
        return facts

    @classmethod
    def _validated_state_patch(cls, selected_data: Mapping[str, Any], state: Mapping[str, Any]) -> Dict[str, Any]:
        """Accept only a patch that can be checked against the current state shape."""
        patch = selected_data.get("statePatch", {})
        if patch is None:
            patch = {}
        if not isinstance(patch, dict):
            raise ContextBundleError("selected.statePatch 必须是对象。")
        private_fields = {
            "branchLedger", "freeTextProgress", "characterOutcomeStates", "goalLedger",
            "threadLedger", "readerEntityStates",
        }
        invalid_private = sorted(private_fields.intersection(patch))
        if invalid_private:
            raise ContextBundleError("selected.statePatch 不得直接修改受保护状态：" + "、".join(invalid_private))
        unknown = sorted(set(patch) - set(state))
        if unknown:
            raise ContextBundleError("selected.statePatch 引用了未声明状态字段：" + "、".join(unknown))
        for field, value in patch.items():
            if field == "characterLocationIds":
                if not isinstance(value, dict) or not isinstance(state.get(field), dict):
                    raise ContextBundleError("selected.statePatch.characterLocationIds 必须是对象。")
                if any(not isinstance(key, str) or not isinstance(location, str)
                       for key, location in value.items()):
                    raise ContextBundleError("selected.statePatch.characterLocationIds 必须是字符串映射。")
                continue
            if isinstance(value, (dict, list)):
                raise ContextBundleError(f"selected.statePatch.{field} 不得使用对象或数组。")
            if value is not None and type(value) is not type(state[field]):
                raise ContextBundleError(f"selected.statePatch.{field} 类型必须与当前状态一致。")
        return _json_value(patch)

    @classmethod
    def _evidence_items(cls, module_context: Mapping[str, Any], lineage: Iterable[Mapping[str, Any]], branch_id: str) -> list[Dict[str, Any]]:
        result: list[Dict[str, Any]] = []
        seen: set[str] = set()

        def add(source_id: Any, kind: str, visibility: str, content: Any,
                location: str, *, authority: str = "confirmed_evidence") -> None:
            source_id = cls._text(source_id)
            content = cls._text(content)
            if not source_id or not content or source_id in seen:
                return
            seen.add(source_id)
            result.append({
                "sourceId": source_id,
                "kind": kind,
                "visibility": visibility,
                "branchId": "package" if location.startswith("module.") else branch_id,
                "location": location,
                "content": content,
                "authority": authority,
                "validity": "confirmed",
            })

        current_beat = module_context.get("currentBeat", {})
        if isinstance(current_beat, dict):
            current_id = cls._text(current_beat.get("id"))
            if current_id:
                add("module:currentBeat:" + current_id, "current_beat",
                    "public_world_fact", current_beat.get("summary"), "module.currentBeat")
        world = module_context.get("world", {})
        if isinstance(world, dict):
            for fact in world.get("immutableFacts", []):
                if not isinstance(fact, dict):
                    continue
                fact_id = fact.get("id")
                if cls._text(fact_id):
                    add("module:fact:" + cls._text(fact_id), "public_fact", "public_world_fact",
                        fact.get("text") or fact.get("description"), "module.world.immutableFacts")
        for field, kind, visibility in (
            ("narrativeBrief", "recent_prose", "player_known"),
            ("priorNarrativeBrief", "recent_prose", "player_known"),
            ("characterIdentityEvidence", "public_fact", "public_world_fact"),
        ):
            for item in module_context.get(field, []):
                if not isinstance(item, dict):
                    continue
                item_id = item.get("evidenceParagraphId")
                if not isinstance(item_id, str) or not item_id.strip():
                    continue
                add(f"module:{field}:{item_id}", kind, visibility,
                    item.get("text") or item.get("detail") or item, f"module.{field}")
        for item in module_context.get("sourceDialogueContext", []):
            if not isinstance(item, dict):
                continue
            item_id = item.get("dialogueParagraphId")
            if not isinstance(item_id, str) or not item_id.strip():
                continue
            add(f"module:dialogue:{item_id}", "source_dialogue", "character_known",
                item, "module.sourceDialogueContext")
        previous_summaries = module_context.get("previousBeatSummaries", [])
        previous_ids = module_context.get("previousBeatSummaryIds", [])
        if isinstance(previous_summaries, list) and isinstance(previous_ids, list):
            for index, summary in enumerate(previous_summaries):
                source_id = previous_ids[index] if index < len(previous_ids) else None
                if cls._text(source_id):
                    add(f"module:previousBeat:{cls._text(source_id)}", "recent_prose", "player_known",
                        summary, "module.previousBeatSummaries")
        opening = module_context.get("openingContext", {})
        if isinstance(opening, dict):
            for field in ("identity", "appearance"):
                add(f"module:opening:{field}", "public_fact", "player_known",
                    opening.get(field), f"module.openingContext.{field}")
            for field in ("knownFacts", "relationships", "visibleItems", "evidence"):
                for index, item in enumerate(opening.get(field, [])):
                    add(f"module:opening:{field}:{index}", "public_fact", "player_known",
                        item, f"module.openingContext.{field}")
        for index, node in enumerate(list(lineage)[-2:]):
            if not isinstance(node, dict):
                continue
            summary = node.get("summary") or (node.get("readerOutcome") or {}).get("action", {}).get("summary")
            node_id = cls._text(node.get("id"))
            if node_id:
                add(f"branch:lineage:{node_id}", "continuity_summary", "player_known",
                    summary, "branch.lineage")
        return result

    @classmethod
    def _continuity_window(
        cls,
        module_context: Mapping[str, Any],
        lineage: Iterable[Mapping[str, Any]],
        branch_id: str,
    ) -> list[Dict[str, Any]]:
        lineage_candidates: list[Dict[str, Any]] = []
        for index, node in enumerate(list(lineage)[-2:]):
            if not isinstance(node, dict):
                continue
            summary = node.get("summary") or (node.get("readerOutcome") or {}).get("action", {}).get("summary")
            node_id = cls._text(node.get("id"))
            if node_id and isinstance(summary, str) and summary.strip():
                lineage_candidates.append({
                    "sourceId": f"branch:lineage:{node_id}",
                    "branchId": branch_id,
                    "status": "confirmed",
                    "kind": "confirmed_event",
                    "content": summary.strip(),
                })
        module_candidates: list[Dict[str, Any]] = []
        previous_summaries = module_context.get("previousBeatSummaries", [])
        previous_ids = module_context.get("previousBeatSummaryIds", [])
        if not isinstance(previous_summaries, list) or not isinstance(previous_ids, list):
            previous_summaries, previous_ids = [], []
        start = max(0, len(previous_summaries) - 2)
        for index in range(start, len(previous_summaries)):
            summary = previous_summaries[index]
            source_id = previous_ids[index] if index < len(previous_ids) else None
            if cls._text(source_id) and isinstance(summary, str) and summary.strip():
                module_candidates.append({
                    "sourceId": f"module:previousBeat:{cls._text(source_id)}",
                    "branchId": branch_id,
                    "status": "confirmed",
                    "kind": "recent_prose",
                    "content": summary.strip(),
                })
        # Lineage is the newest route-specific source, so it wins when the
        # module also supplies more than the two-entry short-term budget.
        deduplicated: Dict[str, Dict[str, Any]] = {}
        for item in module_candidates + lineage_candidates:
            deduplicated[item["sourceId"]] = item
        return list(deduplicated.values())[-_CONTINUITY_WINDOW_LIMIT:]

    def build(
        self,
        *,
        context: Mapping[str, Any],
        selected: Mapping[str, Any],
        state: Mapping[str, Any],
        branch: Mapping[str, Any],
        state_visibility: Mapping[str, str],
        context_id: Optional[str] = None,
        parent_context_id: Optional[str] = None,
        module_context: Optional[Mapping[str, Any]] = None,
        validated_state_patch: Optional[Mapping[str, Any]] = None,
        dynamic_memory: Iterable[Mapping[str, Any]] = (),
        memory_evidence: Iterable[Mapping[str, Any]] = (),
        style_guide: Optional[Mapping[str, Any]] = None,
        budget: Optional[Mapping[str, Any]] = None,
        state_visibility_source: str = "explicit",
        state_visibility_mode: Optional[str] = None,
    ) -> ContextBundle:
        if module_context is None:
            if self.resolver is None:
                raise ContextBundleError("ContextBundleBuilder 缺少 module_context 或 resolver。")
            module_context = self.resolver.resolve(dict(context), dict(selected), dict(state))
        if not isinstance(module_context, Mapping):
            raise ContextBundleError("module_context 必须是对象。")
        package = context.get("package")
        if not isinstance(package, Mapping):
            raise ContextBundleError("builder context.package 必须是对象。")
        branch_data = _require_mapping(_json_value(branch), "branch")
        branch_id = branch_data.get("branchId") or branch_data.get("parentBranchId")
        if not isinstance(branch_id, str) or not branch_id.strip():
            raise ContextBundleError("builder branch 必须提供 branchId 或 parentBranchId。")
        if state_visibility_source not in _STATE_VISIBILITY_SOURCES:
            raise ContextBundleError("builder state_visibility_source 无效。")
        if state_visibility_mode is not None and state_visibility_mode not in STATE_VISIBILITY_MODES:
            raise ContextBundleError("builder state_visibility_mode 无效。")
        selected_data = _json_value(selected)
        raw_input = (context.get("playerDirection") or selected_data.get("summary")
                     or selected_data.get("title"))
        if not isinstance(raw_input, str) or not raw_input.strip():
            raise ContextBundleError("builder 缺少玩家原始输入。")
        action_contract = _json_value(module_context.get("actionContract", {}))
        if not isinstance(action_contract, dict):
            action_contract = {}
        if context.get('narrativePolicy') == 'context_only' and selected_data.get('isFreeText'):
            # An unchanged source cursor is a reference, not an instruction to
            # replay or remain inside that authored beat. State checks still
            # validate every proposed consequence of the player's actual input.
            action_contract = {
                'kind': 'player_input',
                'instruction': '按本次完整输入推进行动及即时回应；等待允许时间经过并得到回应。新的重要玩家决定才是停点。',
            }
        lineage = context.get("lineage", [])
        if not isinstance(lineage, list):
            lineage = []
        if context.get('narrativePolicy') == 'context_only':
            lineage = focused_recent_lineage(lineage, raw_input)
        state_data = _json_value(state)
        if not isinstance(state_data, dict):
            raise ContextBundleError("builder state 必须是对象。")
        state_data.pop("branchLedger", None)
        state_data.pop("freeTextProgress", None)
        selected_patch = selected_data.get("statePatch", {})
        if selected_patch is None:
            selected_patch = {}
        if not isinstance(selected_patch, dict):
            raise ContextBundleError("selected.statePatch 必须是对象。")
        if selected_patch and validated_state_patch is None:
            raise ContextBundleError("selected.statePatch 必须先由状态层校验并通过 validated_state_patch 传入。")
        patch_to_validate = selected_patch if validated_state_patch is None else validated_state_patch
        selected_state_patch = self._validated_state_patch({"statePatch": patch_to_validate}, state_data)
        if _json_value(selected_patch) != selected_state_patch:
            raise ContextBundleError("validated_state_patch 必须与 selected.statePatch 一致。")
        current_beat = _json_value(module_context.get("currentBeat", {}))
        current_chapter = _json_value(module_context.get("currentChapter", {}))
        world = module_context.get("world", {})
        if not isinstance(world, dict):
            world = {}
        hard_constraints = {
            "globalConstraints": _json_value(world.get("globalConstraints", [])),
            "immutableFacts": self._immutable_facts(module_context),
            "currentChapter": current_chapter,
            "currentBeat": current_beat,
            "actionContract": action_contract,
            "branchRules": {"currentBranchOnly": True, "futureBeatsExcluded": True},
            "stopPoint": selected_data.get("stopPoint") or action_contract.get("stopPoint"),
            "entityContext": self._entity_context(module_context),
        }
        if context.get('narrativePolicy') == 'context_only':
            from .narrative_delivery import continuity_context
            from .entity_facts import turn_entity_facts
            hard_constraints.update(continuity_context(context.get('parent', {}), raw_input))
            hard_constraints['entityFacts'] = turn_entity_facts(
                hard_constraints['entityContext'], state_data, raw_input,
                '\n'.join(n.get('narrativeText', '') for n in context.get('lineage', [])[-2:]))
        opening = module_context.get("openingContext", {})
        if isinstance(opening, dict):
            hard_constraints["openingKnowledgeBoundaries"] = _json_value(opening.get("unknownBoundaries", []))
            hard_constraints["continuityContract"] = _json_value(module_context.get("continuityContract", {}))
        selected_direction = {
            key: _json_value(selected_data[key])
            for key in ("id", "title", "summary")
            if key in selected_data and selected_data[key] is not None
        }
        turn_intent = {
            "rawInput": raw_input,
            "selectedDirection": selected_direction,
            "atomicRequirements": _json_value(selected_data.get("atomicRequirements") or [{
                "id": "input-1", "text": raw_input, "status": "unknown", "source": "player_input",
            }]),
            "stopPoint": selected_data.get("stopPoint") or action_contract.get("stopPoint"),
        }
        output_contract = {
            "actionContract": action_contract,
            "selectedStatePatch": selected_state_patch,
            "mustStop": True,
        }
        evidence = self._evidence_items(module_context, lineage, branch_id)
        evidence.extend(_json_value(item) for item in memory_evidence)
        source_ids = [item["sourceId"] for item in evidence]
        module_paths = sorted({str(path) for path in module_context.get("modulePaths", []) if isinstance(path, str)})
        provenance = {
            "modulePaths": module_paths,
            "sourceIds": source_ids,
            "excludedSources": ["future-beats", "sibling-branches", "reader-original", "unmarked-author-truth"],
            "stateVisibilitySource": state_visibility_source,
        }
        if state_visibility_mode is not None:
            provenance["stateVisibilityMode"] = state_visibility_mode
        semantic_seed = {
            "package": {"id": package.get("id"), "version": package.get("version")},
            "branch": branch_data,
            "selected": selected_data,
            "state": state_data,
            "modulePaths": module_paths,
        }
        stable_id = context_id or "ctx-" + hashlib.sha256(
            json.dumps(semantic_seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return ContextBundle.create(
            context_id=stable_id,
            parent_context_id=parent_context_id,
            package={"id": package.get("id"), "version": package.get("version")},
            branch=branch_data,
            hard_constraints=hard_constraints,
            turn_intent=turn_intent,
            authoritative_state=state_data,
            state_visibility=state_visibility,
            allowed_evidence=evidence,
            continuity_window=self._continuity_window(module_context, lineage, branch_id),
            dynamic_memory=dynamic_memory,
            style_guide=style_guide or {},
            output_contract=output_contract,
            provenance=provenance,
            budget=budget or {},
        )
