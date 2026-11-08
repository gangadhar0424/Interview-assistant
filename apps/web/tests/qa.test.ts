// Tests for the typed Q&A client (Phase 8C): SSE framing, event validation, the live-answer state
// machine, stream consumption (success, error, cut, malformed, abort), pre-stream error handling
// and the streaming request itself. Run: npm test (Node's built-in runner, no extra dependencies).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { ApiError, apiStream } from "../lib/api.ts";
import {
  IDLE,
  ProtocolError,
  SseParser,
  answerErrorMessage,
  consumeAnswerStream,
  describeAskError,
  isTerminal,
  isUuid,
  parseQaEvent,
  questionError,
  reduceLive,
  runAsk,
  toQuestionBody,
  type LiveAction,
  type LiveAnswer,
  type QaEvent,
} from "../lib/qa.ts";

const FIXTURES = path.resolve(import.meta.dirname, "fixtures");
const QID = "11111111-1111-4111-8111-111111111111";
const AID = "22222222-2222-4222-8222-222222222222";

const SUCCESS = readFileSync(path.join(FIXTURES, "sse_success.txt"), "utf-8");
const ERROR = readFileSync(path.join(FIXTURES, "sse_error.txt"), "utf-8");

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${typeof data === "string" ? data : JSON.stringify(data)}\n\n`;
}

const created = frame("question.created", { question_id: QID, answer_id: AID });
const retrieved = frame("retrieval.done", { question_id: QID, chunk_count: 2, ms: 5 });
const started = frame("answer.started", { answer_id: AID, question_id: QID, provider: "fake", model: "m" });
const delta = (i: number, text: string) => frame("answer.delta", { answer_id: AID, index: i, text });
const done = frame("answer.done", {
  answer_id: AID,
  question_id: QID,
  provider: "fake",
  model: "m",
  metrics: { retrieval_ms: 5, first_token_ms: 9, total_ms: 20 },
  fallback_used: false,
  input_tokens: 3,
  output_tokens: 4,
});
const failed = frame("answer.error", {
  answer_id: AID,
  question_id: QID,
  code: "llm_unavailable",
  message: "SERVER FREE TEXT",
  retryable: true,
});

/** A Response whose body delivers the given chunks (strings are UTF-8 encoded) one by one. */
function streamOf(chunks: (string | Uint8Array)[], init: ResponseInit = {}): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i >= chunks.length) return controller.close();
      const chunk = chunks[i++] as string | Uint8Array;
      controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
    },
  });
  return new Response(body, { headers: { "Content-Type": "text/event-stream" }, ...init });
}

async function collect(response: Response): Promise<{ events: QaEvent[]; terminal: boolean }> {
  const events: QaEvent[] = [];
  const terminal = await consumeAnswerStream(response, (e) => events.push(e));
  return { events, terminal };
}

function run(actions: LiveAction[], from: LiveAnswer = IDLE): LiveAnswer {
  return actions.reduce(reduceLive, from);
}

// ---------------------------------------------------------------------------- SSE framing

test("the parser yields the documented frames from the real backend transcript", () => {
  const frames = new SseParser().push(SUCCESS);
  assert.deepEqual(
    frames.map((f) => f.event),
    ["question.created", "retrieval.done", "answer.started", "answer.delta", "answer.delta", "answer.delta", "answer.done"],
  );
});

test("a chunk boundary at EVERY byte offset gives the same frames", () => {
  const expected = new SseParser().push(SUCCESS);
  for (let cut = 1; cut < SUCCESS.length; cut += 1) {
    const parser = new SseParser();
    const got = [...parser.push(SUCCESS.slice(0, cut)), ...parser.push(SUCCESS.slice(cut))];
    assert.deepEqual(got, expected, `cut at ${cut}`);
  }
});

test("the parser tolerates CRLF, comments, unknown fields and multi-line data", () => {
  const parser = new SseParser();
  const frames = parser.push(": keep-alive\r\n\r\nid: 7\r\nretry: 100\r\nevent: x\r\ndata: a\r\ndata: b\r\n\r\n");
  assert.deepEqual(frames, [{ event: "x", data: "a\nb" }]);
});

test("an incomplete trailing frame is discarded, not guessed at", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push(`${created}event: answer.delta\ndata: {"answ`), [
    { event: "question.created", data: JSON.stringify({ question_id: QID, answer_id: AID }) },
  ]);
  assert.equal(parser.hasPartialFrame, true);
});

