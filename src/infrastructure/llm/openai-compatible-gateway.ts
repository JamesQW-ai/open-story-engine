import type { LlmGateway, LlmJsonRequest, LlmJsonResponse } from "../../domain/llm/llm-gateway.js";

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
    const response = await this.fetchImplementation(`${this.baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.config.apiKey}`,
        "Content-Type": "application/json",
        Accept: this.stream ? "text/event-stream, application/json" : "application/json",
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
        stream: this.stream,
      }),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!response.ok) {
      const body = await response.text();
      throw new Error(`LLM 请求失败 (${response.status}): ${body.slice(0, 500)}`);
    }

    if (response.headers.get("content-type")?.includes("text/event-stream")) {
      return readSseCompletion(response, this.config.model);
    }

    return parseCompletion(await response.text(), this.config.model);
  }
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
  const content = extractContent(payload);
  if (!content) throw new Error("LLM 响应缺少 choices[0].message.content");
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
