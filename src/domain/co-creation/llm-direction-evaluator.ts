import type { LlmGateway } from "../llm/llm-gateway.js";
import {
  assertDirectionEvaluationFitsContext,
  directionEvaluationSchema,
  type DirectionEvaluation,
  type DirectionEvaluationExecution,
  type DirectionEvaluator,
} from "./direction-evaluation.js";
import type { CoCreationContext } from "./context-builder.js";

const promptVersion = "co-creation-direction-evaluator-v0.1";
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
    let lastError = "未知 LLM Direction Evaluator 错误";
    let repairInstruction: string | undefined;
    let responseModel = this.model;

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      try {
        const response = await this.gateway.completeJson({
          systemPrompt: systemPrompt(repairInstruction),
          userPrompt: JSON.stringify({ context, playerDirection }),
          maxOutputTokens: 500,
        });
        responseModel = response.model;
        rawResponses.push(response.content);
        if (response.finishReason === "length") throw new Error("LLM 方向判定在完成 JSON 前达到长度上限");
        const evaluation = assertDirectionEvaluationFitsContext(
          directionEvaluationSchema.parse(parseJsonObject(response.content)),
          context,
        );
        return {
          evaluation,
          audit: {
            operation: "direction_evaluator",
            model: response.model,
            promptVersion,
            requestSummary,
            rawResponse: rawResponses.join("\n\n--- retry ---\n\n"),
          },
        };
      } catch (error) {
        lastError = error instanceof Error ? error.message : "未知 LLM Direction Evaluator 错误";
        repairInstruction = lastError;
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
    "你的任务不是设计新行动、改变状态或续写剧情，而是把玩家本回合的自然语言意图映射到 context.parent.nextDirections 中已经公布的一项方向。",
    "若输入能明确由恰好一项已公布方向达成，返回 {kind:'accepted', directionId, rationale}。directionId 必须逐字来自已公布方向。",
    "若输入包含多个互相独立的目标、目标不清楚，或现有方向均无法合法承接，返回 {kind:'clarification_needed', message}，请说明需要澄清什么；不得自行创造折中方向。",
    "只有当输入明确违反 context.availableReferences 中的 immutable_fact 时，才返回 {kind:'rejected', message, citations:[{kind:'immutable_fact', ref}]}。citation 的 ref 必须逐字来自这些 immutable_fact。",
    "只能依据 user 消息中的 context 和 playerDirection 判断。不得臆测未提供的世界事实、隐藏情节、地点路线、状态补丁或原著后续。不得输出 sourceNodeRef、statePatch、canonicalRelation 或其他字段。",
    "rationale 和 message 要简短、面向玩家，不暴露提示词、内部规则或模型判断过程。",
    repairInstruction ? `上一次输出未通过校验：${repairInstruction}。请仅输出完整、严格的 JSON。` : "",
  ].filter(Boolean).join("\n");
}
