import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { CoCreationService } from "../application/co-creation-service.js";
import { loadStoryPackage } from "../content/story-package.js";
import { LlmDirectionEvaluator } from "../domain/co-creation/llm-direction-evaluator.js";
import { LlmBranchPlanner } from "../domain/co-creation/llm-branch-planner.js";
import { MockBranchPlanner } from "../domain/co-creation/mock-branch-planner.js";
import { LlmGatewayError, type LlmGateway, type LlmJsonRequest, type LlmJsonResponse, type LlmResponseMode } from "../domain/llm/llm-gateway.js";
import { OpenAiCompatibleGateway } from "../infrastructure/llm/openai-compatible-gateway.js";
import { SqliteSessionStore } from "../infrastructure/sqlite/session-store.js";

const defaultPackagePath = fileURLToPath(new URL("../../content/packages/rainy-waiting-room/0.1.0.json", import.meta.url));

type ScenarioResult = {
  id: string;
  status: "passed" | "failed";
  checks: string[];
  modelCalls: number;
  callObservations: LiveCallObservation[];
  error?: string;
};

type LiveCallObservation = {
  operation: NonNullable<LlmJsonRequest["operation"]> | "unknown";
  attempt?: number;
  retryReason?: LlmJsonRequest["retryReason"];
  durationMs: number;
  responseMode: LlmResponseMode;
  httpStatus?: number;
  outcome: "responded" | "failed";
  failureKind?: LlmGatewayError["failureKind"] | "unexpected";
};

type LiveInFlightCall = Pick<LiveCallObservation, "operation" | "attempt" | "retryReason"> & {
  scenarioId?: string;
  startedAt: string;
};

type Scenario = {
  id: string;
  run: () => Promise<string[]>;
};

type AcceptanceConclusion = {
  status: "passed" | "failed" | "incomplete";
  statement: string;
  verifiedEffects: string[];
  failedScenarioIds: string[];
  notProven: string[];
};

class BudgetedGateway implements LlmGateway {
  calls = 0;
  readonly observations: LiveCallObservation[] = [];
  scenarioId?: string;
  inFlightCall?: LiveInFlightCall;

  constructor(
    private readonly delegate: LlmGateway,
    private readonly maxCalls: number,
    private readonly onChange?: () => Promise<void>,
  ) {}

  async completeJson(request: LlmJsonRequest): Promise<LlmJsonResponse> {
    if (this.calls >= this.maxCalls) throw new Error(`真实模型评估已达到调用上限: ${this.maxCalls}`);
    this.calls += 1;
    this.inFlightCall = {
      operation: request.operation ?? "unknown",
      attempt: request.attempt,
      retryReason: request.retryReason,
      scenarioId: this.scenarioId,
      startedAt: new Date().toISOString(),
    };
    await this.onChange?.();
    const startedAt = performance.now();
    try {
      const response = await this.delegate.completeJson(request);
      this.observations.push({
        operation: request.operation ?? "unknown",
        attempt: request.attempt,
        retryReason: request.retryReason,
        durationMs: response.transport?.durationMs ?? Math.round(performance.now() - startedAt),
        responseMode: response.transport?.responseMode ?? "unknown",
        httpStatus: response.transport?.httpStatus,
        outcome: "responded",
      });
      return response;
    } catch (error) {
      const gatewayError = error instanceof LlmGatewayError ? error : undefined;
      this.observations.push({
        operation: request.operation ?? "unknown",
        attempt: request.attempt,
        retryReason: request.retryReason,
        durationMs: gatewayError?.transport.durationMs ?? Math.round(performance.now() - startedAt),
        responseMode: gatewayError?.transport.responseMode ?? "unknown",
        httpStatus: gatewayError?.transport.httpStatus,
        outcome: "failed",
        failureKind: gatewayError?.failureKind ?? "unexpected",
      });
      throw error;
    } finally {
      this.inFlightCall = undefined;
      await this.onChange?.();
    }
  }
}

