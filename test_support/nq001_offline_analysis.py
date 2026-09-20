"""Re-analyse a retained NQ-001 run without starting the API or calling a model."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import quote

from test_support.nq001_live_eval import _sse_payload


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_only_body(path: Path) -> Dict[str, Any]:
    uri = f"file:{quote(str(path))}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("SELECT body FROM turn_drafts ORDER BY updated DESC LIMIT 1").fetchone()
    if not row:
        return {}
    return json.loads(row[0])


def _audit_item(attempt: Dict[str, Any]) -> Dict[str, Any]:
    audits = attempt.get("failure_audits") or []
    audit = next((item for group in audits if isinstance(group, list) for item in group if isinstance(item, dict)), {})
    observations = audit.get("callObservations") or []
    transport = [item for item in observations if isinstance(item, dict) and isinstance(item.get("transport"), dict)]
    stage_counts = Counter(item.get("generationStage", "Unknown") for item in transport)
    provider_total = 0
    provider_receipts = 0
    raw_lines = 0
    for line in str(audit.get("rawResponse", "")).splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        raw_lines += 1
        usage = value.get("usage") if isinstance(value, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            provider_receipts += 1
            provider_total += usage["total_tokens"]
    failures = [
        {"stage": item.get("failureStage"), "code": item.get("failureCode"), "reason": item.get("failureReason")}
        for item in observations if isinstance(item, dict) and item.get("failureStage")
    ]
    return {
        "metrics": attempt.get("metrics", {}),
        "provider_receipts": provider_receipts,
        "provider_reported_tokens": provider_total,
        "raw_provider_records": raw_lines,
        "call_stage_counts": dict(stage_counts),
        "failure_records": failures,
        "audit_error": audit.get("error"),
        "preview_body_retained": bool(audit.get("retainedDraft")),
    }


def analyse(evidence_dir: Path, draft_db: Path, main_db: Path) -> Dict[str, Any]:
    calls = [_read_json(path) for path in sorted((evidence_dir / "calls").glob("*.json"))]
    stream_calls = []
    for call in calls:
        raw = call.get("response", {}).get("raw", "")
        if "event:" not in raw:
            continue
        parsed = _sse_payload(raw)
        stream_calls.append({
            "stage": call.get("stage"),
            "request_id": call.get("request_id"),
            "events": Counter(event.event for event in parsed.events),
            "preview_cjk": len(parsed.preview_text),
            "terminal_event": parsed.terminal_event,
            "formal_commit": parsed.formal_commit,
            "failure_code": parsed.failure_code,
            "transport_full_body_ms": call.get("timing", {}).get("full_body_ms"),
        })
    draft = _read_only_body(draft_db)
    attempts = draft.get("previous_attempts", []) + [draft]
    attempt_reports = [_audit_item(item) for item in attempts]
    stage_counts = Counter()
    calls_total = 0
    reported_tokens = 0
    for item in attempt_reports:
        stage_counts.update(item["call_stage_counts"])
        metrics = item["metrics"]
        calls_total += int(metrics.get("calls", 0) or 0)
        reported_tokens += int(metrics.get("reported_tokens", 0) or 0)

    with sqlite3.connect(f"file:{quote(str(main_db))}?mode=ro", uri=True) as connection:
        table_counts = {}
        for table in ("branch_nodes", "game_sessions", "turn_requests", "game_events", "llm_audits"):
            try:
                table_counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                table_counts[table] = "Unknown"

    return {
        "schema": "nq001-offline-correction/0.1",
        "source": {"evidence_dir": str(evidence_dir), "draft_db": str(draft_db), "main_db": str(main_db)},
        "stream_calls": stream_calls,
        "attempts": attempt_reports,
        "attempt_count": len(attempts),
        "scenario_count": 1,
        "calls_total": calls_total,
        "provider_reported_tokens_total": reported_tokens,
        "call_stage_counts": dict(stage_counts),
        "metrics": {
            "opening": "1/1 formal commit",
            "first_draft_pass_rate": "Unknown",
            "final_success_rate": "0/1",
            "fact_overreach_rate": "Unknown",
            "review_false_block_rate": "Unknown",
            "repair_success_rate": "Unknown",
            "first_token_ms": "Unknown (raw SSE has no event timestamps)",
            "transport_full_body_ms": [item["transport_full_body_ms"] for item in stream_calls if str(item["stage"]).startswith("turn-1-inform")],
            "internal_first_text_ms": [item["metrics"].get("first_text_ms") for item in attempt_reports],
            "internal_complete_ms": [item["metrics"].get("complete_ms") for item in attempt_reports],
            "direction_available_ms": "Unknown",
            "reported_tokens": reported_tokens,
            "calls": calls_total,
        },
        "state_evidence": table_counts,
        "stage_conclusion": {
            "last_successful_stage": "local_repair generation returned completed observations; its validation did not complete",
            "first_failed_stage": "full_review / narrative validation (generationStage=validation, then local_repair_record=failed_full_review)",
            "planning": "completed",
            "body_generation": "completed with unconfirmed preview",
            "fact_review": "completed through grounding, then validation rejected",
            "repair": "attempted; local repair calls completed but review remained pending/failed",
            "state_validation": "not reached",
            "atomic_commit": "not reached",
            "transport": "HTTP 200 SSE; terminal error event, no transport exception evidence",
        },
        "primary_issue": {
            "statement": "场景全审查拒绝了守门弟子对封山禁令、伤者身份、空灯来历和铁链来源的无来源背景/否定性断言；局部修复后仍未通过，因此没有形成可提交 artifact。",
            "evidence": "两次 attempt 的 audit.error、validation failureRecords，以及 raw SSE 的 delta/reset 后 error:generation_failed。",
            "code_locations": [
                "open_story_engine/cocreation.py:2531-2611 (guard/fact review and rejection)",
                "open_story_engine/cocreation.py:2627-2668 (failure and semantic repair path)",
                "open_story_engine/api_play.py:573-599 (draft failure propagated without artifact)",
            ],
            "replay": "python3 -m unittest tests_py.test_nq001_tools；直接将 calls/0007、0008 的 raw SSE 交给 _sse_payload，不启动服务。",
            "observation_gap": "公开 SSE 不提供 planner/reviewer/repair 分阶段事件；若会话1需要逐次定位，需保留脱敏的 stage、attempt、provider request id 与每次 review/repair 结果。",
        },
    }


def _markdown(report: Dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        "# NQ-001 首轮离线更正报告",
        "",
        "本报告由保留的首轮脱敏响应、临时 draft SQLite 和主 SQLite 只读重放生成；没有启动 API，也没有新增真实模型调用。原 `report.md`、`report.json` 和 `calls/` 文件保持不变。两次同 request_id 尝试计为一个场景。",
        "",
        "## SSE 更正",
        "",
        "- 开场：`delta ×3 → done`，含 `session` 和 `branch`，正式提交 `1/1`。",
        "- 告知首次：`delta ×? → reset ×3 → delta → error:generation_failed`，正文只有预览，正式提交 `false`。",
        "- 告知重试：同一 request_id，`delta ×? → reset ×3 → delta → error:generation_failed`，正文只有预览，正式提交 `false`。",
        "- `HTTP 200` 只表示 SSE 传输建立；终端 `error` 优先，流结束没有提交凭证也不会判定成功。",
        "",
        "## 更正后的指标",
        "",
        f"- 首稿通过率：`{m['first_draft_pass_rate']}`。完整首稿审查通过证据未由公开接口给出，保留 Unknown；不能沿用原报告的 `0/1` 作为统计率。",
        f"- 最终回合成功率：`{m['final_success_rate']}`；两次尝试不是两个场景。",
        f"- 事实越权率、审查误拦率、修复成功率：均为 `{m['fact_overreach_rate']}` / `{m['review_false_block_rate']}` / `{m['repair_success_rate']}`。人工缺陷和审查拒绝不能替代总体统计率。",
        f"- 首字时间：`{m['first_token_ms']}`。内部 draft 指标 first_text_ms 为 `{m['internal_first_text_ms']}` ms，和 SSE 首字时间不是同一口径。",
        f"- HTTP 完整响应时间：`{m['transport_full_body_ms']}` ms；内部生成 complete_ms 为 `{m['internal_complete_ms']}` ms。",
        f"- 供应商报告用量：`{m['calls']}` 次、`{m['reported_tokens']}` tokens（首次 17/109690，重试 15/101738）；未把 raw JSON 行数当作调用次数。估算值：Unknown。",
        "",
        "## 32 次调用阶段分布",
        "",
        "| 阶段 | 次数 |",
        "| --- | ---: |",
    ]
    for stage, count in sorted(report["call_stage_counts"].items()):
        lines.append(f"| `{stage}` | {count} |")
    lines += [
        "",
        "规划和正文生成完成；事实提取、行动审查和场景落地完成；第一次失败闸门是 `validation/full_review`。局部修复调用本身返回完成，但修复验证仍 pending/failed。没有到达状态校验或原子提交，HTTP 层没有传输异常证据。",
        "",
        "## 状态与根因边界",
        "",
        "主 SQLite 仍为一个 root `branch_nodes`、零 `turn_requests`、零 `game_events`；两次失败均未写子分支或改变权威状态。",
        "",
        "会话1应优先处理：**场景全审查拒绝无来源的 NPC 背景/否定性知识断言，而现有局部修复循环没有产出可通过的最终正文**。证据支持这是首个失败闸门和最终失败链条；尚不能仅凭公开响应断言是提示、模型还是修复预算的单一根因。需要的最小新增观测是每次 planner/reviewer/repair 的脱敏 request id、stage、review 结果和最终 artifact 校验结果。",
        "",
        "## 原报告结论变化",
        "",
        "- 保持：开场成功、告知最终失败、两次尝试一条场景、211428 supplier-reported tokens、无子分支/权威状态写入、询问和等待未执行。",
        "- 更正：评估器现在读取 top-level `entries`，按独立 `event:`/多行 `data:`/空行边界解析 SSE；预览正文不再被当作正式正文。",
        "- 更正：首稿通过率改为 Unknown；不能从缺少完整阶段审查字段的记录推导 `0/1`。",
        "- 仍为 Unknown：事实越权率、审查误拦率、修复成功率、方向可用时间和 SSE 首字时间。",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--draft-db", required=True, type=Path)
    parser.add_argument("--main-db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = analyse(args.evidence_dir, args.draft_db, args.main_db)
    args.output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
