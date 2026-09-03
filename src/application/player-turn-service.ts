import type { GameState, StoryPackage } from "../content/story-package.js";
import { MockActionParser } from "../domain/action/mock-action-parser.js";
import type { Narrator } from "../domain/narrative/mock-narrator.js";
import { listNarrativeDirections, type NarrativeDirection } from "../domain/narrative/narrative-directions.js";
import { selectNarrativeProgression } from "../domain/narrative/narrative-progression.js";
import type { ActionIntent, TurnResolution } from "../domain/rules/rule-engine.js";
import { TurnService } from "../domain/turn/turn-service.js";
import { SqliteSessionStore } from "../infrastructure/sqlite/session-store.js";

export type PlayerTurnResult =
  | { kind: "clarification"; message: string; state: GameState }
  | {
    kind: "resolved";
    playerInput: string;
    intent: ActionIntent;
    resolution: TurnResolution;
    narrationStatus: "ready" | "pending";
    narration?: string;
    directionOptions: NarrativeDirection[];
  };

export class PlayerTurnService {
  constructor(
    private readonly storyPackage: StoryPackage,
    private readonly store: SqliteSessionStore,
    private readonly turnService: TurnService,
    private readonly actionParser: MockActionParser,
    private readonly narrator: Narrator,
  ) {}

  submit(sessionId: string, requestId: string, playerInput: string, actionInput = playerInput): PlayerTurnResult {
    const existing = this.store.findEventByRequestId(sessionId, requestId);
    if (existing) {
      const narration = this.store.getNarration(sessionId, existing.sequence);
      return {
        kind: "resolved",
        playerInput: existing.playerInput,
        intent: existing.action,
        resolution: { ...existing.resolution, action: existing.action, state: existing.stateAfter },
        narrationStatus: narration ? "ready" : "pending",
        narration: narration?.text,
        directionOptions: listNarrativeDirections(this.storyPackage, existing.stateAfter, narration?.continuity),
      };
    }

    const session = this.store.getSession(sessionId);
    const parsed = this.actionParser.parse(actionInput);
    if (parsed.kind === "clarification") return { ...parsed, state: session.currentState };

    const resolution = this.turnService.play(sessionId, requestId, parsed.intent, playerInput);
    const progression = selectNarrativeProgression(
      this.storyPackage,
      resolution.state,
      this.store.getLatestNarration(sessionId)?.continuity,
      resolution.endingId,
    );
    const narration = this.narrate(session.currentState, playerInput, parsed.intent, resolution, progression);
    const event = this.store.findEventByRequestId(sessionId, requestId);
    if (!event) {
      return {
        kind: "resolved",
        playerInput,
        intent: parsed.intent,
        resolution,
        narrationStatus: "ready",
        narration,
        directionOptions: listNarrativeDirections(this.storyPackage, resolution.state, progression.continuity),
      };
    }

    try {
      const stored = this.store.saveNarration(sessionId, event.sequence, { text: narration, continuity: progression.continuity });
      return {
        kind: "resolved",
        playerInput,
        intent: parsed.intent,
        resolution,
        narrationStatus: "ready",
        narration: stored.text,
        directionOptions: listNarrativeDirections(this.storyPackage, resolution.state, stored.continuity),
      };
    } catch {
      return {
        kind: "resolved",
        playerInput,
        intent: parsed.intent,
        resolution,
        narrationStatus: "pending",
        directionOptions: listNarrativeDirections(this.storyPackage, resolution.state, progression.continuity),
      };
    }
  }

  private narrate(
    previousState: GameState,
    playerInput: string,
    intent: ActionIntent,
    resolution: TurnResolution,
    progression: ReturnType<typeof selectNarrativeProgression>,
  ) {
    return this.narrator.narrate({
      storyPackage: this.storyPackage,
      playerInput,
      intent,
      previousState: structuredClone(previousState),
      resolution: structuredClone(resolution),
      progression: structuredClone(progression),
    });
  }
}
