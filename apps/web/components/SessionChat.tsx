"use client";

import { useCallback, useEffect, useReducer, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  IDLE,
  QUESTION_MAX_CHARS,
  answerErrorMessage,
  formatMs,
  isActive,
  questionError,
  reduceLive,
  runAsk,
  toQuestionBody,
  type AnswerItem,
  type LiveAnswer,
  type QuestionItem,
  type QuestionPage,
} from "@/lib/qa";
import { labelFor, statusLabel, type AiOptions } from "@/lib/sessions";

// The session page body (Phase 8C): ask typed questions and watch the answer stream in. Every
// value shown (question, answer, company, errors) is rendered as plain text by React; nothing is
// ever inserted as HTML. The server is the authority for ownership, limits and readiness: this UI
// only offers hints and shows the server's refusals.

type SessionHeader = {
  id: string;
  title: string | null;
  company_name: string;
  status: string;
};

const PAGE = 100;
const MAX_PAGES = 10; // history is oldest-first, so the newest turns are on the last page

const ui: Record<string, CSSProperties> = {
  wrap: { display: "grid", gap: "1.25rem", width: "100%", maxWidth: "44rem" },
  card: { border: "1px solid #ddd", borderRadius: "6px", padding: "0.75rem", display: "grid", gap: "0.5rem" },
  q: { fontWeight: 600, whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: 0 },
  a: { whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: 0 },
  meta: { color: "#666", fontSize: "0.8rem", margin: 0 },
  error: { color: "#b00020", margin: 0 },
  input: { padding: "0.5rem", font: "inherit", width: "100%", boxSizing: "border-box" },
  row: { display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" },
};

const PHASE_TEXT: Record<string, string> = {
  sending: "Sending your question…",
  retrieving: "Searching your selected materials…",
  generating: "Writing the answer…",
  streaming: "Answering…",
};

function AnswerMeta({ answer }: { answer: AnswerItem }) {
  const bits: string[] = [];
  if (answer.provider && answer.model) bits.push(`${answer.provider} / ${answer.model}`);
  if (answer.fallback_used) bits.push("fallback provider used");
  if (answer.first_token_ms !== null) bits.push(`first token ${formatMs(answer.first_token_ms)}`);
  if (answer.total_ms !== null) bits.push(`total ${formatMs(answer.total_ms)}`);
  return bits.length > 0 ? <p style={ui.meta}>{bits.join(" · ")}</p> : null;
}

function HistoryAnswer({ answer }: { answer: AnswerItem }) {
  return (
    <div>
      {answer.text && <p style={ui.a}>{answer.text}</p>}
      {answer.status === "error" && (
        <p style={ui.error} role="status">
          {answerErrorMessage(answer.error_code)}
        </p>
      )}
      {answer.status === "cancelled" && <p style={ui.meta}>Stopped before it finished.</p>}
      {answer.status === "streaming" && <p style={ui.meta}>Still being generated…</p>}
      <AnswerMeta answer={answer} />
    </div>
  );
}

export function SessionChat({ sessionId }: { sessionId: string }) {
  const { authedRequest, authedStream } = useAuth();
  const base = `/api/v1/sessions/${sessionId}`;

  const [header, setHeader] = useState<SessionHeader | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [history, setHistory] = useState<QuestionItem[] | null>(null);
  const [historyNote, setHistoryNote] = useState<string | null>(null);
  const [types, setTypes] = useState<string[]>([]);

  const [text, setText] = useState("");
  const [override, setOverride] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [live, dispatch] = useReducer(reduceLive, IDLE);

  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  // Leaving the page (navigation, unmount) closes the stream; the server then records the answer
  // as cancelled with whatever text was produced (docs/API.md). Nothing is dispatched after that.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  // Pure fetch (no state): follows the cursors to the end, because history is oldest-first.
  const fetchHistory = useCallback(async (): Promise<{ items: QuestionItem[]; truncated: boolean }> => {
    const items: QuestionItem[] = [];
    let cursor: string | null = null;
    for (let page = 0; page < MAX_PAGES; page += 1) {
      const query = new URLSearchParams({ limit: String(PAGE) });
      if (cursor) query.set("cursor", cursor);
      const result: QuestionPage = await authedRequest<QuestionPage>(`${base}/questions?${query.toString()}`);
      items.push(...result.items);
      cursor = result.next_cursor;
      if (!cursor) return { items, truncated: false };
    }
    return { items, truncated: true };
  }, [authedRequest, base]);

  const applyHistory = useCallback((result: { items: QuestionItem[]; truncated: boolean }) => {
    setHistory(result.items);
    setHistoryNote(result.truncated ? "Only the first part of a very long history is shown." : null);
  }, []);

  const historyFailed = useCallback(() => {
    setHistoryNote("Could not load the question history.");
  }, []);

  // Refresh after a turn ends (the finished turn is then part of the list).
  const refreshHistory = useCallback(async () => {
    try {
      const result = await fetchHistory();
      if (mountedRef.current) applyHistory(result);
    } catch {
      if (mountedRef.current) historyFailed();
    }
  }, [fetchHistory, applyHistory, historyFailed]);

  useEffect(() => {
    let cancelled = false;
    authedRequest<SessionHeader>(base)
      .then((session) => {
        if (!cancelled) setHeader(session);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(
          err instanceof ApiError && err.status === 404 ? "This session was not found." : "Could not load this session.",
        );
      });
    authedRequest<AiOptions>("/api/v1/ai/options")
      .then((options) => {
        if (!cancelled) setTypes(options.question_types);
      })
      .catch(() => undefined); // the override is optional
    fetchHistory()
      .then((result) => {
        if (!cancelled) applyHistory(result);
      })
      .catch(() => {
        if (!cancelled) historyFailed();
      });
    return () => {
      cancelled = true;
    };
  }, [authedRequest, base, fetchHistory, applyHistory, historyFailed]);

  const busy = isActive(live.phase);
  const ready = header?.status === "ready";

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy || !ready) return;
    const problem = questionError(text);
    setFormError(problem);
    if (problem) return;

    const question = text.trim();
    const controller = new AbortController();
    abortRef.current = controller;
    const outcome = await runAsk(question, {
      open: (signal) => authedStream(`${base}/questions`, { body: toQuestionBody(question, override), signal }),
      dispatch: (action) => {
        if (mountedRef.current) dispatch(action); // nothing is dispatched after the page is left
      },
      signal: controller.signal,
      onAccepted: () => {
        if (mountedRef.current) setText(""); // the question was accepted
      },
    });
    if (abortRef.current === controller) abortRef.current = null;
    if (!mountedRef.current) return;
    if (outcome.kind === "refused") {
      setFormError(outcome.message); // a refusal is a message by the form, never an "answer"
      return;
    }
    await refreshHistory();
  }

  function onStop() {
    abortRef.current?.abort();
  }

  if (loadError) return <p role="alert" style={ui.error}>{loadError}</p>;
  if (header === null) return <p>Loading…</p>;

  const shownHistory = (history ?? []).filter((q) => q.id !== live.questionId);
  const typeError = formError;

  return (
    <section aria-labelledby="chat-title" style={ui.wrap}>
      <div>
        <h1 id="chat-title" style={{ margin: 0, overflowWrap: "anywhere" }}>
          {header.title ?? header.company_name}
        </h1>
        <p style={ui.meta}>
          {header.company_name} · {statusLabel(header.status)}
        </p>
      </div>

      {!ready && (
        <p role="status" style={ui.error}>
          This session is {statusLabel(header.status).toLowerCase()}, so new questions cannot be asked.
        </p>
      )}

      <form onSubmit={onSubmit} style={{ display: "grid", gap: "0.5rem" }} noValidate>
        <label htmlFor="question" style={{ fontWeight: 600 }}>
          Ask a question
        </label>
        <textarea
          id="question"
          rows={3}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={busy || !ready}
          style={ui.input}
          aria-describedby="question-hint"
          aria-invalid={typeError ? true : undefined}
        />
        <p id="question-hint" style={ui.meta}>
          {[...text.trim()].length}/{QUESTION_MAX_CHARS} characters
        </p>
        <div style={ui.row}>
          {types.length > 0 && (
            <label style={ui.row}>
              <span>Question type</span>
              <select value={override} onChange={(e) => setOverride(e.target.value)} disabled={busy || !ready}>
                <option value="">Use the session setting</option>
                {types.map((type) => (
                  <option key={type} value={type}>
                    {labelFor(type)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button type="submit" disabled={busy || !ready}>
            {busy ? "Answering…" : "Ask"}
          </button>
          {busy && (
            <button type="button" onClick={onStop}>
              Stop
            </button>
          )}
        </div>
        {typeError && (
          <p role="alert" style={ui.error}>
            {typeError}
          </p>
        )}
      </form>

      {live.phase !== "idle" && (
        <article aria-label="Current answer" style={ui.card} aria-busy={busy}>
          <p style={ui.q}>{live.questionText}</p>
          <p role="status" aria-live="polite" style={ui.meta}>
            {busy ? PHASE_TEXT[live.phase] : null}
            {live.phase === "done" && "Done."}
            {live.phase === "cancelled" && "Stopped. The part generated so far is kept."}
            {live.phase === "failed" && "The answer could not be completed."}
          </p>
          {live.chunkCount !== null && (
            <p style={ui.meta}>
              Used {live.chunkCount} {live.chunkCount === 1 ? "passage" : "passages"} from your materials
            </p>
          )}
          {live.text && <p style={ui.a}>{live.text}</p>}
          {live.phase === "failed" && live.failure && (
            <p style={ui.error} role="alert">
              {answerErrorMessage(live.failure.code)}
              {live.failure.retryable ? " You can try again." : ""}
            </p>
          )}
          <LiveMeta live={live} />
        </article>
      )}

      <div style={{ display: "grid", gap: "0.75rem" }}>
        <h2 style={{ margin: 0, fontSize: "1.1rem" }}>Earlier questions</h2>
        {historyNote && <p style={ui.meta}>{historyNote}</p>}
        {history === null ? (
          <p style={ui.meta}>Loading…</p>
        ) : shownHistory.length === 0 ? (
          <p style={ui.meta}>No earlier questions in this session.</p>
        ) : (
          shownHistory.map((question) => (
            <article key={question.id} style={ui.card} data-testid="history-item">
              <p style={ui.q}>{question.text}</p>
              {question.answers.map((answer) => (
                <HistoryAnswer key={answer.id} answer={answer} />
              ))}
            </article>
          ))
        )}
      </div>
    </section>
  );
}

function LiveMeta({ live }: { live: LiveAnswer }) {
  if (live.phase !== "done" || live.metrics === null) return null;
  const bits: string[] = [];
  if (live.provider && live.model) bits.push(`${live.provider} / ${live.model}`);
  if (live.fallbackUsed) bits.push("fallback provider used");
  bits.push(`search ${formatMs(live.metrics.retrieval_ms)}`);
  bits.push(`first token ${formatMs(live.metrics.first_token_ms)}`);
  bits.push(`total ${formatMs(live.metrics.total_ms)}`);
  return <p style={ui.meta}>{bits.join(" · ")}</p>;
}
