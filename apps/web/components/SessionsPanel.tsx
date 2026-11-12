"use client";

import Link from "next/link";
import { DesktopHandoff } from "@/components/DesktopHandoff";
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { statusLabel, type SessionList, type SessionSummary } from "@/lib/sessions";

const PAGE_SIZE = 20;

function pageUrl(cursor: string | null): string {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (cursor) query.set("cursor", cursor);
  return `/api/v1/sessions?${query.toString()}`;
}

// Dashboard "Call Sessions" list (FR-HIS-1): the signed-in user's sessions, newest first, with
// status, company and created date. Read-only; the API only ever returns the caller's sessions.
export function SessionsPanel() {
  const { authedRequest } = useAuth();
  const [items, setItems] = useState<SessionSummary[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

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
          {error}
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
          {items.map((item) => (
            <li
              key={item.id}
              data-testid="session-item"
              style={{ borderBottom: "1px solid #ddd", paddingBottom: "0.5rem" }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", gap: "0.75rem" }}>
                <Link
                  href={`/sessions/${item.id}`}
                  className="session-link"
                  style={{ color: "inherit", overflowWrap: "anywhere" }}
                >
                  <strong>{item.company_name}</strong>
                </Link>
                <span
                  style={{
                    fontSize: "0.8rem",
                    border: "1px solid #bbb",
                    borderRadius: "999px",
                    padding: "0 0.6rem",
                    flexShrink: 0,
                  }}
                >
                  {statusLabel(item.status)}
                </span>
              </div>
              {item.title && <div style={{ overflowWrap: "anywhere" }}>{item.title}</div>}
              <div style={{ color: "#666", fontSize: "0.85rem", overflowWrap: "anywhere" }}>
                Created {new Date(item.created_at).toLocaleDateString()} · Resume:{" "}
                {item.resume.id === null ? `${item.resume.filename} (deleted)` : item.resume.filename}{" "}
                · {item.document_count} {item.document_count === 1 ? "document" : "documents"}
              </div>
              <div className="session-actions">
                {item.status === "ready" && <DesktopHandoff sessionId={item.id} compact />}
                <Link href={`/sessions/${item.id}`} className="open-session">
                  Open session
                </Link>
              </div>
            </li>
          ))}
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
