import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { randomUUID } from "node:crypto";
import Database from "better-sqlite3";
import { parseGameState, type GameState, type StoryPackage } from "../../content/story-package.js";
import type { ActionIntent, TurnResolution } from "../../domain/rules/rule-engine.js";
import type { NarrativeContinuity } from "../../domain/narrative/narrative-progression.js";
import { parseBranchNode, type BranchNode } from "../../domain/co-creation/branch-node.js";
import { parseDirectionEvaluation, type DirectionEvaluation } from "../../domain/co-creation/direction-evaluation.js";
import type { PlannerAudit } from "../../domain/co-creation/branch-planner.js";
import type { DirectionEvaluationAudit } from "../../domain/co-creation/direction-evaluation.js";
import { parseSessionStoryContract, type SessionStoryContract } from "../../domain/co-creation/session-story-contract.js";
import { applyStatePatch, createStatePatch, type StatePatchOperation } from "../../domain/state/state-patch.js";

export type SessionStatus = "active" | "terminal";

export type StoredSession = {
  id: string;
  storyPackageId: string;
  storyPackageVersion: string;
  stateVersion: number;
  status: SessionStatus;
  currentState: GameState;
};

type StoredResolution = Omit<TurnResolution, "action" | "state">;

export type StoredGameEvent = {
  sessionId: string;
  sequence: number;
  requestId: string;
  playerInput: string;
  action: ActionIntent;
  resolution: StoredResolution;
  statePatch: StatePatchOperation[];
  stateAfter: GameState;
  createdAt: string;
};

export type CommitTurnInput = {
  sessionId: string;
  requestId: string;
  baseState: GameState;
  baseVersion: number;
  playerInput: string;
  action: ActionIntent;
  resolution: TurnResolution;
};

export type CommitTurnResult =
  | { kind: "committed"; event: StoredGameEvent }
  | { kind: "duplicate"; event: StoredGameEvent };

type SessionRow = {
  id: string;
  story_package_id: string;
  story_package_version: string;
  state_version: number;
  status: SessionStatus;
  current_state_json: string;
};

type EventRow = {
  session_id: string;
  sequence: number;
  request_id: string;
  player_input: string;
  action_json: string;
  resolution_json: string;
  state_patch_json: string;
  state_after_json: string;
  created_at: string;
};

export type StoredNarration = {
  sessionId: string;
  sequence: number;
  text: string;
  continuity: NarrativeContinuity;
  createdAt: string;
};

export type StoredBranchNode = BranchNode & {
  sessionId: string;
  sequence: number;
};

export type StoredDirectionEvaluation = DirectionEvaluation & {
  id: number;
  sessionId: string;
  parentBranchId: string;
  playerDirection: string;
  requestId?: string;
  createdAt: string;
};

export type StoredLlmAudit = PlannerAudit & {
  id: number;
  sessionId: string;
  createdAt: string;
};

export type StoredDirectionEvaluatorAudit = DirectionEvaluationAudit & {
  id: number;
  sessionId: string;
  parentBranchId: string;
  createdAt: string;
};

type NarrationRow = {
  session_id: string;
  sequence: number;
  text: string;
  continuity_json: string;
  created_at: string;
};

type StoryContractRow = {
  session_id: string;
  contract_json: string;
};

type BranchNodeRow = {
  id: string;
  session_id: string;
  sequence: number;
  request_id: string | null;
  node_json: string;
};

type DirectionEvaluationRow = {
  id: number;
  session_id: string;
  parent_branch_id: string;
  player_direction: string;
  request_id: string | null;
  evaluation_json: string;
  created_at: string;
};

type LlmAuditRow = {
  id: number;
  session_id: string;
  operation: "branch_planner";
  model: string;
  prompt_version: string;
  request_summary: string;
  raw_response: string | null;
  error: string | null;
  created_at: string;
};

type DirectionEvaluatorAuditRow = {
  id: number;
  session_id: string;
  parent_branch_id: string;
  model: string;
  prompt_version: string;
  request_summary: string;
  raw_response: string | null;
  error: string | null;
  created_at: string;
};

export class SqliteSessionStore {
  private readonly database: Database.Database;

  constructor(databasePath: string) {
    if (databasePath !== ":memory:") mkdirSync(dirname(databasePath), { recursive: true });
    this.database = new Database(databasePath);
    this.database.pragma("foreign_keys = ON");
    this.initializeSchema();
  }