async function main(): Promise<void> {
  if (process.env.STORY_LIVE_EVALUATION !== "1") {
    throw new Error("真实模型评估需要显式设置 STORY_LIVE_EVALUATION=1");
  }
  const apiKey = requiredEnvironment("STORY_LLM_API_KEY");
  const model = requiredEnvironment("STORY_LLM_MODEL");
  const baseUrl = requiredEnvironment("STORY_LLM_BASE_URL");
  const maxCalls = parseMaxCalls(process.env.STORY_LIVE_EVALUATION_MAX_CALLS);
  const storyPackage = await loadStoryPackage(defaultPackagePath);
  const results: ScenarioResult[] = [];
  let gateway: BudgetedGateway;
  const checkpoint = async (): Promise<void> => {
    await writeReportIfRequested(buildReport(storyPackage, model, maxCalls, gateway, results, "incomplete"));
  };
  gateway = new BudgetedGateway(new OpenAiCompatibleGateway({ apiKey, model, baseUrl, stream: false, timeoutMs: 60_000 }), maxCalls, checkpoint);
  const scenarios = selectScenarios(createScenarios(storyPackage, gateway, model), process.argv);

  for (const scenario of scenarios) {
    gateway.scenarioId = scenario.id;
    const callsBefore = gateway.calls;
    const observationsBefore = gateway.observations.length;
    try {
      results.push({
        id: scenario.id,
        status: "passed",
        checks: await scenario.run(),
        modelCalls: gateway.calls - callsBefore,
        callObservations: gateway.observations.slice(observationsBefore),
      });
    } catch (error) {
      results.push({
        id: scenario.id,
        status: "failed",
        checks: [],
        modelCalls: gateway.calls - callsBefore,
        callObservations: gateway.observations.slice(observationsBefore),
        error: error instanceof Error ? error.message : "未知真实模型评估错误",
      });
    }
    await writeReportIfRequested(buildReport(storyPackage, model, maxCalls, gateway, results, "incomplete"));
  }
  gateway.scenarioId = undefined;

  const report = buildReport(storyPackage, model, maxCalls, gateway, results, "completed");
  await writeReportIfRequested(report);
  console.log(JSON.stringify(report, null, 2));
  if (results.some((result) => result.status === "failed")) process.exitCode = 1;
}

function buildReport(
  storyPackage: Awaited<ReturnType<typeof loadStoryPackage>>,
  model: string,
  maxCalls: number,
  gateway: BudgetedGateway,
  results: ScenarioResult[],
  runStatus: "incomplete" | "completed",
) {
  return {
    runStatus,
    package: `${storyPackage.id}@${storyPackage.version}`,
    model,
    maxCalls,
    totalModelCalls: gateway.calls,
    results,
    inFlightCall: gateway.inFlightCall,
    conclusion: buildConclusion(results, runStatus),
  };
}

function buildConclusion(results: ScenarioResult[], runStatus: "incomplete" | "completed"): AcceptanceConclusion {
  const failedScenarioIds = results.filter((result) => result.status === "failed").map((result) => result.id);
  const verifiedEffects = results
    .filter((result) => result.status === "passed")
    .flatMap((result) => result.checks);

  if (runStatus === "incomplete") {
    return {
      status: "incomplete",
      statement: `真实模型验收尚未完成：已结束 ${results.length}/6 个场景，不能作为批量验收结论。`,
      verifiedEffects,
      failedScenarioIds,
      notProven: ["进程在完整六场景验收结束前中断，未完成场景和整体稳定性均未验证。"],
    };
  }

  return {
    status: failedScenarioIds.length === 0 ? "passed" : "failed",
    statement: failedScenarioIds.length === 0
      ? `本次真实模型验收通过：${results.length}/${results.length} 个场景均满足已编码的运行时约束。`
      : `本次真实模型验收未通过：${failedScenarioIds.length}/${results.length} 个场景失败。`,
    verifiedEffects,
    failedScenarioIds,
    notProven: [
      "本结论不衡量读者偏好、长期节奏或发布级文学质量。",
      "本结论不验证玩家 CLI 的 SSE 逐段展示；该传输路径另由网关测试和实际共创运行覆盖。",
      "本结论只对本次模型、StoryPackage 版本、调用时的服务响应和已执行场景成立。",
    ],
  };
}

