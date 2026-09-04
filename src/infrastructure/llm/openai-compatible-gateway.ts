import {
  LlmGatewayError,
  type LlmGateway,
  type LlmJsonRequest,
  type LlmJsonResponse,
  type LlmResponseMode,
  type LlmTransportAttempt,
  type LlmTransportObservation,
} from "../../domain/llm/llm-gateway.js";

export type OpenAiCompatibleGatewayConfig = {
  apiKey: string;
  model: string;
  baseUrl?: string;
  timeoutMs?: number;
  stream?: boolean;
  fetchImplementation?: typeof fetch;
};

export class OpenAiCompatibleGateway implements LlmGateway {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly stream: boolean;
  private readonly fetchImplementation: typeof fetch;

  constructor(private readonly config: OpenAiCompatibleGatewayConfig) {
    this.baseUrl = (config.baseUrl ?? "https://api.openai.com/v1").replace(/\/+$/, "");
    this.timeoutMs = config.timeoutMs ?? 30_000;
    this.stream = config.stream ?? true;
    this.fetchImplementation = config.fetchImplementation ?? fetch;
  }

  async completeJson(request: LlmJsonRequest): Promise<LlmJsonResponse> {
    try {
      return await this.completeJsonWithMode(request, this.stream);
    } catch (error) {
      if (!(error instanceof LlmGatewayError)) throw error;
      const fallbackReason = fallbackReasonFor(error);
      if (!fallbackReason) throw error;
      const initialAttempt = toFailedAttempt(error);
      try {
        const fallback = await this.completeJsonWithMode(request, false);
        if (!fallback.transport) throw new Error("JSON 回退缺少传输观测");
        return {
          ...fallback,
          transport: {
            ...fallback.transport,
            fallback: { reason: fallbackReason, initialAttempt },
          },
        };
      } catch (fallbackError) {
        if (fallbackError instanceof LlmGatewayError) {
          throw new LlmGatewayError(
            fallbackError.message,
            fallbackError.failureKind,
            { ...fallbackError.transport, fallback: { reason: fallbackReason, initialAttempt } },
          );
        }
        throw fallbackError;
      }
    }
  }