  createSession(storyPackage: StoryPackage, sessionId: string = randomUUID()): StoredSession {
    const state = structuredClone(storyPackage.initialState);
    const now = new Date().toISOString();
    this.database.prepare(`
      INSERT INTO game_sessions (
        id, story_package_id, story_package_version, state_version, status, current_state_json, created_at, updated_at
      ) VALUES (?, ?, ?, 0, 'active', ?, ?, ?)
    `).run(sessionId, storyPackage.id, storyPackage.version, JSON.stringify(state), now, now);
    return { id: sessionId, storyPackageId: storyPackage.id, storyPackageVersion: storyPackage.version, stateVersion: 0, status: "active", currentState: state };
  }

  getSession(sessionId: string): StoredSession {
    const row = this.database.prepare("SELECT * FROM game_sessions WHERE id = ?").get(sessionId) as SessionRow | undefined;
    if (!row) throw new Error(`会话不存在: ${sessionId}`);
    return this.toSession(row);
  }

  findEventByRequestId(sessionId: string, requestId: string): StoredGameEvent | undefined {
    const row = this.database.prepare("SELECT * FROM game_events WHERE session_id = ? AND request_id = ?").get(sessionId, requestId) as EventRow | undefined;
    return row ? this.toEvent(row) : undefined;
  }

  listEvents(sessionId: string): StoredGameEvent[] {
    const rows = this.database.prepare("SELECT * FROM game_events WHERE session_id = ? ORDER BY sequence ASC").all(sessionId) as EventRow[];
    return rows.map((row) => this.toEvent(row));
  }

  commitTurn(input: CommitTurnInput): CommitTurnResult {
    return this.database.transaction(() => {
      const duplicate = this.findEventByRequestId(input.sessionId, input.requestId);
      if (duplicate) return { kind: "duplicate", event: duplicate } as const;

      const current = this.getSession(input.sessionId);
      if (current.status !== "active") throw new Error(`终局会话不能提交新回合: ${input.sessionId}`);
      if (current.stateVersion !== input.baseVersion) throw new Error(`会话版本已变化: expected ${input.baseVersion}, actual ${current.stateVersion}`);
      if (JSON.stringify(current.currentState) !== JSON.stringify(input.baseState)) throw new Error("规则结算使用的基础状态不是当前快照");

      const statePatch = createStatePatch(current.currentState, input.resolution.state);
      if (statePatch.length === 0) throw new Error("有效回合必须产生状态变化");
      const sequence = current.stateVersion + 1;
      const status: SessionStatus = input.resolution.endingId ? "terminal" : "active";
      const now = new Date().toISOString();
      const storedResolution = toStoredResolution(input.resolution);

      this.database.prepare(`
        INSERT INTO game_events (
          session_id, sequence, request_id, player_input, event_type, action_json, resolution_json, state_patch_json, state_after_json, created_at
        ) VALUES (?, ?, ?, ?, 'turn_resolved', ?, ?, ?, ?, ?)
      `).run(
        input.sessionId,
        sequence,
        input.requestId,
        input.playerInput,
        JSON.stringify(input.action),
        JSON.stringify(storedResolution),
        JSON.stringify(statePatch),
        JSON.stringify(input.resolution.state),
        now,
      );
      this.database.prepare(`
        UPDATE game_sessions
        SET state_version = ?, status = ?, current_state_json = ?, updated_at = ?
        WHERE id = ?
      `).run(sequence, status, JSON.stringify(input.resolution.state), now, input.sessionId);

      return {
        kind: "committed",
        event: { sessionId: input.sessionId, sequence, requestId: input.requestId, playerInput: input.playerInput, action: input.action, resolution: storedResolution, statePatch, stateAfter: input.resolution.state, createdAt: now },
      } as const;
    })();
  }

  rebuildState(sessionId: string, initialState: GameState): GameState {
    const events = this.listEvents(sessionId);
    let state = structuredClone(initialState);
    let expectedSequence = 1;
    for (const event of events) {
      if (event.sequence !== expectedSequence) throw new Error(`事件序列不连续: expected ${expectedSequence}, actual ${event.sequence}`);
      state = applyStatePatch(state, event.statePatch);
      expectedSequence += 1;
    }
    return state;
  }