test("an absurdly large frame is rejected instead of buffered forever", () => {
  assert.throws(() => new SseParser().push("data: " + "x".repeat(1_000_001)), ProtocolError);
});

test("answer text containing frame syntax cannot forge an event", () => {
  const events = new SseParser().push(SUCCESS).map(parseQaEvent);
  const deltas = events.filter((e) => e?.type === "answer.delta");
  assert.equal(deltas.length, 3);
  assert.equal(events.filter((e) => e?.type === "answer.done").length, 1); // only the real one
  const forged = deltas[2];
  assert.ok(forged && forged.type === "answer.delta" && forged.text.includes("event: answer.done"));
});

// ---------------------------------------------------------------------------- event validation

test("every documented event parses to a typed value", () => {
  const parse = (s: string) => parseQaEvent(new SseParser().push(s)[0]!);
  assert.deepEqual(parse(created), { type: "question.created", questionId: QID, answerId: AID });
  assert.deepEqual(parse(retrieved), { type: "retrieval.done", questionId: QID, chunkCount: 2, ms: 5 });
  assert.equal(parse(started)?.type, "answer.started");
  assert.deepEqual(parse(delta(3, "hi")), { type: "answer.delta", answerId: AID, index: 3, text: "hi" });
  const d = parse(done);
  assert.ok(d && d.type === "answer.done");
  assert.deepEqual(d.metrics, { retrieval_ms: 5, first_token_ms: 9, total_ms: 20 });
  assert.equal(d.fallbackUsed, false);
  assert.equal(d.inputTokens, 3);
});

test("the server's error message text is never carried into the client event", () => {
  const e = parseQaEvent(new SseParser().push(failed)[0]!);
  assert.ok(e && e.type === "answer.error");
  assert.deepEqual(e, { type: "answer.error", answerId: AID, questionId: QID, code: "llm_unavailable", retryable: true });
  assert.equal(JSON.stringify(e).includes("SERVER FREE TEXT"), false);
});

test("unknown event names are skipped (forward compatible), not fatal", () => {
  assert.equal(parseQaEvent({ event: "answer.thinking", data: "{}" }), null);
  assert.equal(parseQaEvent({ event: "message", data: "hello" }), null);
});

test("malformed known events are protocol errors", () => {
  const bad: [string, string][] = [
    ["question.created", "not json"],
    ["question.created", "[]"],
    ["question.created", "null"],
    ["question.created", JSON.stringify({ question_id: QID })],
    ["retrieval.done", JSON.stringify({ question_id: QID, chunk_count: -1, ms: 1 })],
    ["retrieval.done", JSON.stringify({ question_id: QID, chunk_count: "2", ms: 1 })],
    ["answer.delta", JSON.stringify({ answer_id: AID, index: 1.5, text: "x" })],
    ["answer.delta", JSON.stringify({ answer_id: AID, index: 0, text: 5 })],
    ["answer.done", JSON.stringify({ answer_id: AID, question_id: QID, provider: "p", model: "m", fallback_used: false })],
    ["answer.error", JSON.stringify({ answer_id: AID, question_id: QID, retryable: true })],
  ];
  for (const [event, data] of bad) {
    assert.throws(() => parseQaEvent({ event, data }), ProtocolError, `${event} ${data}`);
  }
});

// ---------------------------------------------------------------------------- stream consumption

test("a successful stream delivers every event in order and reports a terminal event", async () => {
  const { events, terminal } = await collect(streamOf([created, retrieved, started, delta(0, "A"), delta(1, "B"), done]));
  assert.equal(terminal, true);
  assert.deepEqual(
    events.map((e) => e.type),
    ["question.created", "retrieval.done", "answer.started", "answer.delta", "answer.delta", "answer.done"],
  );
});

