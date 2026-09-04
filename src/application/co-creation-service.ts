import type { StoryPackage } from "../content/story-package.js";
import { createEntryBranchNode } from "../domain/co-creation/branch-node.js";
import { createCanonicalBranch } from "../domain/co-creation/canonical-branch.js";
import { applyBranchStatePatch } from "../domain/co-creation/branch-state.js";
import { CoCreationContextBuilder } from "../domain/co-creation/context-builder.js";
import {
  assertDirectionEvaluationFitsContext,
  type DirectionEvaluation,
  type DirectionEvaluator,
} from "../domain/co-creation/direction-evaluation.js";
import { type BranchPlanner } from "../domain/co-creation/branch-planner.js";
import { type PlannedBranchNode } from "../domain/co-creation/mock-branch-planner.js";
import { assertPlannerResultFitsContext, plannerResultSchema } from "../domain/co-creation/planner-result.js";
import { buildNarrativePlan } from "../domain/co-creation/narrative-plan.js";
import { resolveRejoin } from "../domain/co-creation/rejoin-resolver.js";
import { resolveSceneNodeId } from "../domain/co-creation/scene-route-resolver.js";
import { createSessionStoryContract, type CreateSessionStoryContractInput, type SessionStoryContract } from "../domain/co-creation/session-story-contract.js";
import { SqliteSessionStore, type StoredBranchNode, type StoredDirectionEvaluation, type StoredLlmAudit } from "../infrastructure/sqlite/session-store.js";

export class CoCreationService {
  constructor(
    private readonly storyPackage: StoryPackage,
    private readonly store: SqliteSessionStore,
    private readonly planner: BranchPlanner,
    private readonly directionEvaluator: DirectionEvaluator,
    private readonly contextBuilder = new CoCreationContextBuilder(),
  ) {}

  start(input: CreateSessionStoryContractInput): { contract: SessionStoryContract; root: StoredBranchNode } {
    const contract = createSessionStoryContract(this.storyPackage, input);
    const root = createEntryBranchNode(this.storyPackage, contract);
    this.store.saveSessionStoryContract(contract);
    return { contract, root: this.store.createBranchRoot(contract.sessionId, root) };
  }

