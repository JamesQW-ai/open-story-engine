"""NQ-001 repeatable live-model evaluation harness.

The harness speaks only the published HTTP surface. It records every request and
response, and reports an explicit contract gap when P0 has not frozen a field.
It never turns a mock run or a partial route into a passing NQ-001 result.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_FILE = Path(__file__).with_name("fixtures") / "nq001-scenarios.json"
SUCCESS_STATUSES = {200, 201, 202}
FAILURE_CODES = {"fact_conflict", "fact_overreach", "narrative_fact_conflict", "state_conflict"}


def _json(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _deep_get(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first(value: Any, *paths: Tuple[str, ...]) -> Any:
    for path in paths:
        result = _deep_get(value, *path)
        if result is not None:
            return result
    return None


def _body_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    result = _first(value, ("narrativeText",), ("narrative_text",), ("prose",), ("body",), ("text",))
    return result if isinstance(result, str) else ""


def _branch(value: Any) -> Optional[Dict[str, Any]]:
    result = _first(value, ("branch",), ("data", "branch"), ("result", "branch"))
    return result if isinstance(result, dict) else None


def _session(value: Any) -> Optional[Dict[str, Any]]:
    result = _first(value, ("session",), ("data", "session"), ("result", "session"))
    return result if isinstance(result, dict) else None


def _directions(value: Any) -> List[Dict[str, Any]]:
    result = _first(value, ("nextDirections",), ("next_directions",), ("directions",), ("choices",))
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


def _error_code(value: Any) -> Optional[str]:
    result = _first(value, ("error", "code"), ("code",), ("reason", "code"))
    return result if isinstance(result, str) else None


def _usage(value: Any) -> Dict[str, Any]:
    usage = _first(value, ("usage",), ("metrics", "usage"), ("audit", "usage"))
    return usage if isinstance(usage, dict) else {}


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: str


class SseEventParser:
    """Incremental parser for the SSE wire format used by the API.

    Event names and data fields are wire fields.  The parser deliberately does
    not infer an event type from JSON payload content: that was the source of
    the previous evaluator's false success classification.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._event = ""
        self._data: List[str] = []
        self.incomplete_event = False

    def _dispatch(self) -> Optional[SseEvent]:
        if not self._event and not self._data:
            return None
        event = self._event or "message"
        value = "\n".join(self._data)
        self._event = ""
        self._data = []
        return SseEvent(event, value)

    def _line(self, line: str) -> Optional[SseEvent]:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if not separator:
            return None
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        # id/retry and unknown fields are intentionally ignored here.
        return None

    def feed(self, chunk: str | bytes) -> List[SseEvent]:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        self._buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
        events: List[SseEvent] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            event = self._line(line)
            if event is not None:
                events.append(event)
        return events

    def finish(self) -> List[SseEvent]:
        # A stream ending without the required blank line is an incomplete
        # event.  It must not become a formal commit merely because data was
        # received.
        if self._buffer:
            self._line(self._buffer)
            self._buffer = ""
        self.incomplete_event = bool(self._event or self._data)
        return []


@dataclass
class SseParseResult:
    events: List[SseEvent] = field(default_factory=list)
    preview_text: str = ""
    done_payload: Any = None
    error_payload: Any = None
    terminal_event: Optional[str] = None
    formal_commit: bool = False
    incomplete_event: bool = False
    first_delta_ms: Optional[float] = None
    done_ms: Optional[float] = None

    @property
    def failure_code(self) -> Optional[str]:
        if isinstance(self.error_payload, dict):
            return _error_code(self.error_payload) or "stream_error"
        if self.error_payload is not None:
            return "stream_error"
        if self.terminal_event is None:
            return "stream_ended_without_terminal"
        if self.terminal_event == "done" and not self.formal_commit:
            return "stream_ended_without_commit"
        return None


def _formal_commit(value: Any) -> bool:
    """Return true only for the published opening/turn commit responses."""
    if not isinstance(value, dict):
        return False
    branch = _branch(value)
    if not isinstance(branch, dict) or not isinstance(_first(branch, ("id",), ("branchId",), ("branch_id",)), str):
        return False
    # Opening responses have a session.  Turn responses are explicitly
    # status=written; status=committed was an obsolete mock-only shape.
    if isinstance(_session(value), dict):
        return True
    return value.get("status") == "written"