test("the real backend success transcript is consumed end to end (multi-byte text split across chunks)", async () => {
  const bytes = new TextEncoder().encode(SUCCESS);
  const chunks: Uint8Array[] = [];
  for (let i = 0; i < bytes.length; i += 7) chunks.push(bytes.slice(i, i + 7)); // splits UTF-8 mid-character
  const { events, terminal } = await collect(streamOf(chunks));
  assert.equal(terminal, true);
  const text = events.flatMap((e) => (e.type === "answer.delta" ? [e.text] : [])).join("");
  assert.ok(text.includes("తెలుగు ✓"), "Telugu text survived chunking");
  const end = events.at(-1);
  assert.ok(end && end.type === "answer.done" && end.fallbackUsed === true && end.metrics.total_ms === 1450);
});

test("the real backend error transcript ends in a terminal answer.error with the partial delta first", async () => {
  const { events, terminal } = await collect(streamOf([ERROR]));
  assert.equal(terminal, true);
  assert.deepEqual(
    events.map((e) => e.type),
    ["question.created", "retrieval.done", "answer.started", "answer.delta", "answer.error"],
  );
  assert.equal(isTerminal(events.at(-1)!), true);
});

test("reading stops at the terminal event: later frames are never delivered", async () => {
  const { events } = await collect(streamOf([created, done, delta(0, "late"), failed]));
  assert.deepEqual(events.map((e) => e.type), ["question.created", "answer.done"]);
});

test("a stream that ends without a terminal event is reported as not terminal", async () => {
  const { events, terminal } = await collect(streamOf([created, retrieved, started, delta(0, "partial")]));
  assert.equal(terminal, false);
  assert.equal(events.length, 4);
});

test("a stream cut in the middle of a frame is reported as not terminal and the half frame is dropped", async () => {
  const { events, terminal } = await collect(streamOf([created, 'event: answer.delta\ndata: {"answer_id":"x"']));
  assert.equal(terminal, false);
  assert.deepEqual(events.map((e) => e.type), ["question.created"]);
});

test("unknown events inside a stream are skipped and the rest still works", async () => {
  const { events, terminal } = await collect(streamOf([created, frame("answer.future", { a: 1 }), done]));
  assert.equal(terminal, true);
  assert.deepEqual(events.map((e) => e.type), ["question.created", "answer.done"]);
});

test("a malformed known event rejects with ProtocolError after the earlier events were delivered", async () => {
  const events: QaEvent[] = [];
  await assert.rejects(
    consumeAnswerStream(streamOf([created, frame("answer.delta", "{broken")]), (e) => events.push(e)),
    ProtocolError,
  );
  assert.deepEqual(events.map((e) => e.type), ["question.created"]);
});

test("aborting mid-stream rejects with the AbortError and stops delivering events", async () => {
  const controller = new AbortController();
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(encoder.encode(created));
      controller.signal.addEventListener("abort", () => c.error(controller.signal.reason));
    },
  });
  const events: QaEvent[] = [];
  const pending = consumeAnswerStream(new Response(body), (e) => events.push(e));
  await new Promise((r) => setTimeout(r, 20));
  controller.abort(new DOMException("aborted", "AbortError"));
  await assert.rejects(pending, (err: Error) => err.name === "AbortError");
  assert.deepEqual(events.map((e) => e.type), ["question.created"]);
});

// ---------------------------------------------------------------------------- live state machine

const NORMAL: LiveAction[] = [
  { type: "start", questionText: "Tell me about yourself" },
  { type: "question.created", questionId: QID, answerId: AID },
  { type: "retrieval.done", questionId: QID, chunkCount: 2, ms: 5 },
  { type: "answer.started", answerId: AID, questionId: QID, provider: "fake", model: "m" },
];