  async continue(
    sessionId: string,
    parentId: string,
    selectedDirectionId: string,
    playerDirection?: string,
    requestId?: string,
  ): Promise<StoredBranchNode> {
    const contract = this.store.getSessionStoryContract(sessionId);
    const parent = this.store.getBranchNode(sessionId, parentId);
    if (requestId) {
      const existing = this.store.findBranchNodeByRequestId(sessionId, requestId);
      if (existing) {
        if (existing.parentId !== parentId || existing.selectedDirectionId !== selectedDirectionId) {
          throw new Error(`requestId 已绑定到另一项共创选择: ${requestId}`);
        }
        return existing;
      }
      const existingEvaluation = this.store.findDirectionEvaluationByRequestId(sessionId, requestId);
      if (existingEvaluation) {
        if (existingEvaluation.parentBranchId !== parentId || existingEvaluation.kind !== "accepted" || existingEvaluation.directionId !== selectedDirectionId) {
          throw new Error(`requestId 已绑定到另一项共创选择: ${requestId}`);
        }
      }
    }
    if (!parent.nextDirections.some((direction) => direction.id === selectedDirectionId)) {
      throw new Error(`当前分支不存在可选方向: ${selectedDirectionId}`);
    }
    const lineage = this.store.listBranchLineage(sessionId, parent.id);
    const selectedDirection = parent.nextDirections.find((direction) => direction.id === selectedDirectionId);
    if (!selectedDirection) throw new Error(`当前分支不存在可选方向: ${selectedDirectionId}`);
    const parentSourceNodeId = parent.sourceNodeRef ?? contract.entryNodeId;
    const sourceNodeRef = resolveSceneNodeId(
      this.storyPackage,
      parentSourceNodeId,
      parent.branchState.playerLocationId,
      selectedDirection.statePatch,
    );
    const canonicalBranch = isPublishedDirectionSelection(playerDirection) && lineage.every((node) => node.canonicalRelation === "on_line")
      ? createCanonicalBranch(this.storyPackage, selectedDirection)
      : undefined;
    const resolvedState = applyBranchStatePatch(this.storyPackage, parent.branchState, selectedDirection.statePatch, sourceNodeRef);
    const rejoin = resolveRejoin(
      this.storyPackage,
      parentSourceNodeId,
      parent.openThreads,
      sourceNodeRef,
      resolvedState,
      selectedDirection,
    );
    if (rejoin && !lineage.some((node) => node.canonicalRelation === "diverged")) {
      throw new Error(`只有已偏离原著的分支可以汇合: ${rejoin.id}`);
    }
    const narrativePlan = buildNarrativePlan(this.storyPackage, {
      direction: selectedDirection,
      currentState: parent.branchState,
      sourceNodeRef: parentSourceNodeId,
      targetNodeRef: sourceNodeRef,
      openThreads: parent.openThreads,
    });
    if (canonicalBranch) {
      if (canonicalBranch.sourceNodeRef !== sourceNodeRef) {
        throw new Error(`规范方向场景路线与目标锚点不一致: ${selectedDirection.id}`);
      }
      if (JSON.stringify(canonicalBranch.branchState) !== JSON.stringify(resolvedState)) {
        throw new Error(`规范方向状态快照与补丁不一致: ${selectedDirection.id}`);
      }
      this.assertPublishedDirections(canonicalBranch.sourceNodeRef, canonicalBranch.branchState, canonicalBranch.nextDirections);
      return this.store.appendBranchNode(sessionId, parent.id, {
        ...canonicalBranch,
        // 原著复用不调用 Planner；它只能继承既有叙事目标，不能静默改写目标或章节。
        storyArc: parent.storyArc,
        narrativePlan,
        selectedDirectionId,
        playerDirection,
        requestId,
      });
    }
    const context = this.contextBuilder.build(this.storyPackage, contract, lineage, sourceNodeRef);
    const execution = await this.planner.plan({ context, selectedDirectionId, sourceNodeRef, resolvedState, narrativePlan, playerDirection });
    if (execution.audit) this.store.saveLlmAudit(sessionId, execution.audit);
    if (execution.kind === "failed") throw new Error(execution.message);
    const result = assertPlannerResultFitsContext(plannerResultSchema.parse(execution.result), context);
    const rejoinTargetBeat = rejoin
      ? this.storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === rejoin.targetBeatId)
      : undefined;
    if (rejoin && !rejoinTargetBeat) throw new Error(`汇合目标锚点不存在: ${rejoin.targetBeatId}`);
    const planned: PlannedBranchNode & { branchState: typeof resolvedState } = {
      ...result,
      sourceNodeRef,
      branchState: resolvedState,
      canonicalRelation: rejoin ? "rejoined" : "diverged",
      // 汇合后的可选方向是内容包已发布的目标锚点，不接受模型重新发明场景迁移。
      nextDirections: rejoinTargetBeat
        ? rejoinTargetBeat.nextDirections.map(({ id, title, summary, canonicalBeatId, rejoinTargetId, statePatch }) => ({ id, title, summary, canonicalBeatId, rejoinTargetId, statePatch }))
        : result.nextDirections,
      narrativePlan,
      selectedDirectionId,
      playerDirection,
      requestId,
    };
    this.assertPublishedDirections(sourceNodeRef, resolvedState, planned.nextDirections);
    return this.store.appendBranchNode(sessionId, parent.id, planned);
  }

  async continueWithPlayerDirection(sessionId: string, parentId: string, playerDirection: string, requestId?: string): Promise<
    | { kind: "accepted"; node: StoredBranchNode; rationale: string }
    | Exclude<DirectionEvaluation, { kind: "accepted" }>
  > {
    const contract = this.store.getSessionStoryContract(sessionId);
    const parent = this.store.getBranchNode(sessionId, parentId);
    const context = this.buildPlanningContext(sessionId, contract, parent);
    const existingEvaluation = requestId ? this.store.findDirectionEvaluationByRequestId(sessionId, requestId) : undefined;
    if (existingEvaluation && (existingEvaluation.parentBranchId !== parent.id || existingEvaluation.playerDirection !== playerDirection)) {
      throw new Error(`requestId 已绑定到另一项自由文本方向: ${requestId}`);
    }
    let evaluation: DirectionEvaluation;
    if (existingEvaluation) {
      evaluation = existingEvaluation;
    } else {
      const execution = await this.directionEvaluator.evaluate(context, playerDirection);
      if (execution.audit) this.store.saveDirectionEvaluatorAudit(sessionId, parent.id, execution.audit);
      evaluation = assertDirectionEvaluationFitsContext(execution.evaluation, context);
      this.store.saveDirectionEvaluation(sessionId, parent.id, playerDirection, evaluation, requestId);
    }
    if (evaluation.kind !== "accepted") return evaluation;
    return {
      kind: "accepted",
      node: await this.continue(sessionId, parentId, evaluation.directionId, playerDirection, requestId),
      rationale: evaluation.rationale,
    };
  }

  history(sessionId: string): StoredBranchNode[] {
    return this.store.listBranchNodes(sessionId);
  }

  current(sessionId: string): StoredBranchNode {
    const nodes = this.history(sessionId);
    const latest = nodes.at(-1);
    if (!latest) throw new Error(`共创会话没有分支节点: ${sessionId}`);
    return latest;
  }

  getContract(sessionId: string): SessionStoryContract {
    return this.store.getSessionStoryContract(sessionId);
  }

  directionAudits(sessionId: string): StoredDirectionEvaluation[] {
    return this.store.listDirectionEvaluations(sessionId);
  }

  llmAudits(sessionId: string): StoredLlmAudit[] {
    return this.store.listLlmAudits(sessionId);
  }

  directionEvaluatorAudits(sessionId: string) {
    return this.store.listDirectionEvaluatorAudits(sessionId);
  }

  private buildPlanningContext(sessionId: string, contract: SessionStoryContract, parent: StoredBranchNode) {
    return this.contextBuilder.build(this.storyPackage, contract, this.store.listBranchLineage(sessionId, parent.id));
  }

  private assertPublishedDirections(sourceNodeId: string, state: StoredBranchNode["branchState"], directions: StoredBranchNode["nextDirections"]): void {
    for (const direction of directions) {
      resolveSceneNodeId(this.storyPackage, sourceNodeId, state.playerLocationId, direction.statePatch);
    }
  }
}

function isPublishedDirectionSelection(playerDirection: string | undefined): boolean {
  return !playerDirection || playerDirection.startsWith("选择方向：");
}
