// Typed question answering client logic (Phase 8C). Everything here is pure or works on Web
// standard streams, so it runs under Node's test runner (tests/qa.test.ts) as well as the app.
// The contract is docs/API.md + D-39 (Phase 8B); this file mirrors it and never invents events.
//
// Imported by Node's test runner: keep it plain, erasable TypeScript (no enums, no path aliases,
// relative imports with ".ts").

import { ApiError } from "./api.ts";

// ------------------------------------------------------------------ contract types

export const QUESTION_MAX_CHARS = 2000; // UX hint mirroring the server limit (the server decides)

export type AnswerStatus = "streaming" | "complete" | "cancelled" | "error";

// GET /api/v1/sessions/{id}/questions and /answers/{answer_id}
export type AnswerItem = {
  id: string;
  question_id: string;
  status: string;
  text: string;
  provider: string | null;
  model: string | null;
  retrieval_ms: number | null;
  first_token_ms: number | null;
  total_ms: number | null;
  fallback_used: boolean;
  error_code: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  created_at: string;
};

export type QuestionItem = {
  id: string;
  text: string;
  source: string;
  kind: string;
  type: string | null;
  confidence: number | null;
  status: string;
  created_at: string;
  answers: AnswerItem[];
};

export type QuestionPage = { items: QuestionItem[]; next_cursor: string | null };

export type Metrics = {
  retrieval_ms: number | null;
  first_token_ms: number | null;
  total_ms: number | null;
};

// The six SSE events of POST /sessions/{id}/questions, with their payloads.
export type QaEvent =
  | { type: "question.created"; questionId: string; answerId: string }
  | { type: "retrieval.done"; questionId: string; chunkCount: number; ms: number }
  | { type: "answer.started"; answerId: string; questionId: string; provider: string; model: string }
  | { type: "answer.delta"; answerId: string; index: number; text: string }
  | {
      type: "answer.done";
      answerId: string;
      questionId: string;
      provider: string;
      model: string;
      metrics: Metrics;
      fallbackUsed: boolean;
      inputTokens: number | null;
      outputTokens: number | null;
    }
  | {
      type: "answer.error";
      answerId: string | null;
      questionId: string | null;
      code: string;
      retryable: boolean;
    };

// ------------------------------------------------------------------ SSE framing

/** The server sent something that is not the documented protocol. */
export class ProtocolError extends Error {}

export type SseFrame = { event: string; data: string };

const MAX_BUFFER_CHARS = 1_000_000; // a frame is one answer delta: this is far above any real one

// Incremental parser for `event: <name>\ndata: <json>\n\n` frames. Chunk boundaries can fall
// anywhere (mid-line, mid-character is handled by the TextDecoder before this), CRLF is
// tolerated, comment lines (":") and unknown fields (id, retry) are ignored, and an incomplete
// trailing frame is discarded (per the SSE spec) rather than guessed at.
export class SseParser {
  private buffer = "";

  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    if (this.buffer.length > MAX_BUFFER_CHARS) {
      throw new ProtocolError("An event frame was too large.");
    }
    this.buffer = this.buffer.replaceAll("\r\n", "\n").replaceAll("\r", "\n");
    const frames: SseFrame[] = [];
    let boundary = this.buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const raw = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const frame = parseFrame(raw);
      if (frame) frames.push(frame);
      boundary = this.buffer.indexOf("\n\n");
    }
    return frames;
  }

  /** True when bytes were received that never formed a complete frame. */
  get hasPartialFrame(): boolean {
    return this.buffer.trim().length > 0;
  }
}

function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split("\n")) {
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length === 0 ? null : { event, data: data.join("\n") };
}

// ------------------------------------------------------------------ event validation

type Obj = Record<string, unknown>;

function str(o: Obj, key: string): string {
  const v = o[key];
  if (typeof v !== "string") throw new ProtocolError(`Malformed event: ${key}`);
  return v;
}

function int(o: Obj, key: string): number {
  const v = o[key];
  if (typeof v !== "number" || !Number.isInteger(v) || v < 0) {
    throw new ProtocolError(`Malformed event: ${key}`);
  }
  return v;
}

function intOrNull(o: Obj, key: string): number | null {
  const v = o[key];
  return v === null || v === undefined ? null : int(o, key);
}

function strOrNull(o: Obj, key: string): string | null {
  const v = o[key];
  return v === null || v === undefined ? null : str(o, key);
}