test("phases advance sending -> retrieving -> generating -> streaming -> done", () => {
  const phases: string[] = [];
  let state: LiveAnswer = IDLE;
  const steps: LiveAction[] = [
    ...NORMAL,
    { type: "answer.delta", answerId: AID, index: 0, text: "Hel" },
    { type: "answer.delta", answerId: AID, index: 1, text: "lo" },
    {
      type: "answer.done",
      answerId: AID,
      questionId: QID,
      provider: "fake",
      model: "m",
      metrics: { retrieval_ms: 5, first_token_ms: 9, total_ms: 20 },
      fallbackUsed: true,
      inputTokens: null,
      outputTokens: null,
    },
  ];
  for (const step of steps) {
    state = reduceLive(state, step);
    phases.push(state.phase);
  }
  assert.deepEqual(phases, ["sending", "retrieving", "generating", "generating", "streaming", "streaming", "done"]);
  assert.equal(state.text, "Hello");
  assert.equal(state.questionText, "Tell me about yourself");
  assert.equal(state.chunkCount, 2);
  assert.equal(state.fallbackUsed, true);
  assert.deepEqual(state.metrics, { retrieval_ms: 5, first_token_ms: 9, total_ms: 20 });
});

test("answer.error keeps the partial text and records a failure", () => {
  const state = run([
    ...NORMAL,
    { type: "answer.delta", answerId: AID, index: 0, text: "Partial" },
    { type: "answer.error", answerId: AID, questionId: QID, code: "llm_timeout", retryable: true },
  ]);
  assert.equal(state.phase, "failed");
  assert.equal(state.text, "Partial");
  assert.deepEqual(state.failure, { code: "llm_timeout", retryable: true });
});

test("cancel keeps the partial text; a completed answer is never downgraded to cancelled or failed", () => {
  const streaming = run([...NORMAL, { type: "answer.delta", answerId: AID, index: 0, text: "Part" }]);
  const cancelled = reduceLive(streaming, { type: "cancel" });
  assert.equal(cancelled.phase, "cancelled");
  assert.equal(cancelled.text, "Part");

  const finished = reduceLive(streaming, {
    type: "answer.done",
    answerId: AID,
    questionId: QID,
    provider: "p",
    model: "m",
    metrics: { retrieval_ms: 1, first_token_ms: 1, total_ms: 1 },
    fallbackUsed: false,
    inputTokens: null,
    outputTokens: null,
  });
  assert.equal(reduceLive(finished, { type: "cancel" }).phase, "done");
  assert.equal(reduceLive(finished, { type: "failure", code: "stream_interrupted", retryable: true }).phase, "done");
});

test("terminal phases absorb late events", () => {
  const failedState = run([...NORMAL, { type: "answer.error", answerId: AID, questionId: QID, code: "internal_error", retryable: true }]);
  assert.equal(reduceLive(failedState, { type: "answer.delta", answerId: AID, index: 0, text: "late" }), failedState);
  const cancelled = run([...NORMAL, { type: "cancel" }]);
  assert.equal(reduceLive(cancelled, { type: "answer.delta", answerId: AID, index: 0, text: "late" }), cancelled);
});

test("events for another answer are ignored", () => {
  const OTHER = "33333333-3333-4333-8333-333333333333";
  const state = run([...NORMAL, { type: "answer.delta", answerId: OTHER, index: 0, text: "FOREIGN" }]);
  assert.equal(state.text, "");
  assert.equal(state.phase, "generating");
});

test("a replayed delta index is ignored (no duplicated text) and the phase never moves backwards", () => {
  const state = run([
    ...NORMAL,
    { type: "answer.delta", answerId: AID, index: 0, text: "A" },
    { type: "answer.delta", answerId: AID, index: 0, text: "A" },
    { type: "retrieval.done", questionId: QID, chunkCount: 9, ms: 1 }, // out of order
  ]);
  assert.equal(state.text, "A");
  assert.equal(state.phase, "streaming");
});

test("a pre-stream or transport failure becomes failed only while a request is active", () => {
  const active = run([{ type: "start", questionText: "q" }, { type: "failure", code: "stream_interrupted", retryable: true }]);
  assert.equal(active.phase, "failed");
  assert.equal(reduceLive(IDLE, { type: "failure", code: "x", retryable: false }), IDLE);
  assert.equal(reduceLive(active, { type: "reset" }), IDLE);
});

test("start discards the previous turn completely", () => {
  const old = run([...NORMAL, { type: "answer.delta", answerId: AID, index: 0, text: "OLD" }]);
  const next = reduceLive(old, { type: "start", questionText: "new" });
  assert.deepEqual(next, { ...IDLE, phase: "sending", questionText: "new" });
});

