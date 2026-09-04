export type LlmJsonRequest = {
  systemPrompt: string;
  userPrompt: string;
  maxOutputTokens: number;
  operation?: "branch_planner" | "direction_evaluator";
  attempt?: number;
  retryReason?: "model_output_rejected" | LlmGatewayFailureKind;
};

export type LlmResponseMode = "sse" | "json" | "unknown";

export type LlmGatewayFailureKind = "timeout" | "http_error" | "invalid_response" | "transport_error";

export type LlmTransportObservation = {
  durationMs: number;
  responseMode: LlmResponseMode;
  httpStatus?: number;
  fallback?: {
    reason: "sse_missing_content" | "sse_timeout";
    initialAttempt: LlmTransportAttempt;
  };
};

export type LlmTransportAttempt = {
  durationMs: number;
  responseMode: LlmResponseMode;
  httpStatus?: number;
  failureKind?: LlmGatewayFailureKind;
};

export type LlmCallObservation = {
  attempt: number;
  retryReason?: LlmJsonRequest["retryReason"];
  outcome: "completed" | "failed";
  failureKind?: LlmGatewayFailureKind | "model_output_rejected";
  transport?: LlmTransportObservation;
};

export type LlmJsonResponse = {
  model: string;
  content: string;
  finishReason?: string;
  transport?: LlmTransportObservation;
};

export class LlmGatewayError extends Error {
  constructor(
    message: string,
    readonly failureKind: LlmGatewayFailureKind,
    readonly transport: LlmTransportObservation,
  ) {
    super(message);
    this.name = "LlmGatewayError";
  }
}

export interface LlmGateway {
  completeJson(request: LlmJsonRequest): Promise<LlmJsonResponse>;
}
