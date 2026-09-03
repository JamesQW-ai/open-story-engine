import type { ActionIntent, RandomSource, TurnResolution } from "../rules/rule-engine.js";
import { RuleEngine } from "../rules/rule-engine.js";
import { SqliteSessionStore, type StoredGameEvent } from "../../infrastructure/sqlite/session-store.js";

export class TurnService {
  constructor(
    private readonly store: SqliteSessionStore,
    private readonly ruleEngine: RuleEngine,
    private readonly random: RandomSource,
  ) {}

  play(sessionId: string, requestId: string, action: ActionIntent, playerInput = action.approach): TurnResolution {
    const existing = this.store.findEventByRequestId(sessionId, requestId);
    if (existing) return toTurnResolution(existing);

    const session = this.store.getSession(sessionId);
    const resolution = this.ruleEngine.resolve(session.currentState, action, this.random);
    if (resolution.outcome === "blocked" || resolution.outcome === "terminal") return resolution;

    const committed = this.store.commitTurn({
      sessionId,
      requestId,
      baseState: session.currentState,
      baseVersion: session.stateVersion,
      playerInput,
      action,
      resolution,
    });
    return toTurnResolution(committed.event);
  }
}

function toTurnResolution(event: StoredGameEvent): TurnResolution {
  return { ...event.resolution, action: event.action, state: event.stateAfter };
}