// ---------------------------------------------------------------------------- messages

test("pre-stream refusals map to clear, safe messages", () => {
  const msg = (status: number, code: string, retryAfterSeconds: number | null = null, firstDetail: string | null = null) =>
    describeAskError({ status, code, retryAfterSeconds, firstDetail });
  assert.match(msg(429, "rate_limited", 7), /7 seconds/);
  assert.match(msg(429, "rate_limited"), /wait a moment/);
  assert.match(msg(429, "too_many_generations"), /still being generated/);
  assert.match(msg(409, "session_not_ready"), /not ready/);
  assert.match(msg(409, "materials_not_ready"), /deleted or is no longer Ready/);
  assert.match(msg(404, "session_not_found"), /not found/);
  assert.match(msg(404, "anything_else"), /not found/);
  assert.match(msg(406, "not_acceptable"), /streaming/);
  assert.match(msg(401, "invalid_token"), /sign in/);
  assert.match(msg(0, "network_error"), /Could not reach/);
  assert.equal(msg(422, "validation_error", null, "The question must not be empty."), "The question must not be empty.");
  assert.match(msg(422, "validation_error"), /Check it/);
});

test("unknown refusal codes and server text never reach the screen", () => {
  const text = describeAskError({ status: 500, code: "boom", retryAfterSeconds: null, firstDetail: "Traceback (most recent call last)" });
  assert.equal(text, "Something went wrong. Please try again.");
});

test("answer error codes map to fixed text; unknown or hostile codes get the generic one", () => {
  assert.match(answerErrorMessage("llm_unavailable"), /unavailable/);
  assert.match(answerErrorMessage("generation_interrupted"), /interrupted/);
  const generic = answerErrorMessage(null);
  for (const hostile of ["<script>alert(1)</script>", "constructor", "__proto__", "toString", "secret_sk-123", ""]) {
    assert.equal(answerErrorMessage(hostile), generic, hostile);
  }
});

test("question validation and request body", () => {
  assert.equal(questionError("   "), "Type a question first.");
  assert.equal(questionError("x".repeat(2001))?.includes("2000"), true);
  assert.equal(questionError("x".repeat(2000)), null);
  assert.deepEqual(toQuestionBody("  Why us?  ", ""), { text: "Why us?" });
  assert.deepEqual(toQuestionBody("Why?", "behavioral"), { text: "Why?", question_type_override: "behavioral" });
  // Only the documented fields: ownership and materials are never sent.
  assert.deepEqual(Object.keys(toQuestionBody("Why?", "coding")).sort(), ["question_type_override", "text"]);
  assert.equal(isUuid(QID), true);
  assert.equal(isUuid("../../etc/passwd"), false);
  assert.equal(isUuid(`${QID}/extra`), false);
});

// ---------------------------------------------------------------------------- the streaming request

type FetchCall = { url: string; init: RequestInit };

async function withFetch<T>(impl: (url: string, init: RequestInit) => Promise<Response>, body: () => Promise<T>): Promise<{ result: T; calls: FetchCall[] }> {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  globalThis.fetch = ((url: string, init: RequestInit) => {
    calls.push({ url, init });
    return impl(url, init);
  }) as typeof fetch;
  try {
    return { result: await body(), calls };
  } finally {
    globalThis.fetch = original;
  }
}

