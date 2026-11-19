/** Pure helpers for showing sessions (M-9). No Electron or React here, so they are unit-tested. */
import type { SessionSummary } from "./contracts";

export type SessionState = "active" | "completed" | "interrupted";

/** What to show for a session. The server derives it; older servers fall back to the status. */
export function sessionState(session: SessionSummary): SessionState {
  if (session.activity_state) return session.activity_state;
  return session.status === "ended" || session.status === "archived" ? "completed" : "active";
}

export function stateLabel(state: SessionState): string {
  return state === "completed" ? "Completed" : state === "interrupted" ? "Interrupted" : "Active";
}

/** Only a `ready` session accepts questions, audio and screenshots (the server enforces it). */
export function isUsable(session: SessionSummary): boolean {
  return session.status === "ready";
}

export function sessionName(session: SessionSummary): string {
  return session.title?.trim() || session.company_name;
}

export function lastActivityIso(session: SessionSummary): string {
  return session.last_activity_at ?? session.updated_at;
}

/** "just now", "5 min ago", "3 h ago", "2 d ago", else the date. Invalid input shows nothing. */
export function formatActivity(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days} d ago`;
  return new Date(then).toLocaleDateString();
}

/**
 * Which session to open when none is chosen: the most recently used one that can still take
 * questions. Without activity data (an older server) it keeps the old behaviour, the first row.
 * It only selects; nothing here starts audio, capture or an answer.
 */
export function pickResumeSession(sessions: SessionSummary[]): string | null {
  if (sessions.length === 0) return null;
  if (!sessions.some((session) => session.last_activity_at)) return sessions[0]?.id ?? null;
  const usable = sessions
    .filter(isUsable)
    .sort((a, b) => Date.parse(lastActivityIso(b)) - Date.parse(lastActivityIso(a)));
  return (usable[0] ?? sessions[0])?.id ?? null;
}