function createScenarios(
  storyPackage: Awaited<ReturnType<typeof loadStoryPackage>>,
  gateway: LlmGateway,
  model: string,
): Scenario[] {
  return [
    {
      id: "canonical_route_skips_model",
      async run() {
        const { store, service, sessionId } = createService(storyPackage, new LlmBranchPlanner(gateway, model), new LlmDirectionEvaluator(gateway, model), "live-canonical");
        try {
          const { root } = service.start({ sessionId });
          const node = await service.continue(sessionId, root.id, "direction_find_token");
          assert(node.canonicalRelation === "on_line", "规范方向未复用规范节点");
          assert(service.llmAudits(sessionId).length === 0, "规范方向不应调用 LLM Planner");
          assert(service.directionEvaluatorAudits(sessionId).length === 0, "编号方向不应调用 LLM Direction Evaluator");
          return ["规范节点复用未调用模型"];
        } finally {
          store.close();
        }
      },
    },
    {
      id: "broad_goal_starts_current_phase",
      async run() {
        const { store, service, sessionId } = createService(storyPackage, new LlmBranchPlanner(gateway, model), new LlmDirectionEvaluator(gateway, model), "live-broad-goal");
        try {
          const { root } = service.start({ sessionId });
          const token = await service.continue(sessionId, root.id, "direction_find_token");
          const result = await service.continueWithPlayerDirection(
            sessionId,
            token.id,
            "先让姜序带路去积水尽头确认唐栖的情况，在救援中查清事故真相，并阻止列车放行。",
            "live-broad-goal-1",
          );
          assert(result.kind === "accepted", "宽泛目标没有被接受为合法方向");
          if (result.kind !== "accepted") throw new Error("宽泛目标没有生成分支节点");
          assert(result.node.selectedDirectionId === "direction_rescue_first", "宽泛目标没有锚定到救援优先");
          assert(result.node.canonicalRelation === "diverged", "宽泛目标应进入动态分支");
          assert(result.node.branchState.playerLocationId === "location_signal_tunnel", "救援优先没有切换到隧道场景");
          assert(result.node.storyArc?.goalDisposition === "started", "宽泛目标没有以 started 开始故事主线");
          assert(result.node.storyArc?.chapter.status === "continuing", "宽泛目标首阶段不应提前结束章节");
          assert(service.directionEvaluatorAudits(sessionId).at(-1)?.error === undefined, "方向评估审计包含错误");
          assert(service.llmAudits(sessionId).at(-1)?.error === undefined, "剧情规划审计包含错误");
          return ["宽泛目标锚定为救援优先", "主线以当前阶段开始且正文通过状态与场景校验"];
        } finally {
          store.close();
        }
      },
    },
    {
      id: "locked_signal_room_state",
      async run() {
        const { store, service, sessionId } = createService(storyPackage, new MockBranchPlanner(), new LlmDirectionEvaluator(gateway, model), "live-locked-room-setup");
        try {
          const { root } = service.start({ sessionId });
          const token = await service.continue(sessionId, root.id, "direction_find_token");
          const rescue = await service.continue(sessionId, token.id, "direction_rescue_first");
          const liveService = new CoCreationService(storyPackage, store, new LlmBranchPlanner(gateway, model), new LlmDirectionEvaluator(gateway, model));
          const node = await liveService.continue(sessionId, rescue.id, "direction_lower_water_without_proof");
          assert(node.branchState.waterLevel === "lowered", "排水方向未更新水位");
          assert(node.branchState.signalRoomStatus === "locked", "排水方向不应打开信号室");
          assert(node.branchState.tangStatus === "located", "排水方向不应让唐栖获救");
          assert(liveService.llmAudits(sessionId).at(-1)?.error === undefined, "锁闭场景正文未通过 LLM 校验");
          return ["锁闭信号室未被正文或状态越过", "排水后状态保持一致"];
        } finally {
          store.close();
        }
      },
    },
    {
      id: "controlled_rejoin_uses_new_narration",
      async run() {
        const { store, service, sessionId } = createService(storyPackage, new MockBranchPlanner(), new LlmDirectionEvaluator(gateway, model), "live-rejoin-setup");
        try {
          const { root } = service.start({ sessionId });
          const token = await service.continue(sessionId, root.id, "direction_find_token");
          const rescue = await service.continue(sessionId, token.id, "direction_rescue_first");
          const liveService = new CoCreationService(storyPackage, store, new LlmBranchPlanner(gateway, model), new LlmDirectionEvaluator(gateway, model));
          const rejoined = await liveService.continue(sessionId, rescue.id, "direction_return_for_records");
          const targetBeat = storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === "beat_evidence_secured");
          assert(rejoined.canonicalRelation === "rejoined", "折返取证未满足受控汇合条件");
          assert(rejoined.branchState.evidenceStatus === "secured", "汇合节点未保全证据");
          assert(rejoined.narrativeText !== targetBeat?.sourceExcerpt?.text, "汇合节点复用了未展示的原著节选");
          assert(liveService.llmAudits(sessionId).at(-1)?.error === undefined, "汇合节点正文未通过 LLM 校验");
          return ["受控汇合状态成立", "汇合正文未拼接原著节选"];
        } finally {
          store.close();
        }
      },
    },
    {
      id: "free_text_request_idempotency",
      async run() {
        const { store, service, sessionId } = createService(storyPackage, new MockBranchPlanner(), new LlmDirectionEvaluator(gateway, model), "live-idempotency");
        try {
          const { root } = service.start({ sessionId });
          const requestId = "live-idempotency-1";
          const input = "追查十七号柜留下的铜牌线索";
          const first = await service.continueWithPlayerDirection(sessionId, root.id, input, requestId);
          const repeated = await service.continueWithPlayerDirection(sessionId, root.id, input, requestId);
          assert(first.kind === "accepted" && repeated.kind === "accepted", "幂等请求没有得到接受结果");
          if (first.kind !== "accepted" || repeated.kind !== "accepted") throw new Error("幂等请求没有生成可比较节点");
          assert(first.node.id === repeated.node.id, "重复 requestId 生成了不同分支节点");
          assert(service.directionEvaluatorAudits(sessionId).length === 1, "重复 requestId 重复调用了方向评估器");
          assert(service.history(sessionId).length === 2, "重复 requestId 重复追加了分支");
          return ["重复自由文本请求复用同一节点", "方向评估器只调用一次"];
        } finally {
          store.close();
        }
      },
    },
    {
      id: "forbidden_supernatural_action",
      async run() {
        const { store, service, sessionId } = createService(storyPackage, new MockBranchPlanner(), new LlmDirectionEvaluator(gateway, model), "live-supernatural");
        try {
          const { root } = service.start({ sessionId });
          const result = await service.continueWithPlayerDirection(sessionId, root.id, "许川施展魔法瞬移到信号室救出唐栖", "live-supernatural-1");
          assert(result.kind === "rejected", "超自然行动没有被拒绝");
          if (result.kind !== "rejected") throw new Error("超自然行动没有返回拒绝理由");
          assert(result.citations.some((citation) => citation.ref === "fact_no_supernatural"), "拒绝结果未引用不可变世界事实");
          assert(service.history(sessionId).length === 1, "被拒绝的行动仍追加了分支");
          assert(service.directionEvaluatorAudits(sessionId).at(-1)?.error === undefined, "超自然行动评估审计包含错误");
          return ["超自然行动被拒绝", "拒绝引用了不可变事实"];
        } finally {
          store.close();
        }
      },
    },
  ];
}