const json = (status: number, payload: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json", ...headers } });

test("apiStream asks for an event stream, sends the token and the JSON body, and returns the live response", async () => {
  const { result, calls } = await withFetch(
    async () => streamOf([created]),
    () => apiStream("/api/v1/sessions/s/questions", { body: { text: "Hi" }, token: "tok" }),
  );
  assert.equal(result.headers.get("Content-Type"), "text/event-stream");
  const call = calls[0]!;
  const headers = call.init.headers as Record<string, string>;
  assert.equal(call.init.method, "POST");
  assert.equal(headers["Accept"], "text/event-stream");
  assert.equal(headers["Authorization"], "Bearer tok");
  assert.equal(headers["Content-Type"], "application/json");
  assert.equal(call.init.body, JSON.stringify({ text: "Hi" }));
  assert.equal(call.init.credentials, "same-origin");
});

test("pre-stream JSON errors become ApiError with the status, code and Retry-After", async () => {
  const cases: [number, string, Record<string, string>][] = [
    [404, "session_not_found", {}],
    [406, "not_acceptable", {}],
    [409, "materials_not_ready", {}],
    [409, "session_not_ready", {}],
    [422, "validation_error", {}],
    [429, "rate_limited", { "Retry-After": "12" }],
    [429, "too_many_generations", { "Retry-After": "2" }],
    [401, "invalid_token", {}],
  ];
  for (const [status, code, headers] of cases) {
    const { calls } = await withFetch(
      async () => json(status, { error: { code, message: "x", request_id: "r" } }, headers),
      async () => {
        await assert.rejects(apiStream("/p", { body: {}, token: "t" }), (err: unknown) => {
          assert.ok(err instanceof ApiError);
          assert.equal(err.status, status);
          assert.equal(err.code, code);
          assert.equal(err.retryAfterSeconds, headers["Retry-After"] ? Number(headers["Retry-After"]) : null);
          return true;
        });
      },
    );
    assert.equal(calls.length, 1);
  }
});

test("a non-JSON error body still yields a safe ApiError", async () => {
  await withFetch(
    async () => new Response("<html>Bad gateway</html>", { status: 502 }),
    async () => {
      await assert.rejects(apiStream("/p", { body: {} }), (err: unknown) => {
        assert.ok(err instanceof ApiError);
        assert.equal(err.code, "unknown_error");
        assert.equal(err.message.includes("html"), false);
        return true;
      });
    },
  );
});

test("a 200 that is not an event stream is rejected", async () => {
  await withFetch(
    async () => json(200, { hello: "world" }),
    async () => {
      await assert.rejects(apiStream("/p", { body: {} }), (err: unknown) => err instanceof ApiError && err.code === "bad_response");
    },
  );
});

test("a network failure is a network_error; the caller's own abort is NOT", async () => {
  await withFetch(
    async () => {
      throw new TypeError("fetch failed");
    },
    async () => {
      await assert.rejects(apiStream("/p", { body: {} }), (err: unknown) => err instanceof ApiError && err.code === "network_error");
    },
  );
  const controller = new AbortController();
  controller.abort();
  await withFetch(
    async (_url, init) => {
      throw init.signal?.reason ?? new DOMException("aborted", "AbortError");
    },
    async () => {
      await assert.rejects(apiStream("/p", { body: {}, signal: controller.signal }), (err: unknown) => !(err instanceof ApiError));
    },
  );
});

// ---------------------------------------------------------------------------- one ask, end to end

/** Drive runAsk through the real reducer, like the page does. */
async function ask(open: (signal: AbortSignal) => Promise<Response>, controller = new AbortController()) {
  let state: LiveAnswer = IDLE;
  const seen: string[] = [];
  let accepted = 0;
  const outcome = await runAsk("Why this role?", {
    open,
    signal: controller.signal,
    dispatch: (action) => {
      state = reduceLive(state, action);
      seen.push(state.phase);
    },
    onAccepted: () => {
      accepted += 1;
    },
  });
  return { outcome, state, seen, accepted };
}

const apiError = (status: number, code: string, headers: Record<string, string> = {}) => async () => {
  throw new ApiError(status, code, "server text", [], headers["Retry-After"] ? Number(headers["Retry-After"]) : null);
};

test("runAsk: a successful turn ends done with the text, the metrics, and the question kept", async () => {
  const r = await ask(async () => streamOf([created, retrieved, started, delta(0, "Hi "), delta(1, "there"), done]));
  assert.deepEqual(r.outcome, { kind: "streamed" });
  assert.equal(r.state.phase, "done");
  assert.equal(r.state.text, "Hi there");
  assert.equal(r.state.questionText, "Why this role?");
  assert.deepEqual(r.seen, ["sending", "retrieving", "generating", "generating", "streaming", "streaming", "done"]);
  assert.equal(r.accepted, 1);
});

test("runAsk: answer.error ends failed with the partial text and a safe code", async () => {
  const r = await ask(async () => streamOf([ERROR]));
  assert.equal(r.state.phase, "failed");
  assert.equal(r.state.text, "Partial ");
  assert.deepEqual(r.state.failure, { code: "llm_unavailable", retryable: true });
});

test("runAsk: pre-stream refusals reset the turn and return a message (no answer is shown)", async () => {
  const cases: [number, string, string, Record<string, string>][] = [
    [404, "session_not_found", "not found", {}],
    [406, "not_acceptable", "streaming", {}],
    [409, "materials_not_ready", "no longer Ready", {}],
    [409, "session_not_ready", "not ready", {}],
    [422, "validation_error", "Check it", {}],
    [429, "rate_limited", "12 seconds", { "Retry-After": "12" }],
    [429, "too_many_generations", "still being generated", {}],
    [401, "invalid_token", "sign in", {}],
    [0, "network_error", "Could not reach", {}],
  ];
  for (const [status, code, expected, headers] of cases) {
    const r = await ask(apiError(status, code, headers));
    assert.equal(r.outcome.kind, "refused", code);
    assert.ok(r.outcome.kind === "refused" && r.outcome.message.includes(expected), `${code}: ${JSON.stringify(r.outcome)}`);
    assert.equal(r.outcome.kind === "refused" && r.outcome.message.includes("server text"), false);
    assert.equal(r.state, IDLE, `${code} must leave no live turn`);
    assert.equal(r.accepted, 0, `${code} must not report the question as accepted`);
  }
});

test("runAsk: aborting before the response arrives is a cancel, not an error message", async () => {
  const controller = new AbortController();
  const r = await ask(async (signal) => {
    controller.abort();
    throw signal.reason ?? new DOMException("aborted", "AbortError");
  }, controller);
  assert.deepEqual(r.outcome, { kind: "streamed" });
  assert.equal(r.state.phase, "cancelled");
});

test("runAsk: aborting mid-stream keeps the partial text and ends cancelled", async () => {
  const controller = new AbortController();
  const encoder = new TextEncoder();
  const r = await ask(async (signal) => {
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(encoder.encode(created + started + delta(0, "Part")));
        signal.addEventListener("abort", () => c.error(signal.reason));
        setTimeout(() => controller.abort(new DOMException("aborted", "AbortError")), 20);
      },
    });
    return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
  }, controller);
  assert.equal(r.state.phase, "cancelled");
  assert.equal(r.state.text, "Part");
});

