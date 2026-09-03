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

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toEqual({ model: "test-model", content: "{\"ok\":true}", finishReason: undefined });
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe("https://example.test/v1/chat/completions");
    expect(calls[0]?.init?.headers).toMatchObject({ Authorization: "Bearer test-key" });
    expect(JSON.parse(String(calls[0]?.init?.body))).toMatchObject({ model: "test-model", response_format: { type: "json_object" }, max_tokens: 42, stream: true });
  });

  it("reassembles an OpenAI-compatible SSE response", async () => {
    const eventBody = [
      "data: {\"model\":\"test-model\",\"choices\":[{\"delta\":{\"content\":\"{\\\"ok\\\":\"},\"finish_reason\":null}]}\n\n",
      "data: {\"model\":\"test-model\",\"choices\":[{\"delta\":{\"content\":\"true}\"},\"finish_reason\":\"stop\"}]}\n\n",
      "data: [DONE]\n\n",
    ];
    const fakeFetch = async (): Promise<Response> => new Response(eventBody.join(""), { headers: { "content-type": "text/event-stream" } });
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).resolves.toEqual({ model: "test-model", content: "{\"ok\":true}", finishReason: "stop" });
  });

  it("reports a non-success response without accepting it as model output", async () => {
    const fakeFetch = async (): Promise<Response> => new Response("denied", { status: 401 });
    const gateway = new OpenAiCompatibleGateway({ apiKey: "test-key", model: "test-model", fetchImplementation: fakeFetch as typeof fetch });

    await expect(gateway.completeJson({ systemPrompt: "system", userPrompt: "user", maxOutputTokens: 42 })).rejects.toThrow("LLM 请求失败 (401)");
  });
});
