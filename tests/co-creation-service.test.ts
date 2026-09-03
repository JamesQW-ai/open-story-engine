import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { CoCreationService } from "../src/application/co-creation-service.js";
import { parseStoryPackage } from "../src/content/story-package.js";
import { CoCreationContextBuilder, type CoCreationContext } from "../src/domain/co-creation/context-builder.js";
import { MockDirectionEvaluator } from "../src/domain/co-creation/mock-direction-evaluator.js";
import { MockBranchPlanner } from "../src/domain/co-creation/mock-branch-planner.js";
import type { BranchPlanner } from "../src/domain/co-creation/branch-planner.js";
import { LlmBranchPlanner } from "../src/domain/co-creation/llm-branch-planner.js";
import type { LlmGateway } from "../src/domain/llm/llm-gateway.js";
import { createSessionStoryContract } from "../src/domain/co-creation/session-story-contract.js";
import { SqliteSessionStore } from "../src/infrastructure/sqlite/session-store.js";

const packagePath = fileURLToPath(new URL("../content/packages/rainy-waiting-room/0.1.0.json", import.meta.url));

function extendNarrativeForLlmTest(narrativeText: string): string {
  return narrativeText.repeat(8);
}

async function createFixture(): Promise<{ store: SqliteSessionStore; storyPackage: ReturnType<typeof parseStoryPackage>; service: CoCreationService; sessionId: string }> {
  const storyPackage = parseStoryPackage(JSON.parse(await readFile(packagePath, "utf8")) as unknown);
  const store = new SqliteSessionStore(":memory:");
  const session = store.createSession(storyPackage, "co-creation-session");
  return { store, storyPackage, service: new CoCreationService(storyPackage, store, new MockBranchPlanner(), new MockDirectionEvaluator()), sessionId: session.id };
}

