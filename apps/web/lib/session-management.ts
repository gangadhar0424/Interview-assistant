// Pure helpers for session management in the web app (M-9). They mirror docs/SESSION_MANAGEMENT.md;
// the server is always the authority (every rule here is a UX convenience).
//
// Imported by Node's test runner as well as the app: plain, erasable TypeScript, no enums, no path
// aliases, relative imports with ".ts".

export type ActivityState = "active" | "completed" | "interrupted";

export type ManagedSession = {
  id: string;
  title: string | null;
  company_name: string;
  status: string;
  updated_at: string;
  last_activity_at?: string;
  question_count?: number;
  activity_state?: ActivityState;
};

export type ExportFormat = "markdown" | "json";

/** What to show: the server's derived state, or (older server) a guess from the status. */
export function activityState(session: ManagedSession): ActivityState {
  if (session.activity_state) return session.activity_state;
  return session.status === "ended" || session.status === "archived" ? "completed" : "active";
}

export function activityLabel(state: ActivityState): string {
  return state === "completed" ? "Completed" : state === "interrupted" ? "Interrupted" : "Active";
}

export function sessionTitle(session: ManagedSession): string {
  return session.title?.trim() || session.company_name;
}

export function canComplete(session: ManagedSession): boolean {
  return session.status === "ready" || session.status === "active";
}

export function canReopen(session: ManagedSession): boolean {
  return session.status === "ended";
}

/** "just now", "5 min ago", "3 h ago", "2 d ago", else the date. Invalid input gives "". */
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

/** Same name the server suggests: it carries no company, title or user data. */
export function exportFileName(sessionId: string, format: ExportFormat): string {
  return `session-${sessionId.slice(0, 8)}.${format === "markdown" ? "md" : "json"}`;
}

export const EXPORT_WARNING =
  "Save this session to a file on this computer?\n\n" +
  "The file contains the session details, the job description you entered, your answer " +
  "preferences, and every question and answer in this session, as readable text. It does not " +
  "contain your resume or document text, audio, screenshots, passwords, tokens or API keys.\n\n" +
  "Anyone who can open the file can read it, and it is not removed when you delete the session. " +
  "Nothing is sent anywhere.";

export const DELETE_WARNING =
  "Delete this session and its question history?\n\n" +
  "The session, its questions and its answers are removed from your account and cannot be " +
  "recovered. Your resume and documents are not deleted.";

/** A fresh key for one create attempt; reused only to retry the identical request. */
export function newIdempotencyKey(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Remembers the key of the last create attempt so a retry of the same payload (after a lost
 * response) carries the same key and the server returns the session it already made. A different
 * payload gets a new key; success forgets the key.
 */
export class CreateAttempt {
  private fingerprint: string | null = null;
  private key: string | null = null;

  keyFor(payload: unknown): string {
    const fingerprint = JSON.stringify(payload);
    if (this.key === null || this.fingerprint !== fingerprint) {
      this.fingerprint = fingerprint;
      this.key = newIdempotencyKey();
    }
    return this.key;
  }

  succeeded(): void {
    this.fingerprint = null;
    this.key = null;
  }
}