def _sse_payload(raw: str, first_token_ms: Optional[float] = None, full_body_ms: Optional[float] = None) -> SseParseResult:
    parser = SseEventParser()
    events = parser.feed(raw)
    parser.finish()
    result = SseParseResult(events=events, incomplete_event=parser.incomplete_event)
    started = time.monotonic()
    for event in events:
        payload = _json(event.data)
        if event.event == "delta":
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                result.preview_text += payload["text"]
            elif isinstance(payload, str):
                result.preview_text += payload
            if result.first_delta_ms is None:
                result.first_delta_ms = round((time.monotonic() - started) * 1000, 3)
        elif event.event == "reset":
            result.preview_text = ""
        elif event.event == "done":
            result.done_payload = payload
            result.terminal_event = "done"
            result.done_ms = round((time.monotonic() - started) * 1000, 3)
        elif event.event == "error":
            result.error_payload = payload
            result.terminal_event = "error"
    result.formal_commit = _formal_commit(result.done_payload)
    if first_token_ms is not None:
        result.first_delta_ms = first_token_ms
    if full_body_ms is not None:
        result.done_ms = full_body_ms
    return result


class HttpEvidenceClient:
    def __init__(self, base_url: str, output: Path, headers: Dict[str, str]):
        self.base_url = base_url.rstrip("/")
        self.output = output
        self.headers = headers
        self.calls = 0
        self.evidence: List[Dict[str, Any]] = []

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]], stage: str, stream: bool = False) -> Dict[str, Any]:
        request_id = str(uuid.uuid4())
        url = self.base_url + path
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = Request(url, data=payload, method=method, headers={**self.headers, "X-NQ001-Request": request_id})
        if stream:
            request.add_header("Accept", "text/event-stream")
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        started = time.monotonic()
        record: Dict[str, Any] = {"request_id": request_id, "stage": stage, "request": {"method": method, "url": url, "body": body}, "response": {}, "timing": {}}
        try:
            with urlopen(request, timeout=180) as response:
                if stream:
                    chunks: List[str] = []
                    first_delta = None
                    stream_started = time.monotonic()
                    wire_parser = SseEventParser()
                    while True:
                        line = response.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", "replace")
                        chunks.append(text)
                        if first_delta is None and any(event.event == "delta" for event in wire_parser.feed(text)):
                            first_delta = round((time.monotonic() - stream_started) * 1000, 3)
                    wire_parser.finish()
                    raw = "".join(chunks)
                else:
                    raw = response.read().decode("utf-8", "replace")
                status = response.status
                response_headers = dict(response.headers.items())
        except HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            status = error.code
            response_headers = dict(error.headers.items())
        except (URLError, TimeoutError, OSError) as error:
            record["failure"] = {"stage": stage, "code": type(error).__name__, "message": str(error)}
            record["timing"] = {"full_body_ms": round((time.monotonic() - started) * 1000, 3)}
            self._save(record)
            return record
        parsed = _json(raw)
        first_token_ms = None
        full_body_ms = round((time.monotonic() - started) * 1000, 3)
        if stream:
            sse = _sse_payload(raw, locals().get("first_delta"), full_body_ms)
            parsed = sse.done_payload if sse.done_payload is not None else (sse.error_payload if sse.error_payload is not None else parsed)
            first_token_ms = sse.first_delta_ms
            if sse.done_ms is not None:
                full_body_ms = sse.done_ms
            record["stream"] = {
                "events": [{"event": item.event, "data": _json(item.data)} for item in sse.events],
                "preview_body": sse.preview_text,
                "terminal_event": sse.terminal_event,
                "formal_commit": sse.formal_commit,
                "incomplete_event": sse.incomplete_event,
                "failure_code": sse.failure_code,
            }
        record["response"] = {"status": status, "headers": response_headers, "raw": raw, "parsed": parsed}
        record["timing"] = {"first_token_ms": first_token_ms, "full_body_ms": full_body_ms}
        stream_info = record.get("stream") or {}
        formal = stream_info.get("formal_commit", _formal_commit(parsed))
        body = _body_text(parsed) if formal else stream_info.get("preview_body", "")
        record["model_response"] = {"body": body, "preview_body": stream_info.get("preview_body", ""), "formal_commit": formal, "branch": _branch(parsed), "session": _session(parsed), "usage": _usage(parsed)}
        code = _error_code(parsed)
        stream_failure = stream_info.get("failure_code")
        expects_commit = stage in {"opening", "turn", "direction"}
        if status not in SUCCESS_STATUSES or code or stream_failure or (expects_commit and not formal):
            failure_code = stream_failure or code or (f"http_{status}" if status not in SUCCESS_STATUSES else "response_without_commit")
            record["failure"] = {"stage": stage, "code": failure_code, "message": _first(parsed, ("error", "message"), ("message",))}
        self._save(record)
        return record

    def _save(self, record: Dict[str, Any]) -> None:
        self.calls += 1
        self.evidence.append(record)
        path = self.output / "calls" / f"{self.calls:04d}-{record['request_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_scenarios(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "nq001-scenarios/0.1" or not isinstance(value.get("scenarios"), list):
        raise ValueError("NQ-001 场景夹具版本或结构无效")
    return value