/**
 * Turn one frame into a typed event. An event name this client does not know is returned as null
 * (the API's event set is forward-compatible, so unknown names are skipped, never fatal). A KNOWN
 * event with a malformed payload throws ProtocolError.
 */
export function parseQaEvent(frame: SseFrame): QaEvent | null {
  switch (frame.event) {
    case "question.created":
    case "retrieval.done":
    case "answer.started":
    case "answer.delta":
    case "answer.done":
    case "answer.error":
      break;
    default:
      return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(frame.data);
  } catch {
    throw new ProtocolError("Malformed event: not JSON");
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new ProtocolError("Malformed event: not an object");
  }
  const o = parsed as Obj;
  switch (frame.event) {
    case "question.created":
      return { type: "question.created", questionId: str(o, "question_id"), answerId: str(o, "answer_id") };
    case "retrieval.done":
      return { type: "retrieval.done", questionId: str(o, "question_id"), chunkCount: int(o, "chunk_count"), ms: int(o, "ms") };
    case "answer.started":
      return {
        type: "answer.started",
        answerId: str(o, "answer_id"),
        questionId: str(o, "question_id"),
        provider: str(o, "provider"),
        model: str(o, "model"),
      };
    case "answer.delta":
      return { type: "answer.delta", answerId: str(o, "answer_id"), index: int(o, "index"), text: str(o, "text") };
    case "answer.done": {
      const m = o["metrics"];
      if (typeof m !== "object" || m === null) throw new ProtocolError("Malformed event: metrics");
      const metrics = m as Obj;
      if (typeof o["fallback_used"] !== "boolean") throw new ProtocolError("Malformed event: fallback_used");
      return {
        type: "answer.done",
        answerId: str(o, "answer_id"),
        questionId: str(o, "question_id"),
        provider: str(o, "provider"),
        model: str(o, "model"),
        metrics: {
          retrieval_ms: intOrNull(metrics, "retrieval_ms"),
          first_token_ms: intOrNull(metrics, "first_token_ms"),
          total_ms: intOrNull(metrics, "total_ms"),
        },
        fallbackUsed: o["fallback_used"],
        inputTokens: intOrNull(o, "input_tokens"),
        outputTokens: intOrNull(o, "output_tokens"),
      };
    }
    default: // answer.error
      // The server's `message` is deliberately NOT carried: the UI shows its own text per `code`.
      return {
        type: "answer.error",
        answerId: strOrNull(o, "answer_id"),
        questionId: strOrNull(o, "question_id"),
        code: str(o, "code"),
        retryable: o["retryable"] === true,
      };
  }
}

export function isTerminal(event: QaEvent): boolean {
  return event.type === "answer.done" || event.type === "answer.error";
}

// ------------------------------------------------------------------ stream reader

/**
 * Read an SSE Response to its end, calling `onEvent` for each recognised event, in order.
 * Returns true when a terminal event (`answer.done` / `answer.error`) was delivered, false when the
 * stream ended without one (the caller shows an "interrupted" error). Reading stops right after
 * the terminal event. Throws ProtocolError on a malformed known event; an abort rejects with the
 * fetch's AbortError (the caller's own doing).
 */
export async function consumeAnswerStream(
  response: Response,
  onEvent: (event: QaEvent) => void,
): Promise<boolean> {
  if (response.body === null) throw new ProtocolError("The response has no body.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = new SseParser();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      const text = done ? decoder.decode() : decoder.decode(value, { stream: true });
      for (const frame of parser.push(text)) {
        const event = parseQaEvent(frame);
        if (event === null) continue;
        onEvent(event);
        if (isTerminal(event)) return true;
      }
      if (done) return false;
    }
  } finally {
    // Free the connection; a failure here (already closed/aborted) is irrelevant.
    await reader.cancel().catch(() => undefined);
  }
}

// ------------------------------------------------------------------ live answer state

export type Phase =
  | "idle"
  | "sending" // request in flight, nothing received yet
  | "retrieving" // question saved, searching the selected materials
  | "generating" // materials found, waiting for the first token
  | "streaming" // tokens arriving
  | "done"
  | "failed"
  | "cancelled";

export type FailureInfo = { code: string; retryable: boolean };

export type LiveAnswer = {
  phase: Phase;
  questionText: string;
  questionId: string | null;
  answerId: string | null;
  text: string;
  deltaCount: number;
  chunkCount: number | null;
  provider: string | null;
  model: string | null;
  metrics: Metrics | null;
  fallbackUsed: boolean;
  failure: FailureInfo | null;
};