describe("CoCreationService", () => {
  it("derives the immutable source contract and canonical prefix for an entry node", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const contract = createSessionStoryContract(storyPackage, { sessionId, entryNodeId: "node_tunnel" });

    expect(contract.sourcePackageRef).toEqual({ id: "rainy-waiting-room", version: "0.1.0" });
    expect(contract.canonicalPrefixNodeIds).toEqual(["node_arrival", "node_records", "node_tunnel"]);
    expect(contract.immutableFactRefs).toContain("fact_no_supernatural");
    expect(contract.persona).toMatchObject({ kind: "source_character", sourceCharacterId: "character_xu_chuan" });
    store.close();
  });

  it("persists two alternative child branches without replacing their shared source history", async () => {
    const { store, service, sessionId } = await createFixture();
    const { contract, root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token", "选择方向：追查十七号柜");
    const evidencePath = await service.continue(sessionId, tokenPath.id, "direction_secure_evidence", "选择方向：先取得证据");
    const rescuePath = await service.continue(sessionId, tokenPath.id, "direction_rescue_first", "选择方向：救援优先");

    expect(contract.entryNodeId).toBe("node_arrival");
    expect(root.sequence).toBe(0);
    expect(tokenPath.parentId).toBe(root.id);
    expect(evidencePath.parentId).toBe(tokenPath.id);
    expect(evidencePath.canonicalRelation).toBe("on_line");
    expect(rescuePath.parentId).toBe(tokenPath.id);
    expect(rescuePath.canonicalRelation).toBe("diverged");
    expect(rescuePath.nextDirections.map((direction) => direction.id)).toEqual(["direction_lower_water_without_proof"]);
    expect(rescuePath.planning.citations).toEqual([{ kind: "immutable_fact", ref: "fact_tunnel_flooding", rationale: "隧道积水使救援优先具有即时合理性。" }]);
    expect(rescuePath.planning.stateChangeProposals).toEqual([]);
    expect(rescuePath.branchState).toMatchObject({
      playerLocationId: "location_signal_tunnel",
      jiangLocationId: "location_signal_tunnel",
      tangStatus: "located",
      tangLocationId: "location_signal_tunnel",
      evidenceStatus: "unsecured",
      trainStatus: "pending_release",
    });
    expect(service.history(sessionId)).toHaveLength(4);
    expect(store.getSessionStoryContract(sessionId)).toEqual(contract);
    store.close();
  });

  it("reuses the mapped source passage on the canonical route without invoking a planner", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    let plannerCalls = 0;
    const planner: BranchPlanner = {
      async plan() {
        plannerCalls += 1;
        throw new Error("canonical route must not invoke planner");
      },
    };
    const service = new CoCreationService(storyPackage, store, planner, new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });

    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const sourceBeat = storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === "beat_token_found");

    expect(plannerCalls).toBe(0);
    expect(tokenPath.canonicalRelation).toBe("on_line");
    expect(tokenPath.narrativeText).toBe(sourceBeat?.sourceExcerpt?.text);
    expect(tokenPath.nextDirections).toContainEqual(expect.objectContaining({
      id: "direction_secure_evidence",
      canonicalBeatId: "beat_office_entered",
    }));
    store.close();
  });

  it("invokes the planner for every later node after a branch diverges", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    let plannerCalls = 0;
    const mockPlanner = new MockBranchPlanner();
    const planner: BranchPlanner = {
      async plan(request) {
        plannerCalls += 1;
        return mockPlanner.plan(request);
      },
    };
    const service = new CoCreationService(storyPackage, store, planner, new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });

    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const rescuePath = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");
    const valvePath = await service.continue(sessionId, rescuePath.id, "direction_lower_water_without_proof");

    expect(rescuePath.canonicalRelation).toBe("diverged");
    expect(valvePath.canonicalRelation).toBe("diverged");
    expect(plannerCalls).toBe(2);
    expect(valvePath.narrativeText).not.toBe(storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === "beat_valve_with_proof")?.sourceExcerpt?.text);
    store.close();
  });

  it("rejects a generated direction whose state patch violates character continuity", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const mockPlanner = new MockBranchPlanner();
    const planner: BranchPlanner = {
      async plan(request) {
        const execution = await mockPlanner.plan(request);
        if (execution.kind !== "completed") return execution;
        return {
          ...execution,
          result: {
            ...execution.result,
            nextDirections: [{
              id: "direction_invalid_rescue",
              title: "仓促救援",
              summary: "直接宣称唐栖已经获救。",
              statePatch: { tangStatus: "rescued" },
            }],
          },
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, planner, new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const rescuePath = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");

    await expect(service.continue(sessionId, rescuePath.id, "direction_invalid_rescue")).rejects.toThrow("唐栖获救后必须回到候车厅");
    expect(service.history(sessionId)).toHaveLength(3);
    store.close();
  });

  it("rejects a planner result that is not one of its parent's published directions", async () => {
    const { store, storyPackage, service, sessionId } = await createFixture();
    const { contract, root } = service.start({ sessionId });
    const context = new CoCreationContextBuilder().build(storyPackage, contract, store.listBranchLineage(sessionId, root.id));
    const execution = await new MockBranchPlanner().plan({ context, selectedDirectionId: "direction_find_token" });
    if (execution.kind !== "completed") throw new Error("expected mock planner output");
    const planned = { ...execution.result, selectedDirectionId: "direction_not_published" };

    expect(() => store.appendBranchNode(sessionId, root.id, planned)).toThrow("共创子节点必须来自父节点已公布的剧情方向");
    expect(service.history(sessionId)).toHaveLength(1);
    store.close();
  });

  it("builds a bounded planning context from the selected branch lineage", async () => {
    const { store, storyPackage, service, sessionId } = await createFixture();
    const { contract, root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const rescuePath = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");
    const context = new CoCreationContextBuilder().build(storyPackage, contract, store.listBranchLineage(sessionId, rescuePath.id));

    expect(context.branchLedger.map((entry) => entry.id)).toEqual([root.id, tokenPath.id, rescuePath.id]);
    expect(context.sourceWindow.nodeId).toBe("node_tunnel");
    expect(context.availableReferences).toContainEqual({ kind: "immutable_fact", ref: "fact_tunnel_flooding", summary: "信号室排水泵故障，水位会持续威胁救援。" });
    expect(context.availableReferences.some((reference) => reference.kind === "branch_node" && reference.ref === rescuePath.id)).toBe(true);
    store.close();
  });

  it("maps an unambiguous natural-language direction to the current published branch", async () => {
    const { store, service, sessionId } = await createFixture();
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const result = await service.continueWithPlayerDirection(sessionId, tokenPath.id, "许川请姜序先带路去找唐栖");

    expect(result.kind).toBe("accepted");
    if (result.kind === "accepted") {
      expect(result.node.selectedDirectionId).toBe("direction_rescue_first");
      expect(result.node.playerDirection).toBe("许川请姜序先带路去找唐栖");
    }
    expect(service.directionAudits(sessionId)).toMatchObject([{
      kind: "accepted",
      parentBranchId: tokenPath.id,
      playerDirection: "许川请姜序先带路去找唐栖",
      directionId: "direction_rescue_first",
    }]);
    store.close();
  });

  it("rejects a free-text direction that violates an immutable world fact without appending a branch", async () => {
    const { store, service, sessionId } = await createFixture();
    const { root } = service.start({ sessionId });
    const result = await service.continueWithPlayerDirection(sessionId, root.id, "许川施展魔法让雨停下");

    expect(result).toMatchObject({ kind: "rejected", citations: [{ kind: "immutable_fact", ref: "fact_no_supernatural" }] });
    expect(service.history(sessionId)).toHaveLength(1);
    expect(service.directionAudits(sessionId)).toMatchObject([{
      kind: "rejected",
      parentBranchId: root.id,
      playerDirection: "许川施展魔法让雨停下",
      citations: [{ kind: "immutable_fact", ref: "fact_no_supernatural" }],
    }]);
    store.close();
  });

  it("completes both Mock planner routes without publishing a direction that lacks a template", async () => {
    const { store, service, sessionId } = await createFixture();
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const evidencePath = await service.continue(sessionId, tokenPath.id, "direction_secure_evidence");
    const recordsPath = await service.continue(sessionId, evidencePath.id, "direction_verify_records");
    const tunnelPath = await service.continue(sessionId, recordsPath.id, "direction_enter_tunnel_with_proof");
    const valvePath = await service.continue(sessionId, tunnelPath.id, "direction_lower_water_with_proof");
    const rescuePath = await service.continue(sessionId, valvePath.id, "direction_open_signal_room_with_proof");
    const canonicalEnding = await service.continue(sessionId, rescuePath.id, "direction_hold_train");
    const rescueFirst = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");
    const rescueValve = await service.continue(sessionId, rescueFirst.id, "direction_lower_water_without_proof");
    const divergentEnding = await service.continue(sessionId, rescueValve.id, "direction_open_signal_room_without_proof");

    expect(canonicalEnding.nextDirections).toEqual([]);
    expect(canonicalEnding.canonicalRelation).toBe("on_line");
    expect(divergentEnding.nextDirections).toEqual([]);
    expect(divergentEnding.canonicalRelation).toBe("diverged");
    store.close();
  });

  it("records an invalid LLM plan without appending a branch", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const invalidGateway: LlmGateway = {
      async completeJson() {
        return { model: "test-model", content: "{not valid json" };
      },
    };
    const service = new CoCreationService(storyPackage, store, new LlmBranchPlanner(invalidGateway, "test-model"), new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");

    await expect(service.continue(sessionId, tokenPath.id, "direction_rescue_first")).rejects.toThrow("LLM Planner 未生成可用剧情");
    expect(service.history(sessionId)).toHaveLength(2);
    expect(service.llmAudits(sessionId)).toMatchObject([{
      model: "test-model",
      rawResponse: expect.stringContaining("--- retry ---"),
      error: expect.any(String),
    }]);
    store.close();
  });

  it("retries one incomplete LLM response before it appends a valid branch", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    let calls = 0;
    const gateway: LlmGateway = {
      async completeJson(request) {
        calls += 1;
        if (calls === 1) return { model: "test-model", content: "{\"narrativeText\":\"incomplete" };
        const payload = JSON.parse(request.userPrompt) as { context: CoCreationContext; selectedDirectionId: string; playerDirection?: string };
        const execution = await new MockBranchPlanner().plan(payload);
        if (execution.kind !== "completed") throw new Error("expected mock planner output");
        return {
          model: "test-model",
          content: JSON.stringify({ ...execution.result, narrativeText: extendNarrativeForLlmTest(execution.result.narrativeText) }),
          finishReason: "stop",
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, new LlmBranchPlanner(gateway, "test-model"), new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");

    const node = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");
    expect(calls).toBe(2);
    expect(node.selectedDirectionId).toBe("direction_rescue_first");
    expect(service.history(sessionId)).toHaveLength(3);
    expect(service.llmAudits(sessionId)).toMatchObject([{
      error: undefined,
      rawResponse: expect.stringContaining("--- retry ---"),
    }]);
    store.close();
  });

  it("retries an LLM paragraph that crosses a signal room still marked locked", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    let calls = 0;
    const gateway: LlmGateway = {
      async completeJson(request) {
        calls += 1;
        const payload = JSON.parse(request.userPrompt) as { context: CoCreationContext; selectedDirectionId: string; playerDirection?: string };
        const execution = await new MockBranchPlanner().plan(payload);
        if (execution.kind !== "completed") throw new Error("expected mock planner output");
        const narrativeText = calls === 1
          ? "许川侧身挤过门缝，应急灯扫过狭窄的隔间，只见唐栖蜷在配电箱旁。"
          : execution.result.narrativeText;
        return {
          model: "test-model",
          content: JSON.stringify({ ...execution.result, narrativeText: extendNarrativeForLlmTest(narrativeText) }),
          finishReason: "stop",
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, new LlmBranchPlanner(gateway, "test-model"), new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const node = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");

    expect(calls).toBe(2);
    expect(node.narrativeText).not.toContain("侧身挤过门缝");
    expect(service.llmAudits(sessionId)).toMatchObject([{
      rawResponse: expect.stringContaining("--- retry ---"),
      error: undefined,
    }]);
    store.close();
  });

  it("validates an LLM plan before appending it and saves its audit", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const gateway: LlmGateway = {
      async completeJson(request) {
        const payload = JSON.parse(request.userPrompt) as { context: CoCreationContext; selectedDirectionId: string; playerDirection?: string };
        const execution = await new MockBranchPlanner().plan(payload);
        if (execution.kind !== "completed") throw new Error("expected mock planner output");
        return {
          model: "test-model",
          content: JSON.stringify({
            ...execution.result,
            sourceNodeRef: "branch_not_a_source_node",
            narrativeText: extendNarrativeForLlmTest(execution.result.narrativeText),
            planning: {
              ...execution.result.planning,
              stateChangeProposals: [{ proposal: "许川靠近十七号柜。", rationale: "这是玩家已选择的剧情方向。" }],
            },
          }),
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, new LlmBranchPlanner(gateway, "test-model"), new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");

    const node = await service.continue(sessionId, tokenPath.id, "direction_rescue_first", "许川请姜序先带路去找唐栖");
    expect(node.selectedDirectionId).toBe("direction_rescue_first");
    expect(node.sourceNodeRef).toBe("node_tunnel");
    expect(node.planning.stateChangeProposals).toEqual([{ summary: "许川靠近十七号柜。", rationale: "这是玩家已选择的剧情方向。" }]);
    expect(service.llmAudits(sessionId)).toMatchObject([{ model: "test-model", error: undefined }]);
    expect(service.history(sessionId)).toHaveLength(3);
    store.close();
  });

  it("normalizes known LLM state aliases and preserves rationale-only audit proposals", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const gateway: LlmGateway = {
      async completeJson(request) {
        const payload = JSON.parse(request.userPrompt) as { context: CoCreationContext; selectedDirectionId: string; playerDirection?: string };
        const execution = await new MockBranchPlanner().plan(payload);
        if (execution.kind !== "completed") throw new Error("expected mock planner output");
        return {
          model: "test-model",
          content: JSON.stringify({
            ...execution.result,
            narrativeText: extendNarrativeForLlmTest(execution.result.narrativeText),
            nextDirections: [
              { id: "direction_open_with_alias", title: "打开信号室", summary: "尝试打开信号室。", statePatch: { signalRoomStatus: "unlocked" } },
              { id: "direction_partial_evidence", title: "搜集线索", summary: "先取得一部分线索。", statePatch: { evidenceStatus: "partially_secured" } },
            ],
            planning: {
              ...execution.result.planning,
              stateChangeProposals: [{ rationale: "这些方向的实际状态变化仍会在被选择时校验。" }],
            },
          }),
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, new LlmBranchPlanner(gateway, "test-model"), new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const node = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");

    expect(node.nextDirections).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "direction_open_with_alias", statePatch: { signalRoomStatus: "opened" } }),
      expect.objectContaining({ id: "direction_partial_evidence", statePatch: { evidenceStatus: "unsecured" } }),
    ]));
    expect(node.planning.stateChangeProposals).toEqual([{
      summary: "这些方向的实际状态变化仍会在被选择时校验。",
      rationale: "这些方向的实际状态变化仍会在被选择时校验。",
    }]);
    store.close();
  });
});