  getNarration(sessionId: string, sequence: number): StoredNarration | undefined {
    const row = this.database.prepare("SELECT * FROM event_narrations WHERE session_id = ? AND sequence = ?").get(sessionId, sequence) as NarrationRow | undefined;
    return row ? this.toNarration(row) : undefined;
  }

  getLatestNarration(sessionId: string): StoredNarration | undefined {
    const row = this.database.prepare("SELECT * FROM event_narrations WHERE session_id = ? ORDER BY sequence DESC LIMIT 1").get(sessionId) as NarrationRow | undefined;
    return row ? this.toNarration(row) : undefined;
  }

  saveNarration(sessionId: string, sequence: number, narration: Pick<StoredNarration, "text" | "continuity">): StoredNarration {
    const now = new Date().toISOString();
    this.database.prepare(`
      INSERT OR IGNORE INTO event_narrations (session_id, sequence, text, continuity_json, created_at)
      VALUES (?, ?, ?, ?, ?)
    `).run(sessionId, sequence, narration.text, JSON.stringify(narration.continuity), now);
    const storedNarration = this.getNarration(sessionId, sequence);
    if (!storedNarration) throw new Error(`叙事写入失败: ${sessionId}#${sequence}`);
    return storedNarration;
  }

  saveSessionStoryContract(contract: SessionStoryContract): SessionStoryContract {
    const session = this.getSession(contract.sessionId);
    if (session.storyPackageId !== contract.sourcePackageRef.id || session.storyPackageVersion !== contract.sourcePackageRef.version) {
      throw new Error("共创契约的原著包版本与会话不一致");
    }
    this.database.prepare(`
      INSERT INTO session_story_contracts (session_id, contract_json, created_at)
      VALUES (?, ?, ?)
    `).run(contract.sessionId, JSON.stringify(contract), contract.createdAt);
    return this.getSessionStoryContract(contract.sessionId);
  }

  getSessionStoryContract(sessionId: string): SessionStoryContract {
    const row = this.database.prepare("SELECT * FROM session_story_contracts WHERE session_id = ?").get(sessionId) as StoryContractRow | undefined;
    if (!row) throw new Error(`会话尚未建立共创契约: ${sessionId}`);
    return parseSessionStoryContract(JSON.parse(row.contract_json) as unknown);
  }

  createBranchRoot(sessionId: string, node: BranchNode): StoredBranchNode {
    if (node.kind !== "source_entry" || node.parentId) throw new Error("共创根节点必须是无父节点的 source_entry");
    return this.database.transaction(() => {
      const contract = this.getSessionStoryContract(sessionId);
      if (node.sourceNodeRef !== contract.entryNodeId) throw new Error("共创根节点必须引用契约的进入节点");
      const existing = this.database.prepare("SELECT id FROM branch_nodes WHERE session_id = ? AND parent_id IS NULL").get(sessionId) as { id: string } | undefined;
      if (existing) throw new Error(`共创根节点已存在: ${existing.id}`);
      this.database.prepare(`
        INSERT INTO branch_nodes (id, session_id, sequence, parent_id, request_id, node_json, created_at)
        VALUES (?, ?, 0, NULL, NULL, ?, ?)
      `).run(node.id, sessionId, JSON.stringify(node), node.createdAt);
      return { ...node, sessionId, sequence: 0 };
    })();
  }

  appendBranchNode(
    sessionId: string,
    parentId: string,
    node: Omit<BranchNode, "id" | "kind" | "parentId" | "createdAt">,
  ): StoredBranchNode {
    return this.database.transaction(() => {
      this.getSessionStoryContract(sessionId);
      const parent = this.getBranchNode(sessionId, parentId);
      if (!node.selectedDirectionId || !parent.nextDirections.some((direction) => direction.id === node.selectedDirectionId)) {
        throw new Error("共创子节点必须来自父节点已公布的剧情方向");
      }
      const row = this.database.prepare("SELECT COALESCE(MAX(sequence), 0) AS latest_sequence FROM branch_nodes WHERE session_id = ?").get(sessionId) as { latest_sequence: number };
      const fullNode = parseBranchNode({
        ...node,
        id: `branch_${randomUUID()}`,
        kind: "generated",
        parentId: parent.id,
        createdAt: new Date().toISOString(),
      });
      const sequence = row.latest_sequence + 1;
      this.database.prepare(`
        INSERT INTO branch_nodes (id, session_id, sequence, parent_id, request_id, node_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
      `).run(fullNode.id, sessionId, sequence, parent.id, fullNode.requestId ?? null, JSON.stringify(fullNode), fullNode.createdAt);
      return { ...fullNode, sessionId, sequence };
    })();
  }

