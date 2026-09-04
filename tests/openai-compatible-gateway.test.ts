import { describe, expect, it } from "vitest";
import { OpenAiCompatibleGateway } from "../src/infrastructure/llm/openai-compatible-gateway.js";

describe("OpenAiCompatibleGateway", () => {
  it("requests a bounded JSON-only streaming chat completion and accepts a non-streaming fallback", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fakeFetch = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify({ model: "test-model", choices: [{ message: { content: "{\"ok\":true}" } }] }), { status: 200 });
    };
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", baseUrl: "https://example.test/v1/", fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toMatchObject({ model: "test-model", content: "{\"ok\":true}", finishReason: undefined, transport: { responseMode: "json", httpStatus: 200 } });
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe("https://example.test/v1/chat/completions");
    expect(calls[0]?.init?.headers).toMatchObject({ Authorization: "Bearer test-key" });
    expect(JSON.parse(String(calls[0]?.init?.body))).toMatchObject({ model: "test-model", response_format: { type: "json_object" }, max_tokens: 42, stream: true });
  });

  it("can request a bounded non-streaming completion", async () => {
    const calls: Array<{ init?: RequestInit }> = [];
    const fakeFetch = async (_input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      calls.push({ init });
      return new Response(JSON.stringify({ model: "test-model", choices: [{ message: { content: "{\"ok\":true}" } }] }), { status: 200 });
    };
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", stream: false, fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toMatchObject({ model: "test-model", content: "{\"ok\":true}", finishReason: undefined, transport: { responseMode: "json", httpStatus: 200 } });
    expect(calls[0]?.init?.headers).toMatchObject({ Accept: "application/json" });
    expect(JSON.parse(String(calls[0]?.init?.body))).toMatchObject({ stream: false });
  });

  it("accepts a delta content envelope returned from a non-streaming request", async () => {
    const fakeFetch = async (): Promise<Response> => new Response(
      JSON.stringify({ model: "test-model", choices: [{ delta: { content: "{\"ok\":true}" }, finish_reason: "stop" }] }),
      { headers: { "content-type": "application/json" } },
    );
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", stream: false, fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toMatchObject({
      model: "test-model",
      content: "{\"ok\":true}",
      finishReason: "stop",
      transport: { responseMode: "json", httpStatus: 200 },
    });
  });

  it("reassembles an OpenAI-compatible SSE response", async () => {
    const eventBody = [
      "data: {\"model\":\"test-model\",\"choices\":[{\"delta\":{\"content\":\"{\\\"ok\\\":\"},\"finish_reason\":null}]}\n\n",
      "data: {\"model\":\"test-model\",\"choices\":[{\"delta\":{\"content\":\"true}\"},\"finish_reason\":\"stop\"}]}\n\n",
      "data: [DONE]\n\n",
    ];
    const fakeFetch = async (): Promise<Response> => new Response(eventBody.join(""), { headers: { "content-type": "text/event-stream" } });
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toMatchObject({ model: "test-model", content: "{\"ok\":true}", finishReason: "stop", transport: { responseMode: "sse", httpStatus: 200 } });
  });

  it("ignores role, reasoning, and usage SSE events before reassembling content", async () => {
    const eventBody = [
      "data: {\"model\":\"test-model\",\"choices\":[{\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n",
      "data: {\"model\":\"test-model\",\"choices\":[{\"delta\":{\"reasoning_content\":\"hidden\"},\"finish_reason\":null}]}\n\n",
      "data: {\"model\":\"test-model\",\"choices\":[{\"delta\":{\"content\":\"{\\\"ok\\\":\"},\"finish_reason\":null}]}\n\n",
      "data: {\"model\":\"test-model\",\"choices\":[{\"delta\":{\"content\":\"true}\"},\"finish_reason\":\"stop\"}]}\n\n",
      "data: {\"usage\":{\"total_tokens\":1}}\n\n",
      "data: [DONE]\n\n",
    ];
    const fakeFetch = async (): Promise<Response> => new Response(eventBody.join(""), { headers: { "content-type": "text/event-stream" } });
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toMatchObject({ content: "{\"ok\":true}", finishReason: "stop", transport: { responseMode: "sse", httpStatus: 200 } });
  });

  it("retries once with JSON when an SSE response ends without content", async () => {
    const calls: RequestInit[] = [];
    const fakeFetch = async (_input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      calls.push(init ?? {});
      if (calls.length === 1) {
        return new Response("data: {\"model\":\"test-model\",\"choices\":[{\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\ndata: [DONE]\n\n", { headers: { "content-type": "text/event-stream" } });
      }
      return new Response(JSON.stringify({ model: "test-model", choices: [{ message: { content: "{\"ok\":true}" }, finish_reason: "stop" }] }), { headers: { "content-type": "application/json" } });
    };
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toMatchObject({
      content: "{\"ok\":true}",
      transport: {
        responseMode: "json",
        httpStatus: 200,
        fallback: {
          reason: "sse_missing_content",
          initialAttempt: { responseMode: "sse", httpStatus: 200, failureKind: "invalid_response" },
        },
      },
    });
    expect(calls).toHaveLength(2);
    expect(JSON.parse(String(calls[0]?.body))).toMatchObject({ stream: true });
    expect(JSON.parse(String(calls[1]?.body))).toMatchObject({ stream: false });
  });

  it("retries once with JSON when an established SSE response times out", async () => {
    const calls: RequestInit[] = [];
    const fakeFetch = async (_input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      calls.push(init ?? {});
      if (calls.length === 1) {
        const signal = init?.signal as AbortSignal;
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            signal.addEventListener("abort", () => controller.error(new DOMException("Timed out", "TimeoutError")));
          },
        });
        return new Response(body, { headers: { "content-type": "text/event-stream" } });
      }
      return new Response(JSON.stringify({ model: "test-model", choices: [{ message: { content: "{\"ok\":true}" }, finish_reason: "stop" }] }), { headers: { "content-type": "application/json" } });
    };
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", timeoutMs: 25, fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toMatchObject({
      content: "{\"ok\":true}",
      transport: {
        responseMode: "json",
        httpStatus: 200,
        fallback: {
          reason: "sse_timeout",
          initialAttempt: { responseMode: "sse", httpStatus: 200, failureKind: "timeout" },
        },
      },
    });
    expect(calls).toHaveLength(2);
    expect(JSON.parse(String(calls[0]?.body))).toMatchObject({ stream: true });
    expect(JSON.parse(String(calls[1]?.body))).toMatchObject({ stream: false });
  });

  it("does not retry malformed SSE events with JSON", async () => {
    let calls = 0;
    const fakeFetch = async (): Promise<Response> => {
      calls += 1;
      return new Response("data: not-json\n\n", { headers: { "content-type": "text/event-stream" } });
    };
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).rejects.toMatchObject({
      failureKind: "invalid_response",
      transport: { responseMode: "sse", httpStatus: 200 },
    });
    expect(calls).toBe(1);
  });

  it("accepts a complete message payload sent in an SSE event", async () => {
    const eventBody = "data: {\"model\":\"test-model\",\"choices\":[{\"message\":{\"content\":\"{\\\"ok\\\":true}\"},\"finish_reason\":\"stop\"}]}\n\n";
    const fakeFetch = async (): Promise<Response> => new Response(eventBody, { headers: { "content-type": "text/event-stream" } });
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toMatchObject({ model: "test-model", content: "{\"ok\":true}", finishReason: "stop", transport: { responseMode: "sse", httpStatus: 200 } });
  });

  it("reports a non-success response without accepting it as model output", async () => {
    const fakeFetch = async (): Promise<Response> => new Response("denied", { status: 401 });
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).rejects.toMatchObject({
      message: "LLM 请求失败 (401)",
      failureKind: "http_error",
      transport: { responseMode: "json", httpStatus: 401 },
    });
  });

  it("classifies a timeout without retaining an endpoint response body", async () => {
    let calls = 0;
    const fakeFetch = async (): Promise<Response> => {
      calls += 1;
      throw new DOMException("Timed out", "TimeoutError");
    };
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", timeoutMs: 25, fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).rejects.toMatchObject({
      name: "LlmGatewayError",
      failureKind: "timeout",
      transport: { responseMode: "sse" },
    });
    expect(calls).toBe(1);
  });
});