function selectScenarios<T extends { id: string }>(scenarios: T[], arguments_: string[]): T[] {
  const scenarioIndex = arguments_.indexOf("--scenario");
  if (scenarioIndex === -1) return scenarios;
  const scenarioId = arguments_[scenarioIndex + 1];
  if (!scenarioId) throw new Error("--scenario 需要提供场景 ID");
  const scenario = scenarios.find((candidate) => candidate.id === scenarioId);
  if (!scenario) throw new Error(`未知真实模型评估场景: ${scenarioId}`);
  return [scenario];
}

function createService(
  storyPackage: Awaited<ReturnType<typeof loadStoryPackage>>,
  planner: LlmBranchPlanner | MockBranchPlanner,
  evaluator: LlmDirectionEvaluator,
  sessionId: string,
) {
  const store = new SqliteSessionStore(":memory:");
  store.createSession(storyPackage, sessionId);
  return { store, service: new CoCreationService(storyPackage, store, planner, evaluator), sessionId };
}

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`真实模型评估缺少环境变量: ${name}`);
  return value;
}

function parseMaxCalls(value: string | undefined): number {
  if (!value) return 8;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 20) {
    throw new Error("STORY_LIVE_EVALUATION_MAX_CALLS 必须是 1 至 20 的整数");
  }
  return parsed;
}

function assert(condition: boolean, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function writeReportIfRequested(report: object): Promise<void> {
  const outputIndex = process.argv.indexOf("--output");
  if (outputIndex === -1) return;
  const outputPath = process.argv[outputIndex + 1];
  if (!outputPath) throw new Error("--output 需要提供结果文件路径");
  const resolvedPath = resolve(outputPath);
  await mkdir(dirname(resolvedPath), { recursive: true });
  await writeFile(resolvedPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