  getBranchNode(sessionId: string, nodeId: string): StoredBranchNode {
    const row = this.database.prepare("SELECT * FROM branch_nodes WHERE session_id = ? AND id = ?").get(sessionId, nodeId) as BranchNodeRow | undefined;
    if (!row) throw new Error(`共创分支节点不存在: ${nodeId}`);
    return this.toBranchNode(row);
  }

  findBranchNodeByRequestId(sessionId: string, requestId: string): StoredBranchNode | undefined {
    const row = this.database.prepare("SELECT * FROM branch_nodes WHERE session_id = ? AND request_id = ?").get(sessionId, requestId) as BranchNodeRow | undefined;
    return row ? this.toBranchNode(row) : undefined;
  }

  listBranchNodes(sessionId: string): StoredBranchNode[] {
    const rows = this.database.prepare("SELECT * FROM branch_nodes WHERE session_id = ? ORDER BY sequence ASC").all(sessionId) as BranchNodeRow[];
    return rows.map((row) => this.toBranchNode(row));
  }

  listBranchLineage(sessionId: string, nodeId: string): StoredBranchNode[] {
    const lineage: StoredBranchNode[] = [];
    const visited = new Set<string>();
    let current: StoredBranchNode | undefined = this.getBranchNode(sessionId, nodeId);

    while (current) {
      if (visited.has(current.id)) throw new Error(`共创分支存在循环: ${current.id}`);
      visited.add(current.id);
      lineage.push(current);
      current = current.parentId ? this.getBranchNode(sessionId, current.parentId) : undefined;
    }

    return lineage.reverse();
  }

  saveDirectionEvaluation(
    sessionId: string,
    parentBranchId: string,
    playerDirection: string,
    evaluation: DirectionEvaluation,
    requestId?: string,
  ): StoredDirectionEvaluation {
    this.getBranchNode(sessionId, parentBranchId);
    const now = new Date().toISOString();
    const result = this.database.prepare(`
      INSERT INTO direction_evaluations (session_id, parent_branch_id, player_direction, request_id, evaluation_json, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
    `).run(sessionId, parentBranchId, playerDirection, requestId ?? null, JSON.stringify(evaluation), now);
    const row = this.database.prepare("SELECT * FROM direction_evaluations WHERE id = ?").get(result.lastInsertRowid) as DirectionEvaluationRow | undefined;
    if (!row) throw new Error("自由文本方向判定写入失败");
    return this.toDirectionEvaluation(row);
  }

  listDirectionEvaluations(sessionId: string): StoredDirectionEvaluation[] {
    const rows = this.database.prepare("SELECT * FROM direction_evaluations WHERE session_id = ? ORDER BY id ASC").all(sessionId) as DirectionEvaluationRow[];
    return rows.map((row) => this.toDirectionEvaluation(row));
  }

  findDirectionEvaluationByRequestId(sessionId: string, requestId: string): StoredDirectionEvaluation | undefined {
    const row = this.database.prepare("SELECT * FROM direction_evaluations WHERE session_id = ? AND request_id = ?").get(sessionId, requestId) as DirectionEvaluationRow | undefined;
    return row ? this.toDirectionEvaluation(row) : undefined;
  }

  saveLlmAudit(sessionId: string, audit: PlannerAudit): StoredLlmAudit {
    this.getSession(sessionId);
    const now = new Date().toISOString();
    const result = this.database.prepare(`
      INSERT INTO llm_audits (session_id, operation, model, prompt_version, request_summary, raw_response, error, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `).run(sessionId, audit.operation, audit.model, audit.promptVersion, audit.requestSummary, audit.rawResponse ?? null, audit.error ?? null, now);
    const row = this.database.prepare("SELECT * FROM llm_audits WHERE id = ?").get(result.lastInsertRowid) as LlmAuditRow | undefined;
    if (!row) throw new Error("LLM 审计写入失败");
    return this.toLlmAudit(row);
  }

