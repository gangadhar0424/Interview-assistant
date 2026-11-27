/** M-9 renderer state: switching, completing, reopening, deleting, exporting, failures. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DesktopResult, SessionExportResult, SessionSummary } from "../src/shared/contracts";
import { useDesktopStore } from "../src/renderer/src/store";

const A = "11111111-2222-4333-8444-aaaaaaaaaaaa";
const B = "11111111-2222-4333-8444-bbbbbbbbbbbb";
const C = "11111111-2222-4333-8444-cccccccccccc";

const ok = <T,>(data: T): DesktopResult<T> => ({ ok: true, data });
const fail = (kind: "network" | "http", message: string, code?: string): DesktopResult<never> => ({
  ok: false,
  error: { kind, message, ...(code ? { code } : {}) } as never,
});
const NETWORK = fail("network", "Could not reach the backend. Check your connection and try again.", "network_error");

const summary = (id: string, over: Partial<SessionSummary> = {}): SessionSummary => ({
  id,
  type: "interview",
  title: null,
  company_name: `Company ${id.slice(-1)}`,
  status: "ready",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  resume: { id: "r", filename: "cv.pdf" },
  document_count: 0,
  ...over,
});

const question = (text: string) => ({ id: text, text }) as never;

function bridge() {
  return {
    app: { checkBackend: vi.fn(async () => ok({ status: "connected" as const })) },
    sessions: {
      list: vi.fn(async () => ok([summary(A), summary(B)])),
      questions: vi.fn(async (id: string) => ok(id === A ? [question("A-question")] : [question("B-question")])),
      rename: vi.fn(async (id: string, title: string) => ok(summary(id, { title }))),
      end: vi.fn(async (id: string) => ok(summary(id, { status: "ended", activity_state: "completed" as const }))),
      reopen: vi.fn(async (id: string) => ok(summary(id))),
      remove: vi.fn(async () => ok({ deleted: true })),
      exportToFile: vi.fn(async (): Promise<DesktopResult<SessionExportResult>> => ok({ saved: true, fileName: "session-aaaaaaaa.md" })),
      cancelQuestion: vi.fn(async () => ok(undefined)),
    },
    capture: { cancel: vi.fn(async () => ok(undefined)) },
    audio: { start: vi.fn(async () => ok(undefined)), stop: vi.fn(async () => ok(undefined)), sendChunk: vi.fn() },
  };
}

let desktop: ReturnType<typeof bridge>;
const state = () => useDesktopStore.getState();

beforeEach(() => {
  desktop = bridge();
  vi.stubGlobal("window", { desktop });
  useDesktopStore.setState({
    user: { id: "u", email: "p@example.com", created_at: "x" },
    sessions: [summary(A), summary(B)],
    selectedSessionId: A,
    sessionBusyId: null,
    sessionNotice: null,
    history: [question("A-question")],
    connection: "connected",
    error: null,
    errorKind: null,
    liveAnswer: null,
    capture: { phase: "idle" },
    captureNotice: null,
  });
});

describe("switching sessions", () => {
  it("clears the previous session's history and answer before the new one loads", async () => {
    useDesktopStore.setState({
      liveAnswer: { requestId: "r", question: "q", phase: "done", text: "A's answer" } as never,
    });
    let seenDuringLoad: { history: unknown[]; live: unknown } | null = null;
    desktop.sessions.questions.mockImplementationOnce(async () => {
      seenDuringLoad = { history: [...state().history], live: state().liveAnswer };
      return ok([question("B-question")]);
    });
    await state().selectSession(B);
    expect(seenDuringLoad).toEqual({ history: [], live: null });
    expect(state().selectedSessionId).toBe(B);
    expect(state().history).toEqual([question("B-question")]);
  });

  it("never shows the old session's history when loading the new one fails", async () => {
    desktop.sessions.questions.mockResolvedValueOnce(NETWORK);
    await state().selectSession(B);
    expect(state().selectedSessionId).toBe(B);
    expect(state().history).toEqual([]);
    expect(state().connection).toBe("unavailable");
    expect(state().error).toMatch(/Could not reach the backend/);
    // and a retry (opening it again) recovers
    await state().selectSession(B);
    expect(state().history).toEqual([question("B-question")]);
    expect(state().connection).toBe("connected");
  });

  it("stops listening and drops a screenshot before switching (existing behaviour kept)", async () => {
    useDesktopStore.setState({
      audio: { state: "listening" } as never,
      capture: { phase: "countdown" } as never,
    });
    await state().selectSession(B);
    expect(desktop.capture.cancel).toHaveBeenCalledTimes(1);
    expect(desktop.audio.stop).toHaveBeenCalledTimes(1);
    expect(state().audio.state).toBe("idle");
  });

  it("cancels a running answer so it cannot finish under the other session", async () => {
    useDesktopStore.setState({
      liveAnswer: { requestId: "req-1", question: "q", phase: "streaming", text: "partial" } as never,
    });
    await state().selectSession(B);
    expect(desktop.sessions.cancelQuestion).toHaveBeenCalledWith("req-1");
    expect(state().liveAnswer).toBeNull();
  });

  it("selecting the open session again keeps a running answer and reloads its history", async () => {
    useDesktopStore.setState({
      liveAnswer: { requestId: "req-1", question: "q", phase: "streaming", text: "partial" } as never,
    });
    await state().selectSession(A);
    expect(desktop.sessions.cancelQuestion).not.toHaveBeenCalled();
    expect(state().liveAnswer?.requestId).toBe("req-1");
    expect(desktop.sessions.questions).toHaveBeenCalledWith(A);
  });

  it("never starts audio or a capture", async () => {
    await state().selectSession(B);
    expect(desktop.audio.start).not.toHaveBeenCalled();
  });
});

describe("resuming the last session", () => {
  it("opens the most recently used session that can take questions", async () => {
    desktop.sessions.list.mockResolvedValueOnce(
      ok([
        summary(A, { last_activity_at: "2026-05-01T00:00:00Z" }),
        summary(B, { last_activity_at: "2026-05-20T00:00:00Z" }),
        summary(C, { status: "ended", last_activity_at: "2026-05-30T00:00:00Z" }),
      ]),
    );
    useDesktopStore.setState({ selectedSessionId: null });
    await state().refreshSessions();
    expect(state().selectedSessionId).toBe(B);
    expect(desktop.audio.start).not.toHaveBeenCalled();
  });

  it("keeps the open session on refresh", async () => {
    await state().refreshSessions();
    expect(state().selectedSessionId).toBe(A);
  });
});

describe("rename", () => {
  it("updates the list from the server's answer", async () => {
    expect(await state().renameSession(B, "Round two")).toBe(true);
    expect(desktop.sessions.rename).toHaveBeenCalledWith(B, "Round two");
    expect(state().sessions.find((s) => s.id === B)?.title).toBe("Round two");
    expect(state().sessionNotice).toEqual({ text: "Session renamed.", tone: "ok" });
    expect(state().sessionBusyId).toBeNull();
  });

  it("keeps the old name and reports a failure", async () => {
    desktop.sessions.rename.mockResolvedValueOnce(NETWORK);
    expect(await state().renameSession(B, "Round two")).toBe(false);
    expect(state().sessions.find((s) => s.id === B)?.title).toBeNull();
    expect(state().sessionNotice?.tone).toBe("error");
    expect(state().sessionBusyId).toBeNull();
  });

  it("refuses a second action while one is running", async () => {
    useDesktopStore.setState({ sessionBusyId: A });
    expect(await state().renameSession(B, "x")).toBe(false);
    expect(desktop.sessions.rename).not.toHaveBeenCalled();
  });
});

describe("complete and reopen", () => {
  it("completing the open session first stops its listening and capture", async () => {
    useDesktopStore.setState({ audio: { state: "listening" } as never });
    await state().endSession(A);
    expect(desktop.audio.stop).toHaveBeenCalled();
    expect(desktop.sessions.end).toHaveBeenCalledWith(A);
    expect(state().sessions.find((s) => s.id === A)?.status).toBe("ended");
    expect(state().sessionNotice?.tone).toBe("ok");
  });

  it("completing another session does not disturb the open one", async () => {
    useDesktopStore.setState({ audio: { state: "listening" } as never });
    await state().endSession(B);
    expect(desktop.audio.stop).not.toHaveBeenCalled();
    expect(state().selectedSessionId).toBe(A);
  });

  it("reopening starts nothing", async () => {
    useDesktopStore.setState({ sessions: [summary(A, { status: "ended" }), summary(B)] });
    await state().reopenSession(A);
    expect(desktop.sessions.reopen).toHaveBeenCalledWith(A);
    expect(state().sessions.find((s) => s.id === A)?.status).toBe("ready");
    expect(desktop.audio.start).not.toHaveBeenCalled();
    expect(state().sessionNotice?.text).toMatch(/Nothing is recording/);
  });

  it("a session that no longer exists refreshes the list and says so", async () => {
    desktop.sessions.end.mockResolvedValueOnce(fail("http", "Session not found.", "session_not_found"));
    desktop.sessions.list.mockResolvedValueOnce(ok([summary(A)]));
    await state().endSession(B);
    expect(state().sessionNotice).toEqual({ text: "This session is no longer available.", tone: "error" });
    expect(state().sessions.map((s) => s.id)).toEqual([A]);
  });

  it("a network failure says nothing changed", async () => {
    desktop.sessions.end.mockResolvedValueOnce(NETWORK);
    await state().endSession(B);
    expect(state().sessionNotice?.text).toMatch(/was not changed/);
    expect(state().sessions.find((s) => s.id === B)?.status).toBe("ready");
  });
});

describe("delete", () => {
  it("removes the open session, clears its history and opens another without recording", async () => {
    await state().deleteSession(A);
    expect(desktop.sessions.remove).toHaveBeenCalledWith(A);
    expect(state().sessions.map((s) => s.id)).toEqual([B]);
    expect(state().selectedSessionId).toBe(B);
    expect(state().history).toEqual([question("B-question")]);
    expect(desktop.audio.start).not.toHaveBeenCalled();
  });

  it("declining the native confirmation changes nothing", async () => {
    desktop.sessions.remove.mockResolvedValueOnce(ok({ deleted: false }));
    await state().deleteSession(A);
    expect(state().sessions).toHaveLength(2);
    expect(state().selectedSessionId).toBe(A);
    expect(state().history).toEqual([question("A-question")]);
    expect(state().sessionNotice).toBeNull();
    expect(state().sessionBusyId).toBeNull();
  });

  it("deleting a session that is not open leaves the open one alone", async () => {
    await state().deleteSession(B);
    expect(state().sessions.map((s) => s.id)).toEqual([A]);
    expect(state().selectedSessionId).toBe(A);
    expect(state().history).toEqual([question("A-question")]);
  });

  it("deleting the last session leaves nothing selected", async () => {
    useDesktopStore.setState({ sessions: [summary(A)] });
    await state().deleteSession(A);
    expect(state().selectedSessionId).toBeNull();
    expect(state().history).toEqual([]);
  });

  it("a failed delete keeps the session", async () => {
    desktop.sessions.remove.mockResolvedValueOnce(NETWORK);
    await state().deleteSession(A);
    expect(state().sessions).toHaveLength(2);
    expect(state().sessionNotice?.tone).toBe("error");
  });
});

describe("export", () => {
  it("reports the saved file name only; no content reaches the renderer", async () => {
    await state().exportSession(A, "markdown");
    expect(desktop.sessions.exportToFile).toHaveBeenCalledWith(A, "markdown");
    expect(state().sessionNotice).toEqual({ text: "Saved session-aaaaaaaa.md.", tone: "ok" });
    expect(JSON.stringify(state().sessionNotice)).not.toMatch(/A-question/);
  });

  it("says nothing when the user cancels", async () => {
    desktop.sessions.exportToFile.mockResolvedValueOnce(ok({ saved: false, fileName: null }));
    await state().exportSession(A, "pdf");
    expect(state().sessionNotice).toBeNull();
    expect(state().sessionBusyId).toBeNull();
  });

  it("shows a failure and frees the busy flag", async () => {
    desktop.sessions.exportToFile.mockResolvedValueOnce(NETWORK);
    await state().exportSession(A, "pdf");
    expect(state().sessionNotice?.tone).toBe("error");
    expect(state().sessionBusyId).toBeNull();
  });
});
