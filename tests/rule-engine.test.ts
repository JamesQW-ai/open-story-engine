import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parseStoryPackage } from "../src/content/story-package.js";
import { RuleEngine, type ActionIntent, type GameState } from "../src/domain/rules/rule-engine.js";

const packagePath = fileURLToPath(new URL("../content/packages/rainy-waiting-room/0.1.0.json", import.meta.url));
const highRolls = { nextInt: () => 6 };

async function createEngine(): Promise<{ engine: RuleEngine; initialState: GameState }> {
  const rawPackage = JSON.parse(await readFile(packagePath, "utf8")) as unknown;
  const storyPackage = parseStoryPackage(rawPackage);
  return { engine: new RuleEngine(storyPackage), initialState: structuredClone(storyPackage.initialState) };
}

function action(actionType: ActionIntent["actionType"], targetId: string): ActionIntent {
  return { actionType, targetId, approach: "测试使用的自然语言意图归类" };
}

describe("RuleEngine", () => {
  it("blocks actions whose guards are not satisfied without mutating state", async () => {
    const { engine, initialState } = await createEngine();
    const result = engine.resolve(initialState, action("risk", "location_station_office"), highRolls);
    expect(result.outcome).toBe("blocked");
    expect(result.blockReason).toBe("行动前置条件未满足");
    expect(result.state).toEqual(initialState);
  });

  it("reaches the canonical original ending through the four-node path", async () => {
    const { engine, initialState } = await createEngine();
    let state = initialState;
    const canonicalActions: ActionIntent[] = [
      action("investigate", "item_locker_token"), action("risk", "location_station_office"), action("investigate", "item_recorder"),
      action("negotiate", "character_jiang_xu"), action("risk", "item_relief_valve"), action("risk", "item_signal_door"),
      action("negotiate", "character_train_driver"),
    ];
    const results = canonicalActions.map((playerAction) => {
      const result = engine.resolve(state, playerAction, highRolls);
      state = result.state;
      return result;
    });

    expect(results.every((result) => result.outcome === "success")).toBe(true);
    expect(state.currentNodeId).toBe("node_aftermath");
    expect(state.flags.flag_tang_rescued).toBe(true);
    expect(state.flags.flag_evidence_secured).toBe(true);
    expect(state.flags.flag_train_departure_held).toBe(true);
    expect(results.at(-1)?.endingId).toBe("ending_truth_survives");
  });
});
