import { resolve } from "node:path";
import { stdin, stdout } from "node:process";
import { createInterface } from "node:readline/promises";
import { fileURLToPath } from "node:url";
import { CoCreationService } from "../application/co-creation-service.js";
import { loadStoryPackage } from "../content/story-package.js";
import { MockDirectionEvaluator } from "../domain/co-creation/mock-direction-evaluator.js";
import { MockBranchPlanner } from "../domain/co-creation/mock-branch-planner.js";
import type { BranchPlanner } from "../domain/co-creation/branch-planner.js";
import { LlmBranchPlanner } from "../domain/co-creation/llm-branch-planner.js";
import { OpenAiCompatibleGateway } from "../infrastructure/llm/openai-compatible-gateway.js";
import { SqliteSessionStore, type StoredBranchNode } from "../infrastructure/sqlite/session-store.js";

const defaultPackagePath = fileURLToPath(new URL("../../content/packages/rainy-waiting-room/0.1.0.json", import.meta.url));
const defaultDatabasePath = resolve(process.env.STORY_DATABASE_PATH ?? "data/open-story-engine.sqlite");

async function main(): Promise<void> {
  const storyPackage = await loadStoryPackage(defaultPackagePath);
  const { planner, label } = createPlanner();
  const store = new SqliteSessionStore(defaultDatabasePath);
  const session = store.createSession(storyPackage);
  const service = new CoCreationService(storyPackage, store, planner, new MockDirectionEvaluator());
  const { contract, root } = service.start({ sessionId: session.id });
  const readline = createInterface({ input: stdin, output: stdout });
  let current = root;

  console.log(`\n${storyPackage.metadata.title} | 共创树开发试玩 ${session.id}`);
  console.log(`原著前史：${contract.canonicalPrefixNodeIds.join(" -> ")}`);
  console.log(`当前使用 ${label}；输入方向编号或自然语言方向继续，输入 state 查看当前状态，输入 history 查看分支，输入 audits 查看方向判定，输入 llm-audits 查看模型调用，输入 quit 退出。\n`);
  printNode(current);

  try {
    while (true) {
      let input: string;
      try {
        input = (await readline.question("\n方向 > ")).trim();
      } catch (error) {
        if (error instanceof Error && error.message === "readline was closed") break;
        throw error;
      }
      if (!input) continue;
      if (input === "quit" || input === "exit") break;
      if (input === "history") {
        service.history(session.id).forEach((node) => {
          console.log(`#${node.sequence} ${node.canonicalRelation} ${node.selectedDirectionId ?? "source_entry"} -> ${node.summary}`);
        });
        continue;
      }
      if (input === "state") {
        printBranchState(storyPackage, current);
        continue;
      }
      if (input === "audits") {
        service.directionAudits(session.id).forEach((audit) => {
          const detail = audit.kind === "accepted" ? audit.directionId : audit.message;
          console.log(`#${audit.id} ${audit.kind} | ${audit.playerDirection} -> ${detail}`);
        });
        continue;
      }
      if (input === "llm-audits") {
        service.llmAudits(session.id).forEach((audit) => {
          console.log(`#${audit.id} ${audit.operation} | ${audit.model} | ${audit.error ?? "ok"}`);
        });
        continue;
      }
      if (!/^\d+$/.test(input)) {
        console.log("\n正在确认剧情连续性...");
        const result = await service.continueWithPlayerDirection(session.id, current.id, input);
        if (result.kind !== "accepted") {
          console.log(result.message);
          continue;
        }
        current = result.node;
        await printGeneratedNode(current);
        continue;
      }

      const selected = current.nextDirections[Number(input) - 1];
      if (!selected) {
        console.log("没有这个剧情方向。请输入显示的编号。");
        continue;
      }
      console.log("\n正在确认剧情连续性...");
      current = await service.continue(session.id, current.id, selected.id, `选择方向：${selected.title}`);
      await printGeneratedNode(current);
    }
  } finally {
    readline.close();
    store.close();
  }
}

function createPlanner(): { planner: BranchPlanner; label: string } {
  if ((process.env.STORY_PLANNER ?? "mock") === "mock") return { planner: new MockBranchPlanner(), label: "Mock Planner" };
  if (process.env.STORY_PLANNER !== "openai") throw new Error("STORY_PLANNER 仅支持 mock 或 openai");
  const apiKey = process.env.STORY_LLM_API_KEY?.trim();
  const model = process.env.STORY_LLM_MODEL?.trim();
  const baseUrl = process.env.STORY_LLM_BASE_URL?.trim();
  if (!apiKey || !model || !baseUrl) throw new Error("使用 STORY_PLANNER=openai 时必须设置 STORY_LLM_BASE_URL、STORY_LLM_API_KEY 与 STORY_LLM_MODEL");
  return {
    planner: new LlmBranchPlanner(new OpenAiCompatibleGateway({ apiKey, model, baseUrl }), model),
    label: `LLM Planner (${model})`,
  };
}

function printNode(node: StoredBranchNode): void {
  console.log(node.narrativeText);
  if (node.nextDirections.length === 0) return;
  console.log("\n可能的剧情方向：");
  node.nextDirections.forEach((direction, index) => {
    console.log(`${index + 1}. ${direction.title}：${direction.summary}`);
  });
}

async function printGeneratedNode(node: StoredBranchNode): Promise<void> {
  console.log(node.factDeltas.some((delta) => delta.source === "source") ? "\n原著段落：" : "\n剧情：");
  await writeProgressively(node.narrativeText);
  if (node.nextDirections.length === 0) return;
  console.log("\n\n可能的剧情方向：");
  node.nextDirections.forEach((direction, index) => {
    console.log(`${index + 1}. ${direction.title}：${direction.summary}`);
  });
}

function printBranchState(storyPackage: Awaited<ReturnType<typeof loadStoryPackage>>, node: StoredBranchNode): void {
  const locationName = (locationId: string) => storyPackage.locations.find((location) => location.id === locationId)?.name ?? locationId;
  const tangStatus = { missing: "失联", located: "已定位", rescued: "已获救" } as const;
  const evidenceStatus = { unsecured: "未取得", secured: "已保全" } as const;
  const waterLevel = { rising: "上涨中", lowered: "已降低" } as const;
  const signalRoom = { locked: "锁闭", opened: "已打开" } as const;
  const trainStatus = { pending_release: "等待放行", held: "已阻止放行", departed: "已离站" } as const;
  const state = node.branchState;

  console.log(`\n许川：${locationName(state.playerLocationId)}`);
  console.log(`唐栖：${tangStatus[state.tangStatus]}，${locationName(state.tangLocationId)}`);
  console.log(`姜序：${locationName(state.jiangLocationId)}`);
  console.log(`证据：${evidenceStatus[state.evidenceStatus]} | 水位：${waterLevel[state.waterLevel]} | 信号室：${signalRoom[state.signalRoomStatus]} | 列车：${trainStatus[state.trainStatus]}`);
}

async function writeProgressively(text: string): Promise<void> {
  const chunkSize = 12;
  for (let start = 0; start < text.length; start += chunkSize) {
    stdout.write(text.slice(start, start + chunkSize));
    await new Promise<void>((resolveDelay) => setTimeout(resolveDelay, 16));
  }
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