export const IDLE: LiveAnswer = {
  phase: "idle",
  questionText: "",
  questionId: null,
  answerId: null,
  text: "",
  deltaCount: 0,
  chunkCount: null,
  provider: null,
  model: null,
  metrics: null,
  fallbackUsed: false,
  failure: null,
};

export type LiveAction =
  | QaEvent
  | { type: "start"; questionText: string }
  | { type: "cancel" } // the user (or navigation) closed the stream
  | { type: "failure"; code: string; retryable: boolean } // pre-stream refusal, protocol error, cut stream
  | { type: "reset" };

const RANK: Record<Phase, number> = {
  idle: 0,
  sending: 1,
  retrieving: 2,
  generating: 3,
  streaming: 4,
  done: 5,
  failed: 5,
  cancelled: 5,
};

export function isActive(phase: Phase): boolean {
  return phase === "sending" || phase === "retrieving" || phase === "generating" || phase === "streaming";
}

function advance(state: LiveAnswer, phase: Phase): Phase {
  return RANK[phase] > RANK[state.phase] ? phase : state.phase; // never move backwards
}

/**
 * Pure reducer for one question/answer turn. Rules:
 *  - a terminal phase (done / failed / cancelled) absorbs everything except `start` and `reset`;
 *  - events for a different answer than the one announced by `question.created` are ignored;
 *  - the phase only moves forward; a repeated delta index is ignored (no duplicated text);
 *  - partial text is always kept, whatever the outcome.
 */
export function reduceLive(state: LiveAnswer, action: LiveAction): LiveAnswer {
  switch (action.type) {
    case "start":
      return { ...IDLE, phase: "sending", questionText: action.questionText };
    case "reset":
      return IDLE;
    case "cancel":
      return isActive(state.phase) ? { ...state, phase: "cancelled" } : state;
    case "failure":
      return isActive(state.phase)
        ? { ...state, phase: "failed", failure: { code: action.code, retryable: action.retryable } }
        : state;
  }
  if (!isActive(state.phase)) return state;
  if (action.type !== "question.created" && state.answerId !== null) {
    const id = "answerId" in action ? action.answerId : null;
    if (id !== null && id !== state.answerId) return state;
  }
  switch (action.type) {
    case "question.created":
      return {
        ...state,
        phase: advance(state, "retrieving"),
        questionId: action.questionId,
        answerId: action.answerId,
      };
    case "retrieval.done":
      return { ...state, phase: advance(state, "generating"), chunkCount: action.chunkCount };
    case "answer.started":
      return {
        ...state,
        phase: advance(state, "generating"),
        provider: action.provider,
        model: action.model,
      };
    case "answer.delta":
      if (action.index < state.deltaCount) return state; // a replayed delta
      return {
        ...state,
        phase: advance(state, "streaming"),
        text: state.text + action.text,
        deltaCount: state.deltaCount + 1,
      };
    case "answer.done":
      return {
        ...state,
        phase: "done",
        provider: action.provider,
        model: action.model,
        metrics: action.metrics,
        fallbackUsed: action.fallbackUsed,
      };
    case "answer.error":
      return { ...state, phase: "failed", failure: { code: action.code, retryable: action.retryable } };
  }
}

// ------------------------------------------------------------------ safe user-facing messages

// The UI never shows server free text from the stream or provider errors: every message below is
// chosen here from a stable error `code`, so nothing internal can be displayed.
const ANSWER_ERRORS: Record<string, string> = {
  llm_unavailable: "The AI service is unavailable right now.",
  llm_rate_limited: "The AI service is busy. Please wait a moment.",
  llm_timeout: "The AI service took too long to answer.",
  llm_content_filtered: "The AI service could not answer this question.",
  invalid_provider_selection: "This session's AI provider or model is not available.",
  retrieval_failed: "Your materials could not be searched.",
  empty_answer: "The AI service returned an empty answer.",
  internal_error: "Something went wrong while answering.",
  generation_interrupted: "The answer was interrupted before it finished.",
  stream_interrupted: "The connection ended before the answer was complete.",
  bad_response: "The server sent an unexpected response.",
};

const GENERIC_ANSWER_ERROR = "Something went wrong while answering.";

export function answerErrorMessage(code: string | null): string {
  if (code === null) return GENERIC_ANSWER_ERROR;
  return Object.hasOwn(ANSWER_ERRORS, code) ? (ANSWER_ERRORS[code] as string) : GENERIC_ANSWER_ERROR;
}

export type AskErrorInput = {
  status: number;
  code: string;
  retryAfterSeconds: number | null;
  firstDetail?: string | null;
};

