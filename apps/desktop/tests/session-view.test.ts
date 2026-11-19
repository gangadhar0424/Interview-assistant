import { describe, expect, it } from "vitest";
import type { SessionSummary } from "../src/shared/contracts";
import {
  formatActivity,
  isUsable,
  pickResumeSession,
  sessionName,
  sessionState,
  stateLabel,
} from "../src/shared/session-view";

const base: SessionSummary = {
  id: "a",
  type: "interview",
  title: null,
  company_name: "Acme",
  status: "ready",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  resume: { id: "r", filename: "cv.pdf" },
  document_count: 0,
};
const make = (over: Partial<SessionSummary>): SessionSummary => ({ ...base, ...over });

describe("session state", () => {
  it("uses the server's derived state", () => {
    expect(sessionState(make({ activity_state: "interrupted" }))).toBe("interrupted");
    expect(sessionState(make({ activity_state: "completed", status: "ended" }))).toBe("completed");
  });

  it("falls back to the status when an older server sends no state", () => {
    expect(sessionState(make({ status: "ready" }))).toBe("active");
    expect(sessionState(make({ status: "ended" }))).toBe("completed");
    expect(sessionState(make({ status: "archived" }))).toBe("completed");
  });

  it("has a label for each state and only ready sessions are usable", () => {
    expect(stateLabel("active")).toBe("Active");
    expect(stateLabel("completed")).toBe("Completed");
    expect(stateLabel("interrupted")).toBe("Interrupted");
    expect(isUsable(make({ status: "ready" }))).toBe(true);
    expect(isUsable(make({ status: "ended" }))).toBe(false);
  });

  it("names a session by its title, else its company", () => {
    expect(sessionName(make({ title: "  Round 2 " }))).toBe("Round 2");
    expect(sessionName(make({ title: "   " }))).toBe("Acme");
    expect(sessionName(make({ title: null }))).toBe("Acme");
  });
});

describe("last activity text", () => {
  const now = Date.parse("2026-06-01T12:00:00Z");
  const ago = (ms: number) => new Date(now - ms).toISOString();
  it("is relative for recent times and never throws on bad input", () => {
    expect(formatActivity(ago(5_000), now)).toBe("just now");
    expect(formatActivity(ago(5 * 60_000), now)).toBe("5 min ago");
    expect(formatActivity(ago(3 * 3_600_000), now)).toBe("3 h ago");
    expect(formatActivity(ago(4 * 86_400_000), now)).toBe("4 d ago");
    expect(formatActivity("not a date", now)).toBe("");
    expect(formatActivity(new Date(now + 60_000).toISOString(), now)).toBe("just now");
  });
});

describe("which session to open when none is chosen", () => {
  it("is null for an empty list", () => {
    expect(pickResumeSession([])).toBeNull();
  });

  it("keeps the first row when the server sends no activity data", () => {
    expect(pickResumeSession([make({ id: "x" }), make({ id: "y" })])).toBe("x");
  });

  it("prefers the most recently used session that can take questions", () => {
    const sessions = [
      make({ id: "new-but-idle", last_activity_at: "2026-05-01T00:00:00Z" }),
      make({ id: "recent", last_activity_at: "2026-05-20T00:00:00Z" }),
      make({ id: "done", status: "ended", last_activity_at: "2026-05-30T00:00:00Z" }),
    ];
    expect(pickResumeSession(sessions)).toBe("recent");
  });

  it("falls back to the first row when nothing is usable", () => {
    const sessions = [
      make({ id: "one", status: "ended", last_activity_at: "2026-05-01T00:00:00Z" }),
      make({ id: "two", status: "ended", last_activity_at: "2026-05-02T00:00:00Z" }),
    ];
    expect(pickResumeSession(sessions)).toBe("one");
  });
});
