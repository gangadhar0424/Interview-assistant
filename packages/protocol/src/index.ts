// GENERATED FILE - DO NOT EDIT.
// Source: services/api/app/realtime/schemas.py (docs/API.md section 8).
// Regenerate: cd services/api && uv run python -m app.realtime.codegen

export const PROTOCOL_VERSION = 1;

export const CLOSE_CODES = {
  normal: 1000,
  unsupported_data: 1003,
  policy: 1008,
  too_big: 1009,
  superseded: 4001,
  idle_timeout: 4002,
  protocol_error: 4003,
  unsupported_version: 4004,
  rate_limited: 4005,
  slow_consumer: 4010,
  too_many_connections: 4429,
} as const;

export interface AnswerCancel {
  id?: string | null;
  type: "answer.cancel";
  payload: AnswerCancelPayload;
}

export interface AnswerCancelPayload {
  question_id: string;
}

export interface ClientHello {
  id?: string | null;
  type: "client.hello";
  payload: ClientHelloPayload;
}

export interface ClientHelloPayload {
  protocol_version: number;
  client_version?: string | null;
  platform?: string | null;
  capabilities?: string[];
  resume_from_seq?: number | null;
}

export type EmptyPayload = Record<string, never>;

export interface Ping {
  id?: string | null;
  type: "ping";
  payload: EmptyPayload;
}

export interface QuestionSubmit {
  id?: string | null;
  type: "question.submit";
  payload: QuestionSubmitPayload;
}

export interface QuestionSubmitPayload {
  text: string;
  question_type_override?: string | null;
}

export type ClientMessage = ClientHello | Ping | QuestionSubmit | AnswerCancel;

export type ClientMessageType = ClientHello['type'] | Ping['type'] | QuestionSubmit['type'] | AnswerCancel['type'];

export interface AnswerCancelled {
  id: string;
  seq: number;
  ts: number;
  type: "answer.cancelled";
  payload: AnswerCancelledPayload;
}

export interface AnswerCancelledPayload {
  answer_id: string;
  question_id: string;
}

export interface AnswerDelta {
  id: string;
  seq: number;
  ts: number;
  type: "answer.delta";
  payload: AnswerDeltaPayload;
}

export interface AnswerDeltaPayload {
  answer_id: string;
  index: number;
  text: string;
}

export interface AnswerDone {
  id: string;
  seq: number;
  ts: number;
  type: "answer.done";
  payload: AnswerDonePayload;
}

export interface AnswerDonePayload {
  answer_id: string;
  question_id: string;
  provider: string;
  model: string;
  metrics: AnswerMetrics;
  fallback_used: boolean;
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface AnswerError {
  id: string;
  seq: number;
  ts: number;
  type: "answer.error";
  payload: AnswerErrorPayload;
}

export interface AnswerErrorPayload {
  answer_id: string;
  question_id: string;
  code: string;
  message: string;
  retryable: boolean;
}

export interface AnswerMetrics {
  retrieval_ms: number;
  first_token_ms: number;
  total_ms: number;
}

export interface AnswerStarted {
  id: string;
  seq: number;
  ts: number;
  type: "answer.started";
  payload: AnswerStartedPayload;
}

export interface AnswerStartedPayload {
  answer_id: string;
  question_id: string;
  provider: string;
  model: string;
}

export interface ErrorEvent {
  id: string;
  seq: number;
  ts: number;
  type: "error";
  payload: ErrorPayload;
}

export interface ErrorPayload {
  code: string;
  message: string;
  fatal: boolean;
}

export interface Pong {
  id: string;
  seq: number;
  ts: number;
  type: "pong";
  payload: EmptyPayload;
}

export interface QuestionCreated {
  id: string;
  seq: number;
  ts: number;
  type: "question.created";
  payload: QuestionCreatedPayload;
}

export interface QuestionCreatedPayload {
  question_id: string;
  answer_id: string;
}

export interface RetrievalDone {
  id: string;
  seq: number;
  ts: number;
  type: "retrieval.done";
  payload: RetrievalDonePayload;
}

export interface RetrievalDonePayload {
  question_id: string;
  chunk_count: number;
  ms: number;
}

export interface SessionLimits {
  max_frame_bytes: number;
  heartbeat_interval_seconds: number;
  idle_timeout_seconds: number;
  max_messages_per_second: number;
  resume_grace_seconds: number;
  resume_buffer_events: number;
}

export interface SessionReady {
  id: string;
  seq: number;
  ts: number;
  type: "session.ready";
  payload: SessionReadyPayload;
}

export interface SessionReadyPayload {
  session_id: string;
  server_time: number;
  resume_seq: number;
  protocol_version: number;
  limits: SessionLimits;
}

export type ServerMessage = SessionReady | Pong | ErrorEvent | QuestionCreated | RetrievalDone | AnswerStarted | AnswerDelta | AnswerDone | AnswerError | AnswerCancelled;

export type ServerMessageType = SessionReady['type'] | Pong['type'] | ErrorEvent['type'] | QuestionCreated['type'] | RetrievalDone['type'] | AnswerStarted['type'] | AnswerDelta['type'] | AnswerDone['type'] | AnswerError['type'] | AnswerCancelled['type'];