  listLlmAudits(sessionId: string): StoredLlmAudit[] {
    const rows = this.database.prepare("SELECT * FROM llm_audits WHERE session_id = ? ORDER BY id ASC").all(sessionId) as LlmAuditRow[];
    return rows.map((row) => this.toLlmAudit(row));
  }

  saveDirectionEvaluatorAudit(
    sessionId: string,
    parentBranchId: string,
    audit: DirectionEvaluationAudit,
  ): StoredDirectionEvaluatorAudit {
    this.getBranchNode(sessionId, parentBranchId);
    const now = new Date().toISOString();
    const result = this.database.prepare(`
      INSERT INTO direction_evaluator_audits (session_id, parent_branch_id, model, prompt_version, request_summary, raw_response, error, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `).run(sessionId, parentBranchId, audit.model, audit.promptVersion, audit.requestSummary, audit.rawResponse ?? null, audit.error ?? null, now);
    const row = this.database.prepare("SELECT * FROM direction_evaluator_audits WHERE id = ?").get(result.lastInsertRowid) as DirectionEvaluatorAuditRow | undefined;
    if (!row) throw new Error("LLM 方向判定审计写入失败");
    return this.toDirectionEvaluatorAudit(row);
  }

  listDirectionEvaluatorAudits(sessionId: string): StoredDirectionEvaluatorAudit[] {
    const rows = this.database.prepare("SELECT * FROM direction_evaluator_audits WHERE session_id = ? ORDER BY id ASC").all(sessionId) as DirectionEvaluatorAuditRow[];
    return rows.map((row) => this.toDirectionEvaluatorAudit(row));
  }

  close(): void {
    this.database.close();
  }

  private initializeSchema(): void {
    this.database.exec(`
      CREATE TABLE IF NOT EXISTS game_sessions (
        id TEXT PRIMARY KEY,
        story_package_id TEXT NOT NULL,
        story_package_version TEXT NOT NULL,
        state_version INTEGER NOT NULL CHECK (state_version >= 0),
        status TEXT NOT NULL CHECK (status IN ('active', 'terminal')),
        current_state_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS game_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES game_sessions(id),
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        request_id TEXT NOT NULL,
        player_input TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type = 'turn_resolved'),
        action_json TEXT NOT NULL,
        resolution_json TEXT NOT NULL,
        state_patch_json TEXT NOT NULL,
        state_after_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(session_id, sequence),
        UNIQUE(session_id, request_id)
      );

      CREATE TABLE IF NOT EXISTS event_narrations (
        session_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        text TEXT NOT NULL,
        continuity_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        PRIMARY KEY (session_id, sequence),
        FOREIGN KEY (session_id, sequence) REFERENCES game_events(session_id, sequence)
      );

      CREATE TABLE IF NOT EXISTS session_story_contracts (
        session_id TEXT PRIMARY KEY REFERENCES game_sessions(id),
        contract_json TEXT NOT NULL,
        created_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS branch_nodes (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES game_sessions(id),
        sequence INTEGER NOT NULL CHECK (sequence >= 0),
        parent_id TEXT REFERENCES branch_nodes(id),
        request_id TEXT,
        node_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(session_id, sequence)
      );

      CREATE TABLE IF NOT EXISTS direction_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES game_sessions(id),
        parent_branch_id TEXT NOT NULL REFERENCES branch_nodes(id),
        player_direction TEXT NOT NULL,
        request_id TEXT,
        evaluation_json TEXT NOT NULL,
        created_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS llm_audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES game_sessions(id),
        operation TEXT NOT NULL CHECK (operation = 'branch_planner'),
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        request_summary TEXT NOT NULL,
        raw_response TEXT,
        error TEXT,
        created_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS direction_evaluator_audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES game_sessions(id),
        parent_branch_id TEXT NOT NULL REFERENCES branch_nodes(id),
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        request_summary TEXT NOT NULL,
        raw_response TEXT,
        error TEXT,
        created_at TEXT NOT NULL
      );
    `);
    const eventColumns = this.database.prepare("PRAGMA table_info(game_events)").all() as Array<{ name: string }>;
    if (!eventColumns.some((column) => column.name === "player_input")) {
      this.database.exec("ALTER TABLE game_events ADD COLUMN player_input TEXT NOT NULL DEFAULT ''");
    }
    const narrationColumns = this.database.prepare("PRAGMA table_info(event_narrations)").all() as Array<{ name: string }>;
    if (!narrationColumns.some((column) => column.name === "continuity_json")) {
      this.database.exec("ALTER TABLE event_narrations ADD COLUMN continuity_json TEXT NOT NULL DEFAULT '{}'");
    }
    this.ensureColumn("branch_nodes", "request_id", "TEXT");
    this.ensureColumn("direction_evaluations", "request_id", "TEXT");
    this.database.exec("CREATE UNIQUE INDEX IF NOT EXISTS branch_nodes_request_id_unique ON branch_nodes(session_id, request_id) WHERE request_id IS NOT NULL");
    this.database.exec("CREATE UNIQUE INDEX IF NOT EXISTS direction_evaluations_request_id_unique ON direction_evaluations(session_id, request_id) WHERE request_id IS NOT NULL");
  }