  private async completeJsonWithMode(request: LlmJsonRequest, stream: boolean): Promise<LlmJsonResponse> {
    const startedAt = performance.now();
    const signal = AbortSignal.timeout(this.timeoutMs);
    let responseMode: LlmResponseMode = stream ? "sse" : "json";
    let httpStatus: number | undefined;

    try {
      const response = await this.fetchImplementation(`${this.baseUrl}/chat/completions`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.config.apiKey}`,
          "Content-Type": "application/json",
          Accept: stream ? "text/event-stream, application/json" : "application/json",
        },
        body: JSON.stringify({
          model: this.config.model,
          messages: [
            { role: "system", content: request.systemPrompt },
            { role: "user", content: request.userPrompt },
          ],
          response_format: { type: "json_object" },
          max_tokens: request.maxOutputTokens,
          temperature: 0.7,
          stream,
        }),
        signal,
      });
      httpStatus = response.status;
      responseMode = response.headers.get("content-type")?.includes("text/event-stream") ? "sse" : "json";
      if (!response.ok) {
        throw new LlmGatewayError(
          `LLM 请求失败 (${response.status})`,
          "http_error",
          createTransportObservation(startedAt, responseMode, httpStatus),
        );
      }

      const parsed = responseMode === "sse"
        ? await readSseCompletion(response, this.config.model)
        : parseCompletion(await response.text(), this.config.model);
      return { ...parsed, transport: createTransportObservation(startedAt, responseMode, httpStatus) };
    } catch (error) {
      if (error instanceof LlmGatewayError) throw error;
      const transport = createTransportObservation(startedAt, responseMode, httpStatus);
      if (signal.aborted || (error instanceof Error && error.name === "TimeoutError")) {
        throw new LlmGatewayError(`LLM 请求在 ${this.timeoutMs}ms 后超时`, "timeout", transport);
      }
      if (isResponseFormatError(error)) {
        throw new LlmGatewayError(error.message, "invalid_response", transport);
      }
      throw new LlmGatewayError("LLM 请求传输失败", "transport_error", transport);
    }
  }
}

function createTransportObservation(startedAt: number, responseMode: LlmResponseMode, httpStatus?: number): LlmTransportObservation {
  return { durationMs: Math.round(performance.now() - startedAt), responseMode, httpStatus };
}

function isResponseFormatError(error: unknown): error is Error {
  return error instanceof Error && (
    error.message.startsWith("LLM 响应") || error.message.startsWith("LLM 流式响应")
  );
}

function fallbackReasonFor(error: LlmGatewayError): "sse_missing_content" | "sse_timeout" | undefined {
  if (error.transport.responseMode !== "sse") return undefined;
  if (
    error.failureKind === "invalid_response"
    && error.message === "LLM 流式响应缺少 choices[0].delta.content 或 choices[0].message.content"
  ) {
    return "sse_missing_content";
  }
  if (error.failureKind === "timeout" && error.transport.httpStatus !== undefined) return "sse_timeout";
  return undefined;
}

function toFailedAttempt(error: LlmGatewayError): LlmTransportAttempt {
  return {
    durationMs: error.transport.durationMs,
    responseMode: error.transport.responseMode,
    httpStatus: error.transport.httpStatus,
    failureKind: error.failureKind,
  };
}

async function readSseCompletion(response: Response, fallbackModel: string): Promise<LlmJsonResponse> {
  if (!response.body) throw new Error("LLM 流式响应没有正文");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let content = "";
  let model = fallbackModel;
  let finishReason: string | undefined;

  const consumeEvents = (flush: boolean): void => {
    const events = buffer.split(/\r?\n\r?\n/);
    buffer = flush ? "" : (events.pop() ?? "");
    for (const event of events) {
      const data = event.split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (!data || data === "[DONE]") continue;
      let payload: unknown;
      try {
        payload = JSON.parse(data) as unknown;
      } catch {
        throw new Error("LLM 流式响应包含无法解析的事件");
      }
      model = extractModel(payload) ?? model;
      content += extractDeltaContent(payload) ?? extractContent(payload) ?? "";
      finishReason = extractFinishReason(payload) ?? finishReason;
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (value) {
      buffer += decoder.decode(value, { stream: !done });
      consumeEvents(done);
    }
    if (done) break;
  }

  if (buffer.trim()) consumeEvents(true);
  if (!content) throw new Error("LLM 流式响应缺少 choices[0].delta.content 或 choices[0].message.content");
  return { model, content, finishReason };
}

function parseCompletion(body: string, fallbackModel: string): LlmJsonResponse {
  let payload: unknown;
  try {
    payload = JSON.parse(body) as unknown;
  } catch {
    throw new Error("LLM 响应不是 JSON");
  }
  // Some OpenAI-compatible proxies keep the streaming delta envelope even when stream=false.
  const content = extractContent(payload) ?? extractDeltaContent(payload);
  if (!content) throw new Error("LLM 响应缺少 choices[0].message.content 或 choices[0].delta.content");
  return { model: extractModel(payload) ?? fallbackModel, content, finishReason: extractFinishReason(payload) };
}

function extractContent(payload: unknown): string | undefined {
  if (!isRecord(payload) || !Array.isArray(payload.choices)) return undefined;
  const choice = payload.choices[0];
  if (!isRecord(choice) || !isRecord(choice.message)) return undefined;
  return typeof choice.message.content === "string" ? choice.message.content : undefined;
}

function extractModel(payload: unknown): string | undefined {
  return isRecord(payload) && typeof payload.model === "string" ? payload.model : undefined;
}

function extractDeltaContent(payload: unknown): string | undefined {
  if (!isRecord(payload) || !Array.isArray(payload.choices)) return undefined;
  const choice = payload.choices[0];
  if (!isRecord(choice) || !isRecord(choice.delta)) return undefined;
  return typeof choice.delta.content === "string" ? choice.delta.content : undefined;
}

function extractFinishReason(payload: unknown): string | undefined {
  if (!isRecord(payload) || !Array.isArray(payload.choices)) return undefined;
  const choice = payload.choices[0];
  return isRecord(choice) && typeof choice.finish_reason === "string" ? choice.finish_reason : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
