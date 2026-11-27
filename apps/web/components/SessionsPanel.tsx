"use client";

import Link from "next/link";
import { DesktopHandoff } from "@/components/DesktopHandoff";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  DELETE_WARNING,
  EXPORT_WARNING,
  activityLabel,
  activityState,
  canComplete,
  canReopen,
  exportFileName,
  formatActivity,
  sessionTitle,
  type ExportFormat,
} from "@/lib/session-management";
import { type SessionList, type SessionSummary } from "@/lib/sessions";

const PAGE_SIZE = 20;

function pageUrl(cursor: string | null): string {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (cursor) query.set("cursor", cursor);
  return `/api/v1/sessions?${query.toString()}`;
}

type Notice = { text: string; tone: "ok" | "error" };

// Dashboard "Call Sessions" list (FR-HIS-1, M-9): the signed-in user's sessions, newest first,
// with state, company, last activity and question count, plus rename, complete/reopen, export and
// delete. The API only ever returns (and only ever changes) the caller's own sessions; a session
// that is gone is reported and the list refreshed. Export and delete ask first and never start
// any capture.
export function SessionsPanel() {
  const { authedRequest } = useAuth();
  const [items, setItems] = useState<SessionSummary[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const result = await authedRequest<SessionList>(pageUrl(null));
      if (!mounted.current) return;
      setItems(result.items);
      setNextCursor(result.next_cursor);
      setError(null);
    } catch (err) {
      if (!mounted.current) return;
      setError(err instanceof ApiError ? err.message : "Could not load your sessions.");
    }
  }, [authedRequest]);

  useEffect(() => {
    let cancelled = false;
    authedRequest<SessionList>(pageUrl(null))
      .then((result) => {
        if (cancelled) return;
        setItems(result.items);
        setNextCursor(result.next_cursor);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load your sessions.");
      });
    return () => {
      cancelled = true;
    };
  }, [authedRequest]);

  const loadMore = useCallback(async () => {
    if (!nextCursor) return;
    setLoadingMore(true);
    try {
      const result = await authedRequest<SessionList>(pageUrl(nextCursor));
      setItems((current) => [...(current ?? []), ...result.items]);
      setNextCursor(result.next_cursor);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load more sessions.");
    } finally {
      setLoadingMore(false);
    }
  }, [authedRequest, nextCursor]);

  // One management action at a time. A missing session refreshes the list; a network failure says
  // nothing was changed; anything else shows the server's safe message.
  async function run(id: string, action: () => Promise<string | null>) {
    if (busyId !== null) return;
    setBusyId(id);
    setNotice(null);
    try {
      const done = await action();
      if (done && mounted.current) setNotice({ text: done, tone: "ok" });
    } catch (err) {
      if (!mounted.current) return;
      if (err instanceof ApiError && err.status === 404) {
        setNotice({ text: "This session is no longer available.", tone: "error" });
        void load();
      } else if (err instanceof ApiError && err.code === "network_error") {
        setNotice({
          text: "Could not reach the server, so the session was not changed. Try again when you are connected.",
          tone: "error",
        });
      } else {
        setNotice({
          text: err instanceof ApiError ? err.message : "Something went wrong. Please try again.",
          tone: "error",
        });
      }
    } finally {
      if (mounted.current) setBusyId(null);
    }
  }

  function replace(updated: SessionSummary) {
    setItems((current) => (current ?? []).map((item) => (item.id === updated.id ? updated : item)));
  }

  const rename = (item: SessionSummary, event: FormEvent) => {
    event.preventDefault();
    void run(item.id, async () => {
      const updated = await authedRequest<SessionSummary>(`/api/v1/sessions/${item.id}`, {
        method: "PATCH",
        body: { title: draftName.trim() },
      });
      replace(updated);
      setRenamingId(null);
      return "Session renamed.";
    });
  };

  const transition = (item: SessionSummary, verb: "end" | "reopen") =>
    void run(item.id, async () => {
      const updated = await authedRequest<SessionSummary>(`/api/v1/sessions/${item.id}/${verb}`, {
        method: "POST",
      });
      replace(updated);
      return verb === "end"
        ? "Session completed. You can reopen it later; your history is kept."
        : "Session reopened. Nothing is recording: audio and screenshots stay off until you turn them on.";
    });

  const remove = (item: SessionSummary) => {
    if (!window.confirm(DELETE_WARNING)) return;
    void run(item.id, async () => {
      await authedRequest<void>(`/api/v1/sessions/${item.id}`, { method: "DELETE" });
      setItems((current) => (current ?? []).filter((other) => other.id !== item.id));
      return "Session deleted.";
    });
  };

  const exportSession = (item: SessionSummary, format: ExportFormat) => {
    if (!window.confirm(EXPORT_WARNING)) return;
    void run(item.id, async () => {
      const blob = await authedRequest<Blob>(`/api/v1/sessions/${item.id}/export?format=${format}`, {
        asBlob: true,
      });
      // The browser saves the file to the user's chosen downloads location; nothing else is sent.
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = exportFileName(item.id, format);
      link.click();
      URL.revokeObjectURL(url);
      return `Export ready: ${link.download}.`;
    });
  };

  return (
    <section aria-labelledby="sessions-title" style={{ width: "100%", maxWidth: "40rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 id="sessions-title" style={{ margin: 0 }}>
          Call Sessions
        </h2>
        <Link
          href="/sessions/new"
          style={{
            padding: "0.5rem 1rem",
            border: "1px solid #333",
            borderRadius: "4px",
            textDecoration: "none",
            color: "inherit",
          }}
        >
          Create session
        </Link>
      </div>

      {error && (
        <p role="alert" style={{ color: "#b00020" }}>
          {error}{" "}
          <button type="button" onClick={() => void load()}>
            Retry
          </button>
        </p>
      )}
      {notice && (
        <p
          role={notice.tone === "error" ? "alert" : "status"}
          style={{ color: notice.tone === "error" ? "#b00020" : "#146c2e" }}
          data-testid="session-notice"
        >
          {notice.text}
        </p>
      )}

      {items === null ? (
        !error && <p>Loading…</p>
      ) : items.length === 0 ? (
        <p style={{ color: "#666" }}>
          No sessions yet. Upload a resume below, wait until it is Ready, then create a session.
        </p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: "0.5rem" }}>
          {items.map((item) => {
            const state = activityState(item);
            const when = formatActivity(item.last_activity_at ?? item.updated_at);
            const questions = item.question_count;
            const working = busyId === item.id;
            return (
              <li
                key={item.id}
                data-testid="session-item"
                aria-busy={working}
                style={{ borderBottom: "1px solid #ddd", paddingBottom: "0.5rem" }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: "0.75rem" }}>
                  <Link
                    href={`/sessions/${item.id}`}
                    className="session-link"
                    style={{ color: "inherit", overflowWrap: "anywhere" }}
                  >
                    <strong>{sessionTitle(item)}</strong>
                  </Link>
                  <span
                    data-testid="session-state"
                    style={{
                      fontSize: "0.8rem",
                      border: "1px solid #bbb",
                      borderRadius: "999px",
                      padding: "0 0.6rem",
                      flexShrink: 0,
                    }}
                  >
                    {activityLabel(state)}
                  </span>
                </div>
                {item.title && <div style={{ overflowWrap: "anywhere" }}>{item.company_name}</div>}
                <div style={{ color: "#666", fontSize: "0.85rem", overflowWrap: "anywhere" }}>
                  Created {new Date(item.created_at).toLocaleDateString()}
                  {when && ` · Last activity ${when}`}
                  {questions !== undefined && ` · ${questions} ${questions === 1 ? "question" : "questions"}`}
                  {" · Resume: "}
                  {item.resume.id === null ? `${item.resume.filename} (deleted)` : item.resume.filename}{" "}
                  · {item.document_count} {item.document_count === 1 ? "document" : "documents"}
                </div>
                {state === "interrupted" && (
                  <div style={{ color: "#8a5a00", fontSize: "0.85rem" }}>
                    An answer was cut off. Open the session to see it in the history and ask again.
                  </div>
                )}

                {renamingId === item.id ? (
                  <form
                    onSubmit={(event) => rename(item, event)}
                    style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem", flexWrap: "wrap" }}
                  >
                    <label className="sr-only" htmlFor={`rename-${item.id}`}>
                      Session name
                    </label>
                    <input
                      id={`rename-${item.id}`}
                      value={draftName}
                      maxLength={200}
                      autoFocus
                      onChange={(event) => setDraftName(event.target.value)}
                      style={{ flex: "1 1 12rem" }}
                    />
                    <button type="submit" disabled={busyId !== null}>
                      Save
                    </button>
                    <button type="button" onClick={() => setRenamingId(null)}>
                      Cancel
                    </button>
                  </form>
                ) : (
                  <div className="session-actions">
                    {item.status === "ready" && <DesktopHandoff sessionId={item.id} compact />}
                    <Link href={`/sessions/${item.id}`} className="open-session">
                      Open session
                    </Link>
                    <button
                      type="button"
                      disabled={busyId !== null}
                      onClick={() => {
                        setDraftName(item.title ?? "");
                        setRenamingId(item.id);
                      }}
                    >
                      Rename
                    </button>
                    {canReopen(item) ? (
                      <button type="button" disabled={busyId !== null} onClick={() => transition(item, "reopen")}>
                        Reopen
                      </button>
                    ) : (
                      canComplete(item) && (
                        <button type="button" disabled={busyId !== null} onClick={() => transition(item, "end")}>
                          Complete
                        </button>
                      )
                    )}
                    <button type="button" disabled={busyId !== null} onClick={() => exportSession(item, "markdown")}>
                      Export
                    </button>
                    <button type="button" disabled={busyId !== null} onClick={() => exportSession(item, "pdf")}>
                      Export PDF
                    </button>
                    <button type="button" disabled={busyId !== null} onClick={() => remove(item)}>
                      Delete
                    </button>
                    {working && <span role="status">Working…</span>}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {nextCursor && (
        <button type="button" onClick={() => void loadMore()} disabled={loadingMore}>
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </section>
  );
}
