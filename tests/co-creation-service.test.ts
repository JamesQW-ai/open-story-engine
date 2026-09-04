import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { CoCreationService } from "../src/application/co-creation-service.js";
import { parseStoryPackage } from "../src/content/story-package.js";
import { CoCreationContextBuilder, type CoCreationContext } from "../src/domain/co-creation/context-builder.js";
import { LlmDirectionEvaluator } from "../src/domain/co-creation/llm-direction-evaluator.js";
import { MockDirectionEvaluator } from "../src/domain/co-creation/mock-direction-evaluator.js";
import { MockBranchPlanner } from "../src/domain/co-creation/mock-branch-planner.js";
import type { BranchPlanner } from "../src/domain/co-creation/branch-planner.js";
import { LlmBranchPlanner } from "../src/domain/co-creation/llm-branch-planner.js";
import { LlmGatewayError, type LlmGateway } from "../src/domain/llm/llm-gateway.js";
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
    expect(rescuePath.nextDirections.map((direction) => direction.id)).toEqual([
      "direction_lower_water_without_proof",
      "direction_return_for_records",
    ]);
    expect(tokenPath.narrativePlan?.beats.map((beat) => beat.kind)).toEqual(["establish_constraint", "advance_direction"]);
    expect(rescuePath.narrativePlan).toMatchObject({
      directionId: "direction_rescue_first",
      sourceNodeRef: "node_arrival",
      targetNodeRef: "node_tunnel",
      selectedStatePatch: { playerLocationId: "location_signal_tunnel" },
    });
    expect(rescuePath.narrativePlan?.beats.map((beat) => beat.kind)).toEqual([
      "establish_constraint",
      "transition_scene",
      "advance_direction",
    ]);
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

  it("marks an explicitly compatible divergent route as rejoined without splicing a canonical excerpt", async () => {
    const { store, storyPackage, service, sessionId } = await createFixture();
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const rescuePath = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");
    const rejoined = await service.continue(sessionId, rescuePath.id, "direction_return_for_records");
    const targetBeat = storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === "beat_evidence_secured");

    expect(rejoined.canonicalRelation).toBe("rejoined");
    expect(rejoined.sourceNodeRef).toBe("node_records");
    expect(rejoined.branchState).toEqual(targetBeat?.branchState);
    expect(rejoined.narrativeText).not.toBe(targetBeat?.sourceExcerpt?.text);

    const continued = await service.continue(sessionId, rejoined.id, "direction_enter_tunnel_with_proof");
    expect(continued.canonicalRelation).toBe("diverged");
    expect(continued.narrativeText).not.toBe(storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === "beat_tunnel_with_proof")?.sourceExcerpt?.text);
    store.close();
  });

  it("publishes target-beat directions after a rejoin instead of accepting model-invented routes", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const mockPlanner = new MockBranchPlanner();
    const planner: BranchPlanner = {
      async plan(request) {
        const execution = await mockPlanner.plan(request);
        if (execution.kind !== "completed" || request.selectedDirectionId !== "direction_return_for_records") return execution;
        return {
          ...execution,
          result: {
            ...execution.result,
            nextDirections: [{
              id: "direction_model_invented_return",
              title: "直接返回候车厅",
              summary: "跳过已发布的隧道路线，直接回到候车厅。",
              statePatch: { playerLocationId: "location_waiting_hall" },
            }],
          },
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, planner, new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const rescuePath = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");
    const rejoined = await service.continue(sessionId, rescuePath.id, "direction_return_for_records");

    expect(rejoined.canonicalRelation).toBe("rejoined");
    expect(rejoined.nextDirections.map((direction) => direction.id)).toEqual(["direction_enter_tunnel_with_proof"]);
    store.close();
  });

  it("rejects a declared rejoin when the selected patch does not reach its target state", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const mockPlanner = new MockBranchPlanner();
    const planner: BranchPlanner = {
      async plan(request) {
        const execution = await mockPlanner.plan(request);
        if (execution.kind !== "completed" || request.selectedDirectionId !== "direction_rescue_first") return execution;
        return {
          ...execution,
          result: {
            ...execution.result,
            nextDirections: execution.result.nextDirections.map((direction) => direction.id === "direction_return_for_records"
              ? { ...direction, statePatch: { playerLocationId: "location_station_office" } }
              : direction),
          },
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, planner, new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const rescuePath = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");

    await expect(service.continue(sessionId, rescuePath.id, "direction_return_for_records")).rejects.toThrow("汇合目标状态不兼容");
    expect(service.history(sessionId)).toHaveLength(3);
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

  it("resolves a returned station-office direction through the content-owned scene route", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const mockPlanner = new MockBranchPlanner();
    const planner: BranchPlanner = {
      async plan(request) {
        if (request.selectedDirectionId === "direction_lower_water_without_proof") {
          return {
            kind: "completed",
            result: {
              narrativeText: "阀门终于转开，积水暂时退到隧道的裂缝里。唐栖仍被锁在信号室，许川意识到若要找到能撬开滑栓的工具，必须返回站务室。",
              summary: "水位降低后，许川决定回站务室寻找已知的维修工具。",
              factDeltas: [{ id: "fact_water_lowered", source: "derived", summary: "隧道水位已降低，信号室仍锁闭。" }],
              openThreads: ["信号室滑栓", "站务室中的维修工具"],
              nextDirections: [{
                id: "direction_return_to_office",
                title: "返回站务室",
                summary: "回到站务室检查已知的维修工具，再设法打开信号室。",
                statePatch: { playerLocationId: "location_station_office" },
                sourceNodeRef: "node_tunnel",
              }],
              canonicalRelation: "diverged",
              storyArc: {
                activeGoal: request.context.parent.storyArc?.activeGoal ?? "在水位再次上涨前救出唐栖。",
                currentPhase: "先降低隧道水位，再决定是否折返取工具。",
                goalDisposition: "continued",
                chapter: { title: request.context.parent.storyArc?.chapter.title ?? "雨夜救援", status: "continuing" },
              },
              planning: { citations: [{ kind: "immutable_fact", ref: "fact_tunnel_flooding", rationale: "排水只能暂时降低隧道风险。" }], confidence: "high", stateChangeProposals: [] },
            },
          } as never;
        }
        if (request.selectedDirectionId === "direction_return_to_office") {
          expect(request.sourceNodeRef).toBe("node_records");
          expect(request.context.sourceWindow.nodeId).toBe("node_records");
          expect(request.resolvedState.playerLocationId).toBe("location_station_office");
          return {
            kind: "completed",
            result: {
              narrativeText: "许川沿着已经退去一些的积水折回站务室。终端的冷光仍映在潮湿的墙面上，他没有虚构新的线索，只把目光放回已经确认存在的维修图纸和应急灯。",
              summary: "许川回到站务室，准备利用已知工具继续救援。",
              factDeltas: [{ id: "fact_returned_to_office", source: "derived", summary: "许川从隧道返回站务室。" }],
              openThreads: ["信号室滑栓", "唐栖的安危"],
              nextDirections: [],
              canonicalRelation: "diverged",
              storyArc: {
                activeGoal: request.context.parent.storyArc?.activeGoal ?? "在水位再次上涨前救出唐栖。",
                currentPhase: "回到站务室，利用既有工具继续救援。",
                goalDisposition: "completed",
                chapter: { title: request.context.parent.storyArc?.chapter.title ?? "雨夜救援", status: "complete" },
              },
              planning: { citations: [{ kind: "branch_node", ref: request.context.parent.id, rationale: "返回路线承接刚完成的排水。" }], confidence: "high", stateChangeProposals: [] },
            },
          };
        }
        return mockPlanner.plan(request);
      },
    };
    const service = new CoCreationService(storyPackage, store, planner, new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const rescuePath = await service.continue(sessionId, tokenPath.id, "direction_rescue_first");
    const valvePath = await service.continue(sessionId, rescuePath.id, "direction_lower_water_without_proof");
    const returned = await service.continue(sessionId, valvePath.id, "direction_return_to_office");

    expect(valvePath.nextDirections[0]).not.toHaveProperty("sourceNodeRef");
    expect(returned.sourceNodeRef).toBe("node_records");
    expect(returned.branchState).toMatchObject({
      playerLocationId: "location_station_office",
      waterLevel: "lowered",
      signalRoomStatus: "locked",
      tangStatus: "located",
    });
    store.close();
  });

  it("rejects a planner result that publishes a direction without a legal scene route", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const mockPlanner = new MockBranchPlanner();
    const planner: BranchPlanner = {
      async plan(request) {
        const execution = await mockPlanner.plan(request);
        if (execution.kind !== "completed" || request.selectedDirectionId !== "direction_rescue_first") return execution;
        return {
          ...execution,
          result: {
            ...execution.result,
            nextDirections: [{
              id: "direction_unknown_destination",
              title: "前往不存在的地点",
              summary: "尝试离开当前故事空间。",
              statePatch: { playerLocationId: "location_unknown" },
            }],
          },
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, planner, new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");

    await expect(service.continue(sessionId, tokenPath.id, "direction_rescue_first")).rejects.toThrow("当前场景没有到达目标地点的受控路线");
    expect(service.history(sessionId)).toHaveLength(2);
    store.close();
  });

  it("rejects a planner result that is not one of its parent's published directions", async () => {
    const { store, storyPackage, service, sessionId } = await createFixture();
    const { contract, root } = service.start({ sessionId });
    const context = new CoCreationContextBuilder().build(storyPackage, contract, store.listBranchLineage(sessionId, root.id));
    const execution = await new MockBranchPlanner().plan({
      context,
      selectedDirectionId: "direction_find_token",
      sourceNodeRef: "node_arrival",
      resolvedState: root.branchState,
      narrativePlan: {
        directionId: "direction_find_token",
        goal: "追查十七号柜线索。",
        sourceNodeRef: "node_arrival",
        targetNodeRef: "node_arrival",
        selectedStatePatch: { hasLockerToken: true },
        beats: [
          { kind: "establish_constraint", sourceNodeRef: "node_arrival", summary: "承接候车厅的既有局面。" },
          { kind: "advance_direction", sourceNodeRef: "node_arrival", summary: "落实十七号柜线索。" },
        ],
      },
    });
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

  it("anchors a broad free-text plan to its earliest legal phase and preserves the full goal for the generated chapter", async () => {
    const { store, service, sessionId } = await createFixture();
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const goal = "先让姜序带路去积水尽头确认唐栖的情况，在救援中查清事故真相，并阻止列车放行。";

    const result = await service.continueWithPlayerDirection(sessionId, tokenPath.id, goal);

    expect(result.kind).toBe("accepted");
    if (result.kind === "accepted") {
      expect(result.node).toMatchObject({
        canonicalRelation: "diverged",
        selectedDirectionId: "direction_rescue_first",
        playerDirection: goal,
        storyArc: { activeGoal: goal, goalDisposition: "started" },
      });
    }
    store.close();
  });

  it("uses the planner for free text even when its legal anchor has canonical source text", async () => {
    const { store, service, sessionId } = await createFixture();
    const { root } = service.start({ sessionId });
    const goal = "先追查十七号柜，再设法保护唐栖留下的录音。";

    const result = await service.continueWithPlayerDirection(sessionId, root.id, goal);

    expect(result.kind).toBe("accepted");
    if (result.kind === "accepted") {
      expect(result.node).toMatchObject({
        canonicalRelation: "diverged",
        selectedDirectionId: "direction_find_token",
        playerDirection: goal,
        storyArc: { activeGoal: goal, goalDisposition: "started" },
      });
      expect(result.node.factDeltas.some((delta) => delta.source === "source")).toBe(false);
    }
    store.close();
  });

  it("persists a broad player goal across phases and records an explicit override", async () => {
    const { store, storyPackage, service, sessionId } = await createFixture();
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const initialGoal = "先让姜序带路确认唐栖情况，再决定如何救援";
    const rescuePath = await service.continue(sessionId, tokenPath.id, "direction_rescue_first", initialGoal);
    const continued = await service.continue(sessionId, rescuePath.id, "direction_lower_water_without_proof", "选择方向：排开积水");
    const replacementGoal = "放弃立刻救援，先折返站务室保全录音，再重新组织救援";
    const overridden = await service.continue(sessionId, rescuePath.id, "direction_return_for_records", replacementGoal);

    expect(rescuePath.storyArc).toMatchObject({ activeGoal: initialGoal, goalDisposition: "started" });
    expect(continued.storyArc).toMatchObject({ activeGoal: initialGoal, goalDisposition: "continued" });
    expect(overridden.storyArc).toMatchObject({ activeGoal: replacementGoal, goalDisposition: "replaced" });
    expect(new CoCreationContextBuilder().build(storyPackage, service.getContract(sessionId), store.listBranchLineage(sessionId, continued.id)).parent.storyArc).toEqual(continued.storyArc);
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

  it("uses the LLM evaluator to map a semantically equivalent free-text action to a published direction", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const gateway: LlmGateway = {
      async completeJson(request) {
        const payload = JSON.parse(request.userPrompt) as { context: CoCreationContext; playerDirection: string };
        expect(payload.playerDirection).toBe("先把人从积水尽头的维修间带出来");
        expect(payload.context.parent.nextDirections.map((direction) => direction.id)).toContain("direction_rescue_first");
        return {
          model: "test-direction-model",
          content: JSON.stringify({
            kind: "accepted",
            directionId: "direction_rescue_first",
            rationale: "输入明确优先处理唐栖所在区域的救援。",
          }),
        };
      },
    };
    const service = new CoCreationService(
      storyPackage,
      store,
      new MockBranchPlanner(),
      new LlmDirectionEvaluator(gateway, "test-direction-model"),
    );
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const result = await service.continueWithPlayerDirection(sessionId, tokenPath.id, "先把人从积水尽头的维修间带出来");

    expect(result.kind).toBe("accepted");
    if (result.kind === "accepted") expect(result.node.selectedDirectionId).toBe("direction_rescue_first");
    expect(service.directionAudits(sessionId)).toMatchObject([{
      kind: "accepted",
      directionId: "direction_rescue_first",
      playerDirection: "先把人从积水尽头的维修间带出来",
    }]);
    expect(service.directionEvaluatorAudits(sessionId)).toMatchObject([{
      operation: "direction_evaluator",
      model: "test-direction-model",
      error: undefined,
      rawResponse: expect.stringContaining("direction_rescue_first"),
    }]);
    store.close();
  });

  it("reuses an accepted free-text request without invoking the evaluator or appending a second branch", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    let calls = 0;
    const gateway: LlmGateway = {
      async completeJson() {
        calls += 1;
        return {
          model: "test-direction-model",
          content: JSON.stringify({
            kind: "accepted",
            directionId: "direction_find_token",
            rationale: "输入明确要求追查十七号柜线索。",
          }),
        };
      },
    };
    const service = new CoCreationService(
      storyPackage,
      store,
      new MockBranchPlanner(),
      new LlmDirectionEvaluator(gateway, "test-direction-model"),
    );
    const { root } = service.start({ sessionId });
    const requestId = "free-text-idempotent-1";
    const first = await service.continueWithPlayerDirection(sessionId, root.id, "查清十七号柜留下的线索", requestId);
    const repeated = await service.continueWithPlayerDirection(sessionId, root.id, "查清十七号柜留下的线索", requestId);

    expect(first.kind).toBe("accepted");
    expect(repeated.kind).toBe("accepted");
    if (first.kind === "accepted" && repeated.kind === "accepted") {
      expect(repeated.node.id).toBe(first.node.id);
      expect(repeated.node.requestId).toBe(requestId);
    }
    expect(calls).toBe(1);
    expect(service.history(sessionId)).toHaveLength(2);
    expect(service.directionAudits(sessionId)).toHaveLength(1);
    expect(service.directionEvaluatorAudits(sessionId)).toHaveLength(1);
    store.close();
  });

  it("does not append a branch when the LLM evaluator invents a direction outside the parent context", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    let calls = 0;
    const gateway: LlmGateway = {
      async completeJson() {
        calls += 1;
        return {
          model: "test-direction-model",
          content: JSON.stringify({
            kind: "accepted",
            directionId: "direction_invented_by_model",
            rationale: "虚构方向。",
          }),
        };
      },
    };
    const service = new CoCreationService(
      storyPackage,
      store,
      new MockBranchPlanner(),
      new LlmDirectionEvaluator(gateway, "test-direction-model"),
    );
    const { root } = service.start({ sessionId });
    const result = await service.continueWithPlayerDirection(sessionId, root.id, "许川想另找一条秘密通道");

    expect(result).toMatchObject({ kind: "clarification_needed" });
    expect(calls).toBe(2);
    expect(service.history(sessionId)).toHaveLength(1);
    expect(service.directionAudits(sessionId)).toMatchObject([{ kind: "clarification_needed" }]);
    expect(service.directionEvaluatorAudits(sessionId)).toMatchObject([{
      error: expect.stringContaining("当前未公布的剧情方向"),
      rawResponse: expect.stringContaining("--- retry ---"),
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
      callObservations: [
        { attempt: 1, outcome: "failed", failureKind: "model_output_rejected" },
        { attempt: 2, outcome: "failed", failureKind: "model_output_rejected" },
      ],
    }]);
    store.close();
  });

  it("records SSE transport metadata when the gateway rejects a stream without content", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const invalidGateway: LlmGateway = {
      async completeJson() {
        throw new LlmGatewayError("LLM 流式响应缺少正文", "invalid_response", { durationMs: 120, responseMode: "sse", httpStatus: 200 });
      },
    };
    const service = new CoCreationService(storyPackage, store, new LlmBranchPlanner(invalidGateway, "test-model"), new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");

    await expect(service.continue(sessionId, tokenPath.id, "direction_rescue_first")).rejects.toThrow("LLM Planner 未生成可用剧情");
    expect(service.llmAudits(sessionId)).toMatchObject([{
      callObservations: [
        { attempt: 1, outcome: "failed", failureKind: "invalid_response", transport: { responseMode: "sse", httpStatus: 200, durationMs: 120 } },
        { attempt: 2, outcome: "failed", failureKind: "invalid_response", transport: { responseMode: "sse", httpStatus: 200, durationMs: 120 } },
      ],
    }]);
    expect(service.history(sessionId)).toHaveLength(2);
    store.close();
  });

  it("retries one incomplete LLM response before it appends a valid branch", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    let calls = 0;
    const requests: Parameters<LlmGateway["completeJson"]>[0][] = [];
    const gateway: LlmGateway = {
      async completeJson(request) {
        calls += 1;
        requests.push(request);
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
    expect(requests).toMatchObject([
      { operation: "branch_planner", attempt: 1, retryReason: undefined },
      { operation: "branch_planner", attempt: 2, retryReason: "model_output_rejected" },
    ]);
    expect(node.selectedDirectionId).toBe("direction_rescue_first");
    expect(service.history(sessionId)).toHaveLength(3);
    expect(service.llmAudits(sessionId)).toMatchObject([{
      error: undefined,
      rawResponse: expect.stringContaining("--- retry ---"),
      callObservations: [
        { attempt: 1, outcome: "failed", failureKind: "model_output_rejected" },
        { attempt: 2, outcome: "completed" },
      ],
    }]);
    store.close();
  });

  it("accepts a chapter-length LLM segment without a fixed literary word-count gate", async () => {
    const { store, storyPackage, sessionId } = await createFixture();
    const gateway: LlmGateway = {
      async completeJson(request) {
        const payload = JSON.parse(request.userPrompt) as { context: CoCreationContext; selectedDirectionId: string; playerDirection?: string };
        const execution = await new MockBranchPlanner().plan(payload);
        if (execution.kind !== "completed") throw new Error("expected mock planner output");
        return {
          model: "test-model",
          content: JSON.stringify({ ...execution.result, narrativeText: "雨".repeat(659) }),
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, new LlmBranchPlanner(gateway, "test-model"), new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");
    const node = await service.continue(sessionId, tokenPath.id, "direction_rescue_first", "先让姜序带路确认唐栖情况");

    expect([...node.narrativeText].length).toBe(659);
    expect(node.storyArc).toMatchObject({ activeGoal: "先让姜序带路确认唐栖情况", chapter: { status: "continuing" } });
    expect(service.llmAudits(sessionId)).toMatchObject([{ error: undefined }]);
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
            narrativePlan: { directionId: "direction_hold_train", beats: [] },
            narrativeText: extendNarrativeForLlmTest(execution.result.narrativeText),
            planning: {
              ...execution.result.planning,
              stateChangeProposals: [{ proposal: "许川靠近十七号柜。", rationale: "这是玩家已选择的剧情方向。" }],
            },
          }),
          transport: {
            durationMs: 340,
            responseMode: "json",
            httpStatus: 200,
            fallback: {
              reason: "sse_missing_content",
              initialAttempt: {
                durationMs: 120,
                responseMode: "sse",
                httpStatus: 200,
                failureKind: "invalid_response",
              },
            },
          },
        };
      },
    };
    const service = new CoCreationService(storyPackage, store, new LlmBranchPlanner(gateway, "test-model"), new MockDirectionEvaluator());
    const { root } = service.start({ sessionId });
    const tokenPath = await service.continue(sessionId, root.id, "direction_find_token");

    const node = await service.continue(sessionId, tokenPath.id, "direction_rescue_first", "许川请姜序先带路去找唐栖");
    expect(node.selectedDirectionId).toBe("direction_rescue_first");
    expect(node.sourceNodeRef).toBe("node_tunnel");
    expect(node.narrativePlan).toMatchObject({
      directionId: "direction_rescue_first",
      sourceNodeRef: "node_arrival",
      targetNodeRef: "node_tunnel",
    });
    expect(node.planning.stateChangeProposals).toEqual([{ summary: "许川靠近十七号柜。", rationale: "这是玩家已选择的剧情方向。" }]);
    expect(service.llmAudits(sessionId)).toMatchObject([{
      model: "test-model",
      error: undefined,
      callObservations: [{
        outcome: "completed",
        transport: {
          responseMode: "json",
          httpStatus: 200,
          fallback: {
            reason: "sse_missing_content",
            initialAttempt: { responseMode: "sse", httpStatus: 200, failureKind: "invalid_response" },
          },
        },
      }],
    }]);
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
