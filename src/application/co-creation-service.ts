import type { StoryPackage } from "../content/story-package.js";
import { createEntryBranchNode } from "../domain/co-creation/branch-node.js";
import { createCanonicalBranch } from "../domain/co-creation/canonical-branch.js";
import { applyBranchStatePatch } from "../domain/co-creation/branch-state.js";
import { CoCreationContextBuilder } from "../domain/co-creation/context-builder.js";
import { type DirectionEvaluation } from "../domain/co-creation/direction-evaluation.js";
import { type DirectionEvaluator } from "../domain/co-creation/mock-direction-evaluator.js";
import { type BranchPlanner } from "../domain/co-creation/branch-planner.js";
import { type PlannedBranchNode } from "../domain/co-creation/mock-branch-planner.js";
import { assertPlannerResultFitsContext } from "../domain/co-creation/planner-result.js";
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
  ): Promise<StoredBranchNode> {
    const contract = this.store.getSessionStoryContract(sessionId);
    const parent = this.store.getBranchNode(sessionId, parentId);
    if (!parent.nextDirections.some((direction) => direction.id === selectedDirectionId)) {
      throw new Error(`当前分支不存在可选方向: ${selectedDirectionId}`);
    }
    const lineage = this.store.listBranchLineage(sessionId, parent.id);
    const selectedDirection = parent.nextDirections.find((direction) => direction.id === selectedDirectionId);
    if (!selectedDirection) throw new Error(`当前分支不存在可选方向: ${selectedDirectionId}`);
    const canonicalBranch = lineage.every((node) => node.canonicalRelation === "on_line")
      ? createCanonicalBranch(this.storyPackage, selectedDirection)
      : undefined;
    const sourceNodeRef = canonicalBranch?.sourceNodeRef ?? selectedDirection.sourceNodeRef ?? parent.sourceNodeRef ?? contract.entryNodeId;
    const resolvedState = applyBranchStatePatch(this.storyPackage, parent.branchState, selectedDirection.statePatch, sourceNodeRef);
    if (canonicalBranch) {
      if (JSON.stringify(canonicalBranch.branchState) !== JSON.stringify(resolvedState)) {
        throw new Error(`规范方向状态快照与补丁不一致: ${selectedDirection.id}`);
      }
      return this.store.appendBranchNode(sessionId, parent.id, { ...canonicalBranch, selectedDirectionId, playerDirection });
    }
    const context = this.contextBuilder.build(this.storyPackage, contract, lineage, sourceNodeRef);
    const execution = await this.planner.plan({ context, selectedDirectionId, sourceNodeRef, resolvedState, playerDirection });
    if (execution.audit) this.store.saveLlmAudit(sessionId, execution.audit);
    if (execution.kind === "failed") throw new Error(execution.message);
    const result = assertPlannerResultFitsContext(execution.result, context);
    const planned: PlannedBranchNode & { branchState: typeof resolvedState } = {
      ...result,
      sourceNodeRef,
      branchState: resolvedState,
      selectedDirectionId,
      playerDirection,
    };
    return this.store.appendBranchNode(sessionId, parent.id, planned);
  }

  async continueWithPlayerDirection(sessionId: string, parentId: string, playerDirection: string): Promise<
    | { kind: "accepted"; node: StoredBranchNode; rationale: string }
    | Exclude<DirectionEvaluation, { kind: "accepted" }>
  > {
    const contract = this.store.getSessionStoryContract(sessionId);
    const parent = this.store.getBranchNode(sessionId, parentId);
    const context = this.buildPlanningContext(sessionId, contract, parent);
    const evaluation = this.directionEvaluator.evaluate(context, playerDirection);
    this.store.saveDirectionEvaluation(sessionId, parent.id, playerDirection, evaluation);
    if (evaluation.kind !== "accepted") return evaluation;
    return {
      kind: "accepted",
      node: await this.continue(sessionId, parentId, evaluation.directionId, playerDirection),
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

  private buildPlanningContext(sessionId: string, contract: SessionStoryContract, parent: StoredBranchNode) {
    return this.contextBuilder.build(this.storyPackage, contract, this.store.listBranchLineage(sessionId, parent.id));
  }
}
