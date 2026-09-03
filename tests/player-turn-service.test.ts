import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { PlayerTurnService } from "../src/application/player-turn-service.js";
import { parseStoryPackage } from "../src/content/story-package.js";
import { MockActionParser } from "../src/domain/action/mock-action-parser.js";
import { MockNarrator, type Narrator } from "../src/domain/narrative/mock-narrator.js";
import { RuleEngine } from "../src/domain/rules/rule-engine.js";
import { TurnService } from "../src/domain/turn/turn-service.js";
import { SqliteSessionStore } from "../src/infrastructure/sqlite/session-store.js";

const packagePath = fileURLToPath(new URL("../content/packages/rainy-waiting-room/0.1.0.json", import.meta.url));
const highRolls = { nextInt: () => 6 };

async function createFixture(narrator: Narrator = new MockNarrator()): Promise<{ store: SqliteSessionStore; sessionId: string; playerTurnService: PlayerTurnService }> {
  const storyPackage = parseStoryPackage(JSON.parse(await readFile(packagePath, "utf8")) as unknown);
  const store = new SqliteSessionStore(":memory:");
  const session = store.createSession(storyPackage, "player-turn-session");
  const turnService = new TurnService(store, new RuleEngine(storyPackage), highRolls);
  return { store, sessionId: session.id, playerTurnService: new PlayerTurnService(storyPackage, store, turnService, new MockActionParser(storyPackage), narrator) };
}

describe("PlayerTurnService", () => {
  it("maps a natural-language investigation, persists its original input, and stores narration", async () => {
    const { store, sessionId, playerTurnService } = await createFixture();
    const input = "许川俯身检查十七号柜旁的铜牌";
    const result = playerTurnService.submit(sessionId, "natural-language-1", input);

    expect(result.kind).toBe("resolved");
    if (result.kind !== "resolved") throw new Error("expected a resolved turn");
    expect(result.intent).toMatchObject({ actionType: "investigate", targetId: "item_locker_token" });
    expect(result.resolution.outcome).toBe("success");
    expect(result.narration).toContain("许川");
    expect(result.narration).toContain("铜牌");
    expect(result.narration).not.toContain("事情向前挪动了一步");
    expect(result.narration).not.toContain("眼下仍悬着的是");
    expect(result.directionOptions.map((direction) => direction.id)).toEqual(["direction_secure_evidence", "direction_rescue_first"]);
    expect(store.listEvents(sessionId)[0]?.playerInput).toBe(input);
    expect(store.getNarration(sessionId, 1)?.text).toBe(result.narration);
    expect(store.getNarration(sessionId, 1)?.continuity.beatId).toBe("beat_token_found");
    store.close();
  });

  it("returns clarification for an unrecognized input without persisting an event", async () => {
    const { store, sessionId, playerTurnService } = await createFixture();
    const result = playerTurnService.submit(sessionId, "natural-language-unknown", "许川站在原地听雨");

    expect(result.kind).toBe("clarification");
    expect(store.listEvents(sessionId)).toHaveLength(0);
    expect(store.getSession(sessionId).stateVersion).toBe(0);
    store.close();
  });

  it("continues from the prior narrative beat instead of repeating the opening", async () => {
    const { store, sessionId, playerTurnService } = await createFixture();
    playerTurnService.submit(sessionId, "continuity-1", "许川检查十七号柜旁的铜牌");
    const result = playerTurnService.submit(sessionId, "continuity-2", "他绕过人群进入站务室");

    expect(result.kind).toBe("resolved");
    if (result.kind !== "resolved") throw new Error("expected a resolved turn");
    expect(result.narration).toContain("穿过了站务室的封锁");
    expect(result.narration).not.toContain("候车厅的灯光在雨幕里忽明忽暗");
    expect(store.getNarration(sessionId, 2)?.continuity.beatId).toBe("beat_office_entered");
    store.close();
  });

  it("reaches the controlled rescue-first ending through a noncanonical narrative path", async () => {
    const { store, sessionId, playerTurnService } = await createFixture();
    const inputs = [
      "许川检查十七号柜旁的铜牌",
      "他请求姜序先带路去信号室",
      "他拧开隧道里的手动阀",
      "他拉开信号室的滑栓，救出唐栖",
    ];
    const results = inputs.map((input, index) => playerTurnService.submit(sessionId, `rescue-first-${index}`, input));
    const last = results.at(-1);

    expect(last?.kind).toBe("resolved");
    if (!last || last.kind !== "resolved") throw new Error("expected a resolved turn");
    expect(last.resolution.endingId).toBe("ending_rescue_without_proof");
    expect(last.resolution.state.flags.flag_evidence_secured).toBe(false);
    expect(last.narration).toContain("没有人能替他们证明");
    expect(store.getNarration(sessionId, 4)?.continuity.beatId).toBe("beat_ending_rescue_without_proof");
    store.close();
  });

  it("allows a different narrator to change only the text, not the confirmed state", async () => {
    const first = await createFixture(new MockNarrator());
    const second = await createFixture({ narrate: () => "替代叙事正文。" });
    const input = "许川检查储物柜的铜牌";
    const firstResult = first.playerTurnService.submit(first.sessionId, "narrator-a", input);
    const secondResult = second.playerTurnService.submit(second.sessionId, "narrator-b", input);

    expect(firstResult.kind).toBe("resolved");
    expect(secondResult.kind).toBe("resolved");
    if (firstResult.kind !== "resolved" || secondResult.kind !== "resolved") throw new Error("expected resolved turns");
    expect(firstResult.narration).not.toBe(secondResult.narration);
    expect(firstResult.resolution.state).toEqual(secondResult.resolution.state);
    expect(first.store.getSession(first.sessionId).currentState).toEqual(second.store.getSession(second.sessionId).currentState);
    expect(first.store.getNarration(first.sessionId, 1)?.continuity).toEqual(second.store.getNarration(second.sessionId, 1)?.continuity);
    first.store.close();
    second.store.close();
  });
});
