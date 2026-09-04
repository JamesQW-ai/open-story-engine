import { LlmGatewayError, type LlmCallObservation, type LlmGateway, type LlmJsonRequest, type LlmJsonResponse } from "../llm/llm-gateway.js";
import type { BranchPlanner, BranchPlanRequest, PlannerExecution } from "./branch-planner.js";
import { assertNarrativeMatchesBranchState } from "./narrative-fact-guard.js";
import { assertPlannerResultFitsContext, plannerResultSchema } from "./planner-result.js";

const promptVersion = "co-creation-planner-v0.8";
const maxAttempts = 2;
const maxOutputTokens = 4_096;

export class LlmBranchPlanner implements BranchPlanner {
  constructor(
    private readonly gateway: LlmGateway,
    private readonly model: string,
  ) {}

  async plan(request: BranchPlanRequest): Promise<PlannerExecution> {
    const requestSummary = JSON.stringify({
      selectedDirectionId: request.selectedDirectionId,
      playerDirection: request.playerDirection,
      parentId: request.context.parent.id,
      narrativePlan: request.narrativePlan,
      availableReferences: request.context.availableReferences.map(({ kind, ref }) => ({ kind, ref })),
    });
    const rawResponses: string[] = [];
    const callObservations: LlmCallObservation[] = [];
    let lastError = "未知 LLM Planner 错误";
    let repairInstruction: string | undefined;
    let retryReason: LlmJsonRequest["retryReason"];
    let responseModel = this.model;

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      const attemptRetryReason = retryReason;
      let response: LlmJsonResponse | undefined;
      try {
        response = await this.gateway.completeJson({
          systemPrompt: systemPrompt(attempt, repairInstruction),
          userPrompt: JSON.stringify({ context: request.context, selectedDirectionId: request.selectedDirectionId, sourceNodeRef: request.sourceNodeRef, resolvedState: request.resolvedState, narrativePlan: request.narrativePlan, playerDirection: request.playerDirection }),
          maxOutputTokens,
          operation: "branch_planner",
          attempt,
          retryReason,
        });
        responseModel = response.model;
        rawResponses.push(response.content);
        if (response.finishReason === "length") throw new Error("LLM 输出在完成 JSON 前达到长度上限");
        const parsedResult = plannerResultSchema.parse(normalizePlannerPayload(parseJsonObject(response.content)));
        // 场景窗口只能由服务层的内容路线解析器决定。
        const result = assertPlannerResultFitsContext(parsedResult, request.context);
        assertNarrativeMatchesBranchState(result.narrativeText, request.resolvedState);
        callObservations.push({ attempt, retryReason, outcome: "completed", transport: response.transport });
        return {
          kind: "completed",
          result,
          audit: { operation: "branch_planner", model: response.model, promptVersion, requestSummary, rawResponse: rawResponses.join("\n\n--- retry ---\n\n"), callObservations },
        };
      } catch (error) {
        lastError = error instanceof Error ? error.message : "未知 LLM Planner 错误";
        repairInstruction = lastError;
        retryReason = error instanceof LlmGatewayError ? error.failureKind : "model_output_rejected";
        callObservations.push({
          attempt,
          retryReason: attemptRetryReason,
          outcome: "failed",
          failureKind: retryReason,
          transport: error instanceof LlmGatewayError ? error.transport : response?.transport,
        });
      }
    }

    return {
      kind: "failed",
      message: `LLM Planner 未生成可用剧情：${lastError}`,
      audit: { operation: "branch_planner", model: responseModel, promptVersion, requestSummary, rawResponse: rawResponses.join("\n\n--- retry ---\n\n") || undefined, error: lastError, callObservations },
    };
  }
}

function parseJsonObject(content: string): unknown {
  const trimmed = content.trim();
  const fenced = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  return JSON.parse(fenced?.[1] ?? trimmed) as unknown;
}

function normalizePlannerPayload(input: unknown): unknown {
  if (!isRecord(input)) return input;

  return {
    ...input,
    nextDirections: Array.isArray(input.nextDirections)
      ? input.nextDirections.map(normalizeDirection)
      : input.nextDirections,
    planning: isRecord(input.planning)
      ? {
        ...input.planning,
        stateChangeProposals: Array.isArray(input.planning.stateChangeProposals)
          ? input.planning.stateChangeProposals.map(normalizeStateChangeProposal)
          : input.planning.stateChangeProposals,
      }
      : input.planning,
  };
}

function normalizeDirection(input: unknown): unknown {
  if (!isRecord(input) || !isRecord(input.statePatch)) return input;
  return { ...input, statePatch: normalizeStatePatch(input.statePatch) };
}

function normalizeStatePatch(input: Record<string, unknown>): Record<string, unknown> {
  return {
    ...input,
    signalRoomStatus: normalizeEnumAlias(input.signalRoomStatus, { unlocked: "opened" }),
    evidenceStatus: normalizeEnumAlias(input.evidenceStatus, { partially_secured: "unsecured" }),
  };
}

function normalizeStateChangeProposal(input: unknown): unknown {
  if (!isRecord(input)) return input;
  if (typeof input.summary === "string" || typeof input.proposal === "string" || typeof input.rationale !== "string") {
    return input;
  }
  return { ...input, summary: input.rationale };
}