  private ensureColumn(tableName: "branch_nodes" | "direction_evaluations", columnName: "request_id", declaration: string): void {
    const columns = this.database.prepare(`PRAGMA table_info(${tableName})`).all() as Array<{ name: string }>;
    if (!columns.some((column) => column.name === columnName)) {
      this.database.exec(`ALTER TABLE ${tableName} ADD COLUMN ${columnName} ${declaration}`);
    }
  }

  private toSession(row: SessionRow): StoredSession {
    return {
      id: row.id,
      storyPackageId: row.story_package_id,
      storyPackageVersion: row.story_package_version,
      stateVersion: row.state_version,
      status: row.status,
      currentState: parseGameState(JSON.parse(row.current_state_json) as unknown),
    };
  }

  private toEvent(row: EventRow): StoredGameEvent {
    return {
      sessionId: row.session_id,
      sequence: row.sequence,
      requestId: row.request_id,
      playerInput: row.player_input,
      action: JSON.parse(row.action_json) as ActionIntent,
      resolution: JSON.parse(row.resolution_json) as StoredResolution,
      statePatch: JSON.parse(row.state_patch_json) as StatePatchOperation[],
      stateAfter: parseGameState(JSON.parse(row.state_after_json) as unknown),
      createdAt: row.created_at,
    };
  }

  private toNarration(row: NarrationRow): StoredNarration {
    return {
      sessionId: row.session_id,
      sequence: row.sequence,
      text: row.text,
      continuity: JSON.parse(row.continuity_json) as NarrativeContinuity,
      createdAt: row.created_at,
    };
  }

  private toBranchNode(row: BranchNodeRow): StoredBranchNode {
    return { ...parseBranchNode(JSON.parse(row.node_json) as unknown), sessionId: row.session_id, sequence: row.sequence };
  }

  private toDirectionEvaluation(row: DirectionEvaluationRow): StoredDirectionEvaluation {
    return {
      ...parseDirectionEvaluation(JSON.parse(row.evaluation_json) as unknown),
      id: row.id,
      sessionId: row.session_id,
      parentBranchId: row.parent_branch_id,
      playerDirection: row.player_direction,
      requestId: row.request_id ?? undefined,
      createdAt: row.created_at,
    };
  }

  private toLlmAudit(row: LlmAuditRow): StoredLlmAudit {
    return {
      id: row.id,
      sessionId: row.session_id,
      operation: row.operation,
      model: row.model,
      promptVersion: row.prompt_version,
      requestSummary: row.request_summary,
      rawResponse: row.raw_response ?? undefined,
      error: row.error ?? undefined,
      createdAt: row.created_at,
    };
  }

  private toDirectionEvaluatorAudit(row: DirectionEvaluatorAuditRow): StoredDirectionEvaluatorAudit {
    return {
      id: row.id,
      sessionId: row.session_id,
      parentBranchId: row.parent_branch_id,
      operation: "direction_evaluator",
      model: row.model,
      promptVersion: row.prompt_version,
      requestSummary: row.request_summary,
      rawResponse: row.raw_response ?? undefined,
      error: row.error ?? undefined,
      createdAt: row.created_at,
    };
  }
}

function toStoredResolution(resolution: TurnResolution): StoredResolution {
  const { action: _action, state: _state, ...storedResolution } = resolution;
  return storedResolution;
}