/**
 * Message for a refusal that happened BEFORE the stream started (ordinary JSON errors from
 * POST /sessions/{id}/questions: 401, 404, 406, 409, 422, 429, or a network failure). Unknown
 * codes get a generic message; server text is only used for 422, whose messages are the fixed
 * validation wording of the API.
 */
export function describeAskError(e: AskErrorInput): string {
  switch (e.code) {
    case "rate_limited": {
      const wait = e.retryAfterSeconds;
      return wait ? `You are asking too quickly. Try again in ${wait} seconds.` : "You are asking too quickly. Please wait a moment.";
    }
    case "too_many_generations":
      return "Another answer is still being generated. Wait for it to finish, then ask again.";
    case "session_not_ready":
      return "This session is not ready for questions.";
    case "materials_not_ready":
      return "A resume or document selected for this session was deleted or is no longer Ready. Check your files; every selected file must be Ready.";
    case "session_not_found":
      return "This session was not found.";
    case "not_acceptable":
      return "Your browser did not ask for a streaming answer. Please reload the page and try again.";
    case "network_error":
      return "Could not reach the server. Please try again.";
    case "bad_response":
      return "The server sent an unexpected response.";
    case "session_preferences_invalid":
      return "This session's preferences need to be updated before asking questions.";
  }
  if (e.status === 401) return "Your sign-in expired. Please sign in again.";
  if (e.status === 404) return "This session was not found.";
  if (e.status === 422) return e.firstDetail || "The question could not be accepted. Check it and try again.";
  return "Something went wrong. Please try again.";
}

// ------------------------------------------------------------------ small helpers

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string): boolean {
  return UUID.test(value);
}

export function questionError(text: string): string | null {
  const trimmed = text.trim();
  if (trimmed.length === 0) return "Type a question first.";
  if ([...trimmed].length > QUESTION_MAX_CHARS) return `Keep the question under ${QUESTION_MAX_CHARS} characters.`;
  return null;
}

/** Body of POST /sessions/{id}/questions: only the question (and the optional type override). */
export function toQuestionBody(text: string, override: string): { text: string; question_type_override?: string } {
  const body: { text: string; question_type_override?: string } = { text: text.trim() };
  if (override !== "") body.question_type_override = override;
  return body;
}

export function formatMs(ms: number | null): string {
  if (ms === null) return "n/a";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

// ------------------------------------------------------------------ one ask, end to end

export type AskDeps = {
  /** Opens the streaming request (the authenticated POST). Aborted through `signal`. */
  open: (signal: AbortSignal) => Promise<Response>;
  dispatch: (action: LiveAction) => void;
  signal: AbortSignal;
  /** Called once the server accepted the question (the stream is open). */
  onAccepted?: () => void;
};

export type AskOutcome =
  | { kind: "streamed" } // the turn is shown through the live state (done, failed or cancelled)
  | { kind: "refused"; message: string }; // refused before the stream started: no answer exists

/**
 * Run one question: open the stream, feed every event to `dispatch`, and turn every way it can
 * end into a state the UI can show:
 *  - answer.done / answer.error  -> done / failed (from the events themselves)
 *  - the user (or navigation) aborts -> cancelled, partial text kept
 *  - an ordinary JSON refusal (401/404/406/409/422/429/network) before any event -> "refused":
 *    the live turn is reset and the caller shows the message next to the form
 *  - a malformed event, or a stream that ends without a terminal event -> failed
 * It never throws.
 */
export async function runAsk(questionText: string, deps: AskDeps): Promise<AskOutcome> {
  const { open, dispatch, signal } = deps;
  dispatch({ type: "start", questionText });
  try {
    const response = await open(signal);
    deps.onAccepted?.();
    const terminal = await consumeAnswerStream(response, dispatch);
    if (!terminal) dispatch({ type: "failure", code: "stream_interrupted", retryable: true });
    return { kind: "streamed" };
  } catch (err) {
    if (signal.aborted) {
      dispatch({ type: "cancel" });
      return { kind: "streamed" };
    }
    if (err instanceof ApiError) {
      dispatch({ type: "reset" });
      return {
        kind: "refused",
        message: describeAskError({
          status: err.status,
          code: err.code,
          retryAfterSeconds: err.retryAfterSeconds,
          firstDetail: err.details[0]?.message ?? null,
        }),
      };
    }
    dispatch({
      type: "failure",
      code: err instanceof ProtocolError ? "bad_response" : "stream_interrupted",
      retryable: true,
    });
    return { kind: "streamed" };
  }
}