test("runAsk: a stream that ends without answer.done or answer.error fails as interrupted, keeping the text", async () => {
  const r = await ask(async () => streamOf([created, retrieved, started, delta(0, "Half")]));
  assert.equal(r.state.phase, "failed");
  assert.equal(r.state.text, "Half");
  assert.equal(r.state.failure?.code, "stream_interrupted");
});

test("runAsk: a malformed event fails the turn as bad_response and keeps what arrived before it", async () => {
  const r = await ask(async () => streamOf([created, started, delta(0, "OK "), frame("answer.delta", "{nope")]));
  assert.equal(r.state.phase, "failed");
  assert.equal(r.state.text, "OK ");
  assert.equal(r.state.failure?.code, "bad_response");
});

test("runAsk: an unexpected transport error mid-stream fails as interrupted, never leaking its text", async () => {
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new TextEncoder().encode(created));
      c.error(new TypeError("socket hang up: secret-internal-host:5432"));
    },
  });
  const r = await ask(async () => new Response(body, { headers: { "Content-Type": "text/event-stream" } }));
  assert.equal(r.state.phase, "failed");
  assert.equal(r.state.failure?.code, "stream_interrupted");
  assert.equal(JSON.stringify(r.state).includes("secret-internal-host"), false);
});

test("runAsk: a completed answer is not downgraded by an abort that arrives right after answer.done", async () => {
  const controller = new AbortController();
  const r = await ask(async () => streamOf([created, started, delta(0, "A"), done]), controller);
  controller.abort(); // the user clicks Stop / leaves the page just after completion
  assert.equal(r.state.phase, "done");
  assert.equal(r.state.text, "A");
});
