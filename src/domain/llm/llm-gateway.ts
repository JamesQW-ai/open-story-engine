export type LlmJsonRequest = {
  systemPrompt: string;
  userPrompt: string;
  maxOutputTokens: number;
};

export type LlmJsonResponse = {
  model: string;
  content: string;
  finishReason?: string;
};

export interface LlmGateway {
  completeJson(request: LlmJsonRequest): Promise<LlmJsonResponse>;
}
