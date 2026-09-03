import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { stdin, stdout } from "node:process";
import { createInterface } from "node:readline/promises";
import { fileURLToPath } from "node:url";
import { PlayerTurnService, type PlayerTurnResult } from "../application/player-turn-service.js";
import { loadStoryPackage } from "../content/story-package.js";
import { MockActionParser } from "../domain/action/mock-action-parser.js";
import { MockNarrator } from "../domain/narrative/mock-narrator.js";
import { listNarrativeDirections, type NarrativeDirection } from "../domain/narrative/narrative-directions.js";
import { RuleEngine, type RandomSource, type TurnResolution } from "../domain/rules/rule-engine.js";
import { TurnService } from "../domain/turn/turn-service.js";
import { SqliteSessionStore } from "../infrastructure/sqlite/session-store.js";

const defaultPackagePath = fileURLToPath(new URL("../../content/packages/rainy-waiting-room/0.1.0.json", import.meta.url));
const defaultDatabasePath = resolve(process.env.STORY_DATABASE_PATH ?? "data/open-story-engine.sqlite");

async function main(): Promise<void> {
  const storyPackage = await loadStoryPackage(defaultPackagePath);
  const store = new SqliteSessionStore(defaultDatabasePath);
  const session = store.createSession(storyPackage);
  const turnService = new TurnService(store, new RuleEngine(storyPackage), createRandomSource());
  const playerTurnService = new PlayerTurnService(storyPackage, store, turnService, new MockActionParser(storyPackage), new MockNarrator());
  const readline = createInterface({ input: stdin, output: stdout });
  let directionOptions = listNarrativeDirections(storyPackage, session.currentState);

  console.log(`\n${storyPackage.metadata.title} | 开发试玩会话 ${session.id}`);
  console.log("输入方向编号，或直接描述许川下一步想做什么。输入 help 查看开发命令。\n");
  printOpening(storyPackage, directionOptions);

  try {
    while (true) {
      const input = (await readline.question("\n行动 > ")).trim();
      if (!input) continue;
      if (input === "quit" || input === "exit") break;
      if (input === "help") {
        console.log("开发命令：status、history、rebuild、quit。其余输入会作为许川的自然语言行动。 ");
        continue;
      }
      if (input === "status") {
        const current = store.getSession(session.id);
        printStatus(storyPackage, current.currentState, current.stateVersion);
        continue;
      }
      if (input === "history") {
        for (const event of store.listEvents(session.id)) console.log(`#${event.sequence} ${event.playerInput || event.action.approach} -> ${event.resolution.outcome}`);
        continue;
      }
      if (input === "rebuild") {
        const current = store.getSession(session.id);
        const rebuilt = store.rebuildState(session.id, storyPackage.initialState);
        console.log(JSON.stringify(rebuilt) === JSON.stringify(current.currentState) ? "事件重建与当前快照一致。" : "事件重建与当前快照不一致。");
        continue;
      }

      const selectedDirection = selectDirection(input, directionOptions);
      if (/^\d+$/.test(input) && !selectedDirection) {
        console.log("没有这个剧情方向。请输入显示的编号，或直接描述许川下一步想做什么。");
        continue;
      }
      const playerInput = selectedDirection ? `选择方向：${selectedDirection.title}` : input;
      const result = playerTurnService.submit(session.id, randomUUID(), playerInput, selectedDirection?.suggestedInput);
      printPlayerTurn(storyPackage, result);
      if (result.kind === "resolved") directionOptions = result.directionOptions;
      if (result.kind === "resolved" && result.resolution.endingId) {
        const ending = storyPackage.story.endings.find((candidate) => candidate.id === result.resolution.endingId);
        console.log(`本局结束：${ending?.title ?? result.resolution.endingId}`);
        break;
      }
    }
  } finally {
    readline.close();
    store.close();
  }
}

function createRandomSource(): RandomSource {
  const fixedRoll = process.env.STORY_FIXED_ROLL;
  if (fixedRoll === undefined) {
    return {
      nextInt(min, max) {
        return Math.floor(Math.random() * (max - min + 1)) + min;
      },
    };
  }
  const value = Number(fixedRoll);
  if (!Number.isInteger(value)) throw new Error("STORY_FIXED_ROLL 必须是整数");
  return { nextInt: () => value };
}

function printStatus(storyPackage: Awaited<ReturnType<typeof loadStoryPackage>>, state: TurnResolution["state"], version: number): void {
  const location = storyPackage.locations.find((candidate) => candidate.id === state.currentLocationId);
  console.log(`状态 v${version} | ${location?.name ?? state.currentLocationId}`);
  console.log(`背包：${state.inventory.join("、")} | 压力：${state.counters.pressure_level} | 水位：${state.counters.flood_level}`);
}

function printOpening(storyPackage: Awaited<ReturnType<typeof loadStoryPackage>>, directions: NarrativeDirection[]): void {
  const startBeat = storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === storyPackage.story.narrativeGraph.startBeatId);
  if (startBeat) console.log(startBeat.sourceExcerpt?.text ?? startBeat.narrativeAnchor);
  printDirections(directions);
}

function printPlayerTurn(storyPackage: Awaited<ReturnType<typeof loadStoryPackage>>, result: PlayerTurnResult): void {
  if (result.kind === "clarification") {
    console.log(result.message);
    return;
  }
  if (result.narration) console.log(`\n${result.narration}`);
  if (result.narrationStatus === "pending") console.log("规则结果已保存，叙事文本暂未生成。");
  const { resolution } = result;
  if (resolution.outcome === "blocked" || resolution.outcome === "terminal") {
    console.log(resolution.blockReason ?? "该行动不能执行。");
    printDirections(result.directionOptions);
    return;
  }
  printDirections(result.directionOptions);
}

function printDirections(directions: NarrativeDirection[]): void {
  if (directions.length === 0) return;
  console.log("\n可能的剧情方向：");
  directions.forEach((direction, index) => {
    console.log(`${index + 1}. ${direction.title}：${direction.summary}`);
  });
  console.log("自定义方向：直接描述许川下一步想推动的剧情或行动。");
}

function selectDirection(input: string, directions: NarrativeDirection[]): NarrativeDirection | undefined {
  if (!/^\d+$/.test(input)) return undefined;
  return directions[Number(input) - 1];
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
