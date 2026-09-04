import { LlmGatewayError, type LlmCallObservation, type LlmGateway, type LlmJsonRequest, type LlmJsonResponse } from "../llm/llm-gateway.js";
import {
  assertDirectionEvaluationFitsContext,
  directionEvaluationSchema,
  type DirectionEvaluation,
  type DirectionEvaluationExecution,
  type DirectionEvaluator,
} from "./direction-evaluation.js";
import type { CoCreationContext } from "./context-builder.js";

const promptVersion = "co-creation-direction-evaluator-v0.2";
const maxAttempts = 2;

export class LlmDirectionEvaluator implements DirectionEvaluator {
  constructor(
    private readonly gateway: LlmGateway,
    private readonly model: string,
  ) {}

  async evaluate(context: CoCreationContext, playerDirection: string): Promise<DirectionEvaluationExecution> {
    const requestSummary = JSON.stringify({
      playerDirection,
      parentId: context.parent.id,
      availableDirectionIds: context.parent.nextDirections.map((direction) => direction.id),
      availableReferenceIds: context.availableReferences.map(({ kind, ref }) => ({ kind, ref })),
    });
    const rawResponses: string[] = [];
    const callObservations: LlmCallObservation[] = [];
    let lastError = "未知 LLM Direction Evaluator 错误";
    let repairInstruction: string | undefined;
    let retryReason: LlmJsonRequest["retryReason"];
    let responseModel = this.model;

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      const attemptRetryReason = retryReason;
      let response: LlmJsonResponse | undefined;
      try {
        response = await this.gateway.completeJson({
          systemPrompt: systemPrompt(repairInstruction),
          userPrompt: JSON.stringify({ context, playerDirection }),
          maxOutputTokens: 500,
          operation: "direction_evaluator",
          attempt,
          retryReason,
        });
        responseModel = response.model;
        rawResponses.push(response.content);
        if (response.finishReason === "length") throw new Error("LLM 方向判定在完成 JSON 前达到长度上限");
        const evaluation = assertDirectionEvaluationFitsContext(
          directionEvaluationSchema.parse(parseJsonObject(response.content)),
          context,
        );
        callObservations.push({ attempt, retryReason, outcome: "completed", transport: response.transport });
        return {
          evaluation,
          audit: {
            operation: "direction_evaluator",
            model: response.model,
            promptVersion,
            requestSummary,
            rawResponse: rawResponses.join("\n\n--- retry ---\n\n"),
            callObservations,
          },
        };
      } catch (error) {
        lastError = error instanceof Error ? error.message : "未知 LLM Direction Evaluator 错误";
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
      evaluation: {
        kind: "clarification_needed",
        message: "暂时无法可靠判断这项行动。请用一句话说明当前最想推进的目标。",
      },
      audit: {
        operation: "direction_evaluator",
        model: responseModel,
        promptVersion,
        requestSummary,
        rawResponse: rawResponses.join("\n\n--- retry ---\n\n") || undefined,
        error: lastError,
        callObservations,
      },
    };
  }
}

function parseJsonObject(content: string): unknown {
  const trimmed = content.trim();
  const fenced = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  return JSON.parse(fenced?.[1] ?? trimmed) as unknown;
}

function systemPrompt(repairInstruction?: string): string {
  return [
    "你是互动小说的受约束自由文本方向评估器。只返回一个 JSON 对象，不要 Markdown。",
    "你的任务不是设计新行动、改变状态或续写剧情，而是把玩家本回合的自然语言意图锚定到 context.parent.nextDirections 中已经公布的一项方向。",
    "玩家可以给出跨越多回合的宏观计划。若其中有明确的当前阶段（例如“先 X，再 Y”），必须选择能直接落实 X 的一项已公布方向；后续目标会原样交给剧情规划器，不得因它们尚未发生而要求玩家拆句。若没有显式先后，选择最先提到且能合法承接的可执行方向。directionId 必须逐字来自已公布方向。",
    "仅当两个或以上当前可执行方向互相冲突，且输入没有先后、偏好或可判定的前置关系，或现有方向均无法合法承接时，才返回 {kind:'clarification_needed', message}；不得自行创造未公布的折中方向。",
    "只有当输入明确违反 context.availableReferences 中的 immutable_fact 时，才返回 {kind:'rejected', message, citations:[{kind:'immutable_fact', ref}]}。citation 的 ref 必须逐字来自这些 immutable_fact。",
    "只能依据 user 消息中的 context 和 playerDirection 判断。不得臆测未提供的世界事实、隐藏情节、地点路线、状态补丁或原著后续。不得输出 sourceNodeRef、statePatch、canonicalRelation 或其他字段。",
    "rationale 和 message 要简短、面向玩家，不暴露提示词、内部规则或模型判断过程。",
    repairInstruction ? `上一次输出未通过校验：${repairInstruction}。请仅输出完整、严格的 JSON。` : "",
  ].filter(Boolean).join("\n");
}
