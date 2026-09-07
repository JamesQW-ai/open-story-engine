"""SQLite persistence compatible with the runtime's event-oriented model."""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .state import apply_patch, create_patch


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load(value: str) -> Any:
    return json.loads(value)


class SessionStore:
    """Persists sessions, normal turns, co-creation nodes and auditable model calls.

    The schema is owned by the Python runtime. Its flexible audit tables keep
    model requests and local validation outcomes available for later review.
    """

    def __init__(self, database_path: str) -> None:
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS game_sessions (
          id TEXT PRIMARY KEY, story_package_id TEXT NOT NULL, story_package_version TEXT NOT NULL,
          state_version INTEGER NOT NULL, status TEXT NOT NULL, current_state_json TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS game_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES game_sessions(id),
          sequence INTEGER NOT NULL, request_id TEXT NOT NULL, player_input TEXT NOT NULL,
          event_type TEXT NOT NULL, action_json TEXT NOT NULL, resolution_json TEXT NOT NULL,
          state_patch_json TEXT NOT NULL, state_after_json TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(session_id, sequence), UNIQUE(session_id, request_id)
        );
        CREATE TABLE IF NOT EXISTS event_narrations (
          session_id TEXT NOT NULL, sequence INTEGER NOT NULL, text TEXT NOT NULL,
          continuity_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          PRIMARY KEY(session_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS session_story_contracts (
          session_id TEXT PRIMARY KEY REFERENCES game_sessions(id), contract_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_derived_story_packages (
          session_id TEXT PRIMARY KEY REFERENCES game_sessions(id), package_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS branch_nodes (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES game_sessions(id), sequence INTEGER NOT NULL,
          parent_id TEXT REFERENCES branch_nodes(id), request_id TEXT, node_json TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(session_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS direction_evaluations (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES game_sessions(id),
          parent_branch_id TEXT NOT NULL REFERENCES branch_nodes(id), player_direction TEXT NOT NULL,
          request_id TEXT, evaluation_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS llm_audits (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES game_sessions(id),
          operation TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
          request_summary TEXT NOT NULL, raw_response TEXT, error TEXT, call_observations_json TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS direction_evaluator_audits (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES game_sessions(id),
          parent_branch_id TEXT NOT NULL REFERENCES branch_nodes(id), model TEXT NOT NULL, prompt_version TEXT NOT NULL,
          request_summary TEXT NOT NULL, raw_response TEXT, error TEXT, call_observations_json TEXT, created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS branch_nodes_request_id_unique ON branch_nodes(session_id, request_id) WHERE request_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS direction_evaluations_request_id_unique ON direction_evaluations(session_id, request_id) WHERE request_id IS NOT NULL;
        """)
        self.connection.commit()

    def create_session(self, package: Dict[str, Any], session_id: Optional[str] = None) -> Dict[str, Any]:
        session_id = session_id or str(uuid.uuid4())
        state = copy.deepcopy(package["initialState"])
        timestamp = now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO game_sessions VALUES (?, ?, ?, 0, 'active', ?, ?, ?)",
                (session_id, package["id"], package["version"], dump(state), timestamp, timestamp),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> Dict[str, Any]:
        row = self.connection.execute("SELECT * FROM game_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"会话不存在: {session_id}")
        return {"id": row["id"], "storyPackageId": row["story_package_id"], "storyPackageVersion": row["story_package_version"], "stateVersion": row["state_version"], "status": row["status"], "currentState": load(row["current_state_json"])}

    def commit_turn(self, session_id: str, request_id: str, base_state: Dict[str, Any], base_version: int, player_input: str, action: Dict[str, Any], resolution: Dict[str, Any]) -> Dict[str, Any]:
        with self.connection:
            duplicate = self.connection.execute("SELECT * FROM game_events WHERE session_id = ? AND request_id = ?", (session_id, request_id)).fetchone()
            if duplicate is not None:
                return {"kind": "duplicate", "event": self._event(duplicate)}
            session = self.get_session(session_id)
            if session["status"] != "active":
                raise ValueError("终局会话不能提交新回合")
            if session["stateVersion"] != base_version or session["currentState"] != base_state:
                raise ValueError("会话快照已变化，拒绝提交过期回合")
            patch = create_patch(base_state, resolution["state"])
            if not patch:
                raise ValueError("有效回合必须产生状态变化")
            sequence = base_version + 1
            status = "terminal" if resolution.get("endingId") else "active"
            timestamp = now()
            stored = {key: value for key, value in resolution.items() if key not in ("action", "state")}
            self.connection.execute(
                "INSERT INTO game_events(session_id,sequence,request_id,player_input,event_type,action_json,resolution_json,state_patch_json,state_after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (session_id, sequence, request_id, player_input, "turn_resolved", dump(action), dump(stored), dump(patch), dump(resolution["state"]), timestamp),
            )
            self.connection.execute("UPDATE game_sessions SET state_version=?, status=?, current_state_json=?, updated_at=? WHERE id=?", (sequence, status, dump(resolution["state"]), timestamp, session_id))
        event = self.find_event(session_id, request_id)
        return {"kind": "committed", "event": event}

    def _event(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {"sessionId": row["session_id"], "sequence": row["sequence"], "requestId": row["request_id"], "playerInput": row["player_input"], "action": load(row["action_json"]), "resolution": load(row["resolution_json"]), "statePatch": load(row["state_patch_json"]), "stateAfter": load(row["state_after_json"]), "createdAt": row["created_at"]}

    def find_event(self, session_id: str, request_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM game_events WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()
        return self._event(row) if row else None

    def list_events(self, session_id: str) -> List[Dict[str, Any]]:
        return [self._event(row) for row in self.connection.execute("SELECT * FROM game_events WHERE session_id=? ORDER BY sequence", (session_id,))]

    def rebuild_state(self, session_id: str, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        state = copy.deepcopy(initial_state)
        expected = 1
        for event in self.list_events(session_id):
            if event["sequence"] != expected:
                raise ValueError("事件序列不连续")
            state = apply_patch(state, event["statePatch"])
            expected += 1
        return state

    def save_narration(self, session_id: str, sequence: int, text: str, continuity: Dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO event_narrations VALUES(?,?,?,?,?)", (session_id, sequence, text, dump(continuity), now()))

    def latest_narration(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM event_narrations WHERE session_id=? ORDER BY sequence DESC LIMIT 1", (session_id,)).fetchone()
        return None if row is None else {"sessionId": row["session_id"], "sequence": row["sequence"], "text": row["text"], "continuity": load(row["continuity_json"]), "createdAt": row["created_at"]}

    def save_contract(self, contract: Dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO session_story_contracts VALUES(?,?,?)", (contract["sessionId"], dump(contract), contract["createdAt"]))

    def contract(self, session_id: str) -> Dict[str, Any]:
        row = self.connection.execute("SELECT contract_json FROM session_story_contracts WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError("会话尚未建立共创契约")
        return load(row[0])

    def save_derived(self, package: Dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO session_derived_story_packages VALUES(?,?,?)", (package["sessionId"], dump(package), package["createdAt"]))

    def derived(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT package_json FROM session_derived_story_packages WHERE session_id=?", (session_id,)).fetchone()
        return load(row[0]) if row else None

    def update_derived(self, package: Dict[str, Any]) -> None:
        existing = self.derived(package["sessionId"])
        if existing is None:
            raise ValueError("衍生故事包不存在")
        immutable = ("id", "sourcePackageRef", "forkBranchId", "title", "goal", "createdAt")
        if any(existing[key] != package[key] for key in immutable) or len(package["revisions"]) != len(existing["revisions"]) + 1 or package["revisions"][:-1] != existing["revisions"]:
            raise ValueError("衍生故事包只能追加修订，不能改写来源或既有内容")
        with self.connection:
            self.connection.execute("UPDATE session_derived_story_packages SET package_json=? WHERE session_id=?", (dump(package), package["sessionId"]))

    def create_branch_root(self, session_id: str, node: Dict[str, Any]) -> Dict[str, Any]:
        if node["kind"] != "source_entry" or node.get("parentId"):
            raise ValueError("共创根节点必须是无父 source_entry")
        with self.connection:
            exists = self.connection.execute("SELECT 1 FROM branch_nodes WHERE session_id=? AND parent_id IS NULL", (session_id,)).fetchone()
            if exists:
                raise ValueError("共创根节点已存在")
            self.connection.execute(
                "INSERT INTO branch_nodes(id,session_id,sequence,parent_id,request_id,node_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (node["id"], session_id, 0, None, None, dump(node), node["createdAt"]),
            )
        return self.branch(session_id, node["id"])

    def append_branch(self, session_id: str, parent_id: str, node: Dict[str, Any]) -> Dict[str, Any]:
        parent = self.branch(session_id, parent_id)
        if node.get("selectedDirectionId") not in {direction["id"] for direction in parent["nextDirections"]}:
            raise ValueError("共创子节点必须来自父节点已公布的剧情方向")
        node = copy.deepcopy(node)
        node.setdefault("id", "branch_" + str(uuid.uuid4()))
        node["kind"] = "generated"
        node["parentId"] = parent_id
        node.setdefault("createdAt", now())
        with self.connection:
            sequence = self.connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM branch_nodes WHERE session_id=?", (session_id,)).fetchone()[0]
            self.connection.execute(
                "INSERT INTO branch_nodes(id,session_id,sequence,parent_id,request_id,node_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (node["id"], session_id, sequence, parent_id, node.get("requestId"), dump(node), node["createdAt"]),
            )
        return self.branch(session_id, node["id"])

    def create_derived_entry(self, session_id: str, parent_id: str, node: Dict[str, Any]) -> Dict[str, Any]:
        with self.connection:
            sequence = self.connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM branch_nodes WHERE session_id=?", (session_id,)).fetchone()[0]
            self.connection.execute(
                "INSERT INTO branch_nodes(id,session_id,sequence,parent_id,request_id,node_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (node["id"], session_id, sequence, parent_id, None, dump(node), node["createdAt"]),
            )
        return self.branch(session_id, node["id"])

    def branch(self, session_id: str, node_id: str) -> Dict[str, Any]:
        row = self.connection.execute("SELECT * FROM branch_nodes WHERE session_id=? AND id=?", (session_id, node_id)).fetchone()
        if row is None:
            raise ValueError(f"共创分支节点不存在: {node_id}")
        node = load(row["node_json"])
        node.update({"sessionId": row["session_id"], "sequence": row["sequence"]})
        return node

    def find_branch_request(self, session_id: str, request_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM branch_nodes WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()
        return self.branch(session_id, row["id"]) if row else None

    def branches(self, session_id: str) -> List[Dict[str, Any]]:
        return [self.branch(session_id, row["id"]) for row in self.connection.execute("SELECT id FROM branch_nodes WHERE session_id=? ORDER BY sequence", (session_id,))]

    def lineage(self, session_id: str, node_id: str) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        current = self.branch(session_id, node_id)
        visited = set()
        while current:
            if current["id"] in visited:
                raise ValueError("共创分支存在循环")
            visited.add(current["id"])
            result.append(current)
            current = self.branch(session_id, current["parentId"]) if current.get("parentId") else None
        return list(reversed(result))

    def save_direction_evaluation(self, session_id: str, parent_id: str, player_direction: str, evaluation: Dict[str, Any], request_id: Optional[str] = None) -> Dict[str, Any]:
        with self.connection:
            cursor = self.connection.execute("INSERT INTO direction_evaluations(session_id,parent_branch_id,player_direction,request_id,evaluation_json,created_at) VALUES(?,?,?,?,?,?)", (session_id, parent_id, player_direction, request_id, dump(evaluation), now()))
        return self.direction_audits(session_id)[-1]

    def direction_audits(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM direction_evaluations WHERE session_id=? ORDER BY id", (session_id,))
        return [{**load(row["evaluation_json"]), "id": row["id"], "sessionId": row["session_id"], "parentBranchId": row["parent_branch_id"], "playerDirection": row["player_direction"], "requestId": row["request_id"], "createdAt": row["created_at"]} for row in rows]

    def save_audit(self, session_id: str, audit: Dict[str, Any], parent_id: Optional[str] = None) -> None:
        table = "direction_evaluator_audits" if audit["operation"] == "direction_evaluator" else "llm_audits"
        with self.connection:
            if table == "direction_evaluator_audits":
                self.connection.execute("INSERT INTO direction_evaluator_audits(session_id,parent_branch_id,model,prompt_version,request_summary,raw_response,error,call_observations_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (session_id, parent_id, audit["model"], audit["promptVersion"], audit["requestSummary"], audit.get("rawResponse"), audit.get("error"), dump(audit.get("callObservations", [])), now()))
            else:
                self.connection.execute("INSERT INTO llm_audits(session_id,operation,model,prompt_version,request_summary,raw_response,error,call_observations_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (session_id, audit["operation"], audit["model"], audit["promptVersion"], audit["requestSummary"], audit.get("rawResponse"), audit.get("error"), dump(audit.get("callObservations", [])), now()))

    def llm_audits(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM llm_audits WHERE session_id=? ORDER BY id", (session_id,))
        return [{"id": row["id"], "operation": row["operation"], "model": row["model"], "error": row["error"], "callObservations": load(row["call_observations_json"]) if row["call_observations_json"] else []} for row in rows]

    def direction_evaluator_audits(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM direction_evaluator_audits WHERE session_id=? ORDER BY id", (session_id,))
        return [{"id": row["id"], "operation": "direction_evaluator", "model": row["model"], "error": row["error"], "callObservations": load(row["call_observations_json"]) if row["call_observations_json"] else []} for row in rows]
