import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parseStoryPackage } from "../src/content/story-package.js";
import { RuleEngine, type ActionIntent } from "../src/domain/rules/rule-engine.js";
import { TurnService } from "../src/domain/turn/turn-service.js";
import { SqliteSessionStore } from "../src/infrastructure/sqlite/session-store.js";

const packagePath = fileURLToPath(new URL("../content/packages/rainy-waiting-room/0.1.0.json", import.meta.url));
const highRolls = { nextInt: () => 6 };

async function createFixture(): Promise<{ store: SqliteSessionStore; storyPackage: ReturnType<typeof parseStoryPackage>; sessionId: string; turnService: TurnService }> {
  const storyPackage = parseStoryPackage(JSON.parse(await readFile(packagePath, "utf8")) as unknown);
  const store = new SqliteSessionStore(":memory:");
  const session = store.createSession(storyPackage, "session-test");
  return { store, storyPackage, sessionId: session.id, turnService: new TurnService(store, new RuleEngine(storyPackage), highRolls) };
}

function action(actionType: ActionIntent["actionType"], targetId: string): ActionIntent {
  return { actionType, targetId, approach: "测试回合" };
}

describe("SqliteSessionStore and TurnService", () => {
  it("does not persist a blocked action", async () => {
    const { store, sessionId, turnService } = await createFixture();
    const result = turnService.play(sessionId, "request-blocked", action("risk", "location_station_office"));

    expect(result.outcome).toBe("blocked");
    expect(store.listEvents(sessionId)).toHaveLength(0);
    expect(store.getSession(sessionId).stateVersion).toBe(0);
    store.close();
  });

  it("rebuilds the canonical session state from immutable event patches", async () => {
    const { store, storyPackage, sessionId, turnService } = await createFixture();
    const actions: ActionIntent[] = [
      action("investigate", "item_locker_token"), action("risk", "location_station_office"), action("investigate", "item_recorder"),
      action("negotiate", "character_jiang_xu"), action("risk", "item_relief_valve"), action("risk", "item_signal_door"),
      action("negotiate", "character_train_driver"),
    ];

    actions.forEach((playerAction, index) => turnService.play(sessionId, `request-${index}`, playerAction));

    const persisted = store.getSession(sessionId);
    const rebuilt = store.rebuildState(sessionId, storyPackage.initialState);
    expect(store.listEvents(sessionId)).toHaveLength(7);
    expect(persisted.stateVersion).toBe(7);
    expect(persisted.status).toBe("terminal");
    expect(rebuilt).toEqual(persisted.currentState);
    expect(rebuilt.flags.flag_train_departure_held).toBe(true);
    store.close();
  });

  it("returns the original result for a duplicate request without adding an event", async () => {
    const { store, sessionId, turnService } = await createFixture();
    const playerAction = action("investigate", "item_locker_token");
    const first = turnService.play(sessionId, "request-idempotent", playerAction);
    const repeated = turnService.play(sessionId, "request-idempotent", playerAction);

    expect(repeated).toEqual(first);
    expect(store.listEvents(sessionId)).toHaveLength(1);
    expect(store.getSession(sessionId).stateVersion).toBe(1);
    store.close();
  });
});