def _choose_entry(package: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], List[str]]:
    gaps: List[str] = []
    # /api/v1/packages/{id}/{version} is the catalog contract.  Its entries
    # are top-level objects; package.story.entryModel is not a read API shape.
    entries = package.get("entries") if isinstance(package, dict) else None
    entries = entries if isinstance(entries, list) else []
    entry = next((item for item in entries if isinstance(item, dict)), None)
    entry_id = _first(entry or {}, ("id",), ("entryPointId",), ("entry_point_id",))
    character = _first(entry or {}, ("source_character_id",), ("sourceCharacterId",))
    if not isinstance(entry_id, str):
        gaps.append("entry_point_id")
    if not isinstance(character, str):
        character_ids = _first(entry or {}, ("source_character_ids",), ("sourceCharacterIds",))
        if isinstance(character_ids, list) and character_ids and isinstance(character_ids[0], str):
            character = character_ids[0]
        else:
            gaps.append("source_character_id")
    return entry_id if isinstance(entry_id, str) else None, character if isinstance(character, str) else None, gaps


def _metrics(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    records = list(records)
    generation = [r for r in records if r["stage"] in {"opening", "turn", "ending"}]
    successful = [r for r in generation if r.get("response", {}).get("status") in SUCCESS_STATUSES and not r.get("failure")]
    first_tokens = [r["timing"]["first_token_ms"] for r in generation if r.get("timing", {}).get("first_token_ms") is not None]
    complete = [r["timing"]["full_body_ms"] for r in generation if r.get("timing", {}).get("full_body_ms") is not None]
    direction_times = [r["timing"]["full_body_ms"] for r in records if r["stage"] == "direction" and r.get("response", {}).get("status") in SUCCESS_STATUSES]
    fact_signals: List[int] = []
    for record in records:
        parsed = record.get("response", {}).get("parsed")
        signal = _first(parsed or {}, ("metrics", "factOverreach"), ("metrics", "fact_overreach"), ("quality", "factOverreach"), ("quality", "fact_overreach"))
        if isinstance(signal, bool):
            fact_signals.append(1 if signal else 0)
        elif isinstance(signal, (int, float)):
            fact_signals.append(int(signal))
    usages = [_usage(r.get("response", {}).get("parsed")) for r in records]
    def average(values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 3) if values else None
    return {
        "first_draft_pass_rate": round(len(successful) / len(generation), 4) if generation else None,
        "final_success_rate": None,
        "fact_overreach_rate": round(sum(fact_signals) / len(fact_signals), 4) if fact_signals else None,
        "review_false_block_rate": None,
        "repair_success_rate": None,
        "first_token_ms_avg": average(first_tokens),
        "full_body_ms_avg": average(complete),
        "direction_available_ms_avg": average(direction_times),
        "input_tokens": sum(int(u.get("prompt_tokens", u.get("input_tokens", 0)) or 0) for u in usages),
        "output_tokens": sum(int(u.get("completion_tokens", u.get("output_tokens", 0)) or 0) for u in usages),
        "failure_stages": sorted({r["failure"]["stage"] for r in records if r.get("failure")}),
        "metric_gaps": ["final_success_rate", "review_false_block_rate", "repair_success_rate", "fact_overreach_rate"] if not fact_signals else ["final_success_rate", "review_false_block_rate", "repair_success_rate"],
        "quality_gate_observed": bool(fact_signals),
    }


def run(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    scenarios = _load_scenarios(Path(args.scenarios))
    database_path = Path(args.database_path).resolve() if args.database_path else Path(tempfile.mkdtemp(prefix="nq001-db-")) / "sessions.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "evaluation": "NQ-001",
        "mode": args.mode,
        "acceptance_claim": False,
        "package_id": args.package_id,
        "package_version": args.package_version,
        "prompt_version": args.prompt_version,
        "model": args.model,
        "route_id": args.route_id,
        "database_path": str(database_path),
        "database_binding": bool(args.launch_server),
        "base_url": args.base_url,
        "transport": "sse" if args.stream else "json",
        "scenario_file": str(Path(args.scenarios).resolve()),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (output / "run-config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.mode == "mock":
        config["status"] = "offline_mock_only"
    if not args.launch_server:
        config["database_binding"] = False
    server: Optional[subprocess.Popen[str]] = None
    if args.launch_server:
        command = shlex.split(args.server_command)
        env = {**os.environ, "STORY_API_PLAY": "1", "STORY_DATABASE_PATH": str(database_path), "STORY_LLM_MODEL": args.model, "STORY_PROMPT_VERSION": args.prompt_version}
        if args.mode == "mock":
            env["STORY_PLANNER"] = "mock"
        server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(90):
            try:
                with urlopen(args.base_url.rstrip("/") + "/api/v1/health", timeout=2):
                    break
            except OSError:
                time.sleep(1)
        else:
            config["status"] = "blocked_server_unavailable"
    client = HttpEvidenceClient(args.base_url, output, {"Accept": "application/json", "X-NQ001-Route": args.route_id, "X-NQ001-Prompt-Version": args.prompt_version, "X-NQ001-Model": args.model})
    report: Dict[str, Any] = {"schema": "nq001-report/0.1", "config": config, "status": "running", "contract_gaps": [], "scenarios": [], "metrics": {}}
    try:
        package_call = client.request("GET", f"/api/v1/packages/{args.package_id}/{args.package_version}", None, "package")
        package = package_call.get("response", {}).get("parsed") or {}
        entry_id, character_id, gaps = _choose_entry(package)
        report["contract_gaps"].extend({"stage": "package", "field": gap} for gap in gaps)
        if not args.launch_server:
            report["contract_gaps"].append({"stage": "startup", "field": "temporary_database_binding", "message": "未由评估器启动绑定临时数据库的 API 进程"})
        if args.launch_server and not database_path.parent.exists():
            report["contract_gaps"].append({"stage": "startup", "field": "temporary_database_binding"})
        for scenario in scenarios["scenarios"]:
            scenario_record: Dict[str, Any] = {"id": scenario["id"], "label": scenario.get("label"), "status": "running", "calls": [], "failure_stage": None, "restored": False, "ended": False}
            report["scenarios"].append(scenario_record)
            session_body = {"package": {"package_id": args.package_id, "version": args.package_version}, "entry_point_id": entry_id, "source_character_id": character_id, "identity_opening": True, "request_id": f"nq001-{args.route_id}-{scenario['id']}-opening"}
            opening_path = "/api/v1/sessions/stream" if args.stream else "/api/v1/sessions"
            opening = client.request("POST", opening_path, session_body, "opening", stream=args.stream)
            scenario_record["calls"].append(opening["request_id"])
            session = _session(opening.get("response", {}).get("parsed"))
            branch = _branch(opening.get("response", {}).get("parsed"))
            session_id = _first(session or {}, ("id",), ("sessionId",), ("session_id",))
            parent_id = _first(branch or {}, ("id",), ("branchId",), ("branch_id",))
            if not isinstance(session_id, str) or not isinstance(parent_id, str):
                scenario_record.update(status="blocked", failure_stage="opening")
                report["contract_gaps"].append({"scenario": scenario["id"], "stage": "opening", "fields": ["session.id", "branch.id"]})
                continue
            for index, step in enumerate(scenario.get("steps", []), 1):
                kind = step.get("kind")
                if kind in {"refresh", "review"}:
                    path = f"/api/v1/sessions/{session_id}" if kind == "refresh" else f"/api/v1/sessions/{session_id}/branches/{parent_id}"
                    call = client.request("GET", path, None, "restore" if kind == "refresh" else "review")
                    scenario_record["calls"].append(call["request_id"])
                    if call.get("response", {}).get("status") in SUCCESS_STATUSES:
                        scenario_record["restored"] = True
                    continue
                if kind == "ending":
                    call = client.request("POST", f"/api/v1/sessions/{session_id}/end", {"branch_id": parent_id}, "ending", stream=False)
                    scenario_record["calls"].append(call["request_id"])
                    parsed = call.get("response", {}).get("parsed")
                    scenario_record["ended"] = call.get("response", {}).get("status") in SUCCESS_STATUSES and not call.get("failure")
                    if not scenario_record["ended"] and _error_code(parsed) in {"route_ended", "ending_review_required", "endpoint_unavailable"}:
                        report["contract_gaps"].append({"scenario": scenario["id"], "stage": "ending", "code": _error_code(parsed)})
                    continue
                body: Dict[str, Any] = {"parent_branch_id": parent_id, "request_id": f"nq001-{args.route_id}-{scenario['id']}-{index}"}
                directions = _directions(branch or {})
                if kind == "preset":
                    chosen = directions[int(step.get("index", 0))] if directions and int(step.get("index", 0)) < len(directions) else None
                    direction_id = _first(chosen or {}, ("id",), ("directionId",), ("direction_id",))
                    if not isinstance(direction_id, str):
                        scenario_record.update(status="blocked", failure_stage="direction")
                        report["contract_gaps"].append({"scenario": scenario["id"], "stage": "direction", "field": "direction.id"})
                        break
                    body["direction_id"] = direction_id
                else:
                    body["text"] = step.get("text", "")
                branch_path = f"/api/v1/sessions/{session_id}/branches/stream" if args.stream else f"/api/v1/sessions/{session_id}/branches"
                call = client.request("POST", branch_path, body, "direction" if kind == "preset" else "turn", stream=args.stream)
                scenario_record["calls"].append(call["request_id"])
                next_branch = _branch(call.get("response", {}).get("parsed"))
                next_id = _first(next_branch or {}, ("id",), ("branchId",), ("branch_id",))
                if isinstance(next_id, str):
                    parent_id, branch = next_id, next_branch
                elif call.get("failure"):
                    scenario_record.update(status="failed", failure_stage=call["failure"]["stage"])
                    break
            if scenario_record["status"] == "running":
                scenario_record["status"] = "passed" if scenario_record["ended"] or scenario["id"] != "automatic-ending" else "incomplete"
            scenario_record["evidence_dir"] = str(output / "calls")
        report["metrics"] = _metrics(client.evidence)
        report["metrics"]["scenario_count"] = len(report["scenarios"])
        report["metrics"]["scenario_passed"] = sum(item["status"] == "passed" for item in report["scenarios"])
        if report["contract_gaps"]:
            report["status"] = "completed_with_contract_gaps"
        elif report["metrics"].get("metric_gaps"):
            report["status"] = "completed_with_metric_gaps"
        else:
            report["status"] = "completed"
        if args.mode == "mock":
            report["status"] = "offline_mock_only"
    finally:
        config["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
    return 0 if report.get("status") == "completed" and args.mode == "real" and not report["contract_gaps"] and not report["metrics"].get("metric_gaps") and all(s["status"] == "passed" for s in report["scenarios"]) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="NQ-001 真实模型连续路线评估；证据写入独立目录")
    parser.add_argument("--output", required=True)
    parser.add_argument("--package-id", required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt-version", required=True)
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--database-path")
    parser.add_argument("--scenarios", default=str(SCENARIO_FILE))
    parser.add_argument("--mode", choices=("real", "mock"), default="real")
    parser.add_argument("--stream", action="store_true", help="使用 /stream 写入端点并记录首个 delta")
    parser.add_argument("--launch-server", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只校验夹具与覆盖范围，不请求 API；结果永远不是 NQ-001 通过")
    parser.add_argument("--server-command", default=f"{os.environ.get('PYTHON', 'python3')} -m uvicorn open_story_engine.api:create_app --factory --host 127.0.0.1 --port 8000")
    args = parser.parse_args(argv)
    if args.dry_run:
        scenarios = _load_scenarios(Path(args.scenarios))
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=False)
        report = {
            "schema": "nq001-report/0.1",
            "status": "offline_plan_only",
            "config": {"evaluation": "NQ-001", "mode": args.mode, "acceptance_claim": False, "package_id": args.package_id, "package_version": args.package_version, "prompt_version": args.prompt_version, "model": args.model, "route_id": args.route_id, "database_binding": False},
            "scenarios": [{"id": item["id"], "label": item.get("label"), "status": "planned", "step_count": len(item.get("steps", []))} for item in scenarios["scenarios"]],
            "metrics": {"scenario_count": len(scenarios["scenarios"]), "coverage": "offline fixture plan only"},
            "contract_gaps": [{"stage": "all", "field": "live_api_and_p0_contract", "message": "dry-run 未调用 API"}],
        }
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