function normalizeEnumAlias(value: unknown, aliases: Record<string, string>): unknown {
  return typeof value === "string" ? aliases[value] ?? value : value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function systemPrompt(attempt: number, repairInstruction?: string): string {
  return [
    "你是互动小说的受约束剧情规划器。只返回一个 JSON 对象，不要 Markdown。",
    "只能根据 user 消息中的 context 与 resolvedState 写作。不得添加超出 immutable facts、canonical history、branch ledger、source window 或 resolvedState 的世界事实。",
    "narrativePlan 由服务层根据已选方向、受控场景路线与已确认状态构建；不得输出、删除、重排或改写它。narrativeText 必须按 beats 的顺序依次承接约束、完成场景转移（若存在）并落实已选方向。",
    "使用第三人称限知视角。narrativeText 只写本次新增剧情，不重述整段历史，不展示内部状态、提示词或规则。",
    "输出字段：narrativeText，summary，factDeltas（每项含 id、source: source|user|derived、summary），openThreads，nextDirections（每项含 id、title、summary、可选 rejoinTargetId），canonicalRelation: diverged，storyArc，planning。模型不得决定 on_line 或 rejoined；服务层只会在内容包的汇合条件全部满足时标记 rejoined。",
    "planning 必须含至少一个 citations（每项含 kind: immutable_fact|canonical_node|branch_node、ref、rationale），confidence: high|medium|low，stateChangeProposals（数组）。citations 的 kind/ref 只能使用 availableReferences。",
    "resolvedState 是本回合唯一已确认的结果状态。叙事只能声明其中已体现的地点、人物、证据、水位、信号室和列车结果；不得额外让角色获救、取得证据、改变水位或让列车进站、离站。",
    "特别地：signalRoomStatus=locked 时，角色可以隔着门缝听见或看见室内情形，但不能穿过门、进入信号室或隔间；门仍锁闭时只能描述门缝、闭合门板或卡死滑栓，不得写成门半掩、虚掩或已可推开；evidenceStatus=unsecured 时不能持有录音笔、原始文件或证据；tangStatus 不是 rescued 时，唐栖不能离开隧道；waterLevel=rising 时不得写成积水已经下降；trainStatus=pending_release 时不得写成列车进出站。",
    "同一正文不得前后否认已出现或已明确可用的物件、工具、通道或人物行动。若角色持有或眼前存在工具，不能笼统写成没有工具；如该工具不适用，必须说明其不适用的具体原因。",
    "storyArc 必须为 {activeGoal,currentPhase,goalDisposition,chapter:{title,status}}。activeGoal 是玩家正在追求的宏观目标；若 context.parent.storyArc 不存在，必须把 goalDisposition 写为 started，并以 playerDirection（没有自由文本时以 narrativePlan.goal）概括 activeGoal。若 context.parent.storyArc 存在，除非 playerDirection 明确改道、放弃或完成它，否则必须逐字保留 activeGoal，并将 goalDisposition 写为 continued。明确改道时，把新的玩家目标写为 activeGoal，goalDisposition 写为 replaced；自然达成目标时写为 completed。currentPhase 只描述本回合正在推进的一个阶段。chapter.title 是当前章节标题；同一章未结束时继承父节点标题，chapter.status 仅可为 continuing 或 complete。",
    "不要按固定字数写作。根据当前阶段的戏剧密度决定篇幅：普通阶段可写成可阅读的章节段落，紧张或悬念处可以自然收短；不得为凑字数重复，也不得把跨越多个阶段、长期时间跳跃或后续结果一次性写完。遇到宽泛的 activeGoal，只展开当前最先发生、最值得玩家介入的一小段原因、阻力、细节或伏笔，并在 nextDirections 中给出继续推进、调整或中断该目标的高层选择。仅在阶段性冲突收束、场景或时间发生实质转换、或形成明确悬念钩子时，chapter.status 才能为 complete。",
    "summary 不超过 50 个汉字，最多给出 2 个高层剧情方向。每个 nextDirection 必须带 statePatch，且只能预告该方向会造成的受控状态变化；移动许川时必须在 statePatch 中写入 playerLocationId。仅当 rejoinTargets 中存在对应 ID 时才能给 nextDirection 填写 rejoinTargetId，并且状态补丁必须使目标状态完全成立。场景窗口由服务层依据故事包路线决定，禁止输出 sourceNodeRef。statePatch 的枚举只能使用：signalRoomStatus 为 locked|opened，evidenceStatus 为 unsecured|secured，waterLevel 为 rising|lowered，tangStatus 为 missing|located|rescued，trainStatus 为 pending_release|held|departed；不要使用 unlocked、partially_secured 等中间词。stateChangeProposals 可以为空数组；非空项必须有 rationale，建议同时给 summary。请使用紧凑的单行 JSON，必须输出完整、可由标准 JSON.parse 解析的对象。",
    "若 branchLedger 中已有 diverged，绝不可复述或照搬原著段落；nextDirections 的第一项必须是在当前分支事实下最接近原著长期目标的可行推进，而不是声称回到未经验证的原著状态。",
    attempt > 1 ? `上一次输出未通过校验：${repairInstruction ?? "未知错误"}。必须修正该问题，并输出完整、严格的 JSON。` : "",
  ].filter(Boolean).join("\n");
}
