/** Web -> desktop handoff (D-55), renderer state: READY selects the prepared session and starts nothing. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { IDLE_HANDOFF, type DesktopResult, type HandoffState, type SessionSummary } from "../src/shared/contracts";
import { useDesktopStore } from "../src/renderer/src/store";

const PREPARED = "11111111-2222-4333-8444-555555555555";
const OTHER = "99999999-2222-4333-8444-555555555555";
const ok = <T,>(data: T): DesktopResult<T> => ({ ok: true, data });
const summary = (id: string): SessionSummary => ({
  id, type: "interview", title: null, company_name: id === PREPARED ? "Acme" : "Other", status: "ready",
  created_at: "x", updated_at: "x", resume: { id: null, filename: "cv.pdf" }, document_count: 0,
});
const READY: HandoffState = {
  phase: "ready",
  session: { id: PREPARED, title: "Backend", company_name: "Acme" },
  failure: null,
};
const user = { id: "u1", email: "p@example.com", created_at: "x" };

function bridge() {
  return {
    app: { checkBackend: vi.fn(async () => ok({ status: "connected" as const })) },
    auth: { logout: vi.fn(async () => ok(undefined)), getState: vi.fn(async () => ok({ user })) },
    sessions: {
      list: vi.fn(async () => ok([summary(OTHER), summary(PREPARED)])),
      questions: vi.fn(async () => ok([])),
      startQuestion: vi.fn(async () => ok(undefined)),
    },
    handoff: {
      getState: vi.fn(async () => ok(IDLE_HANDOFF)),
      dismiss: vi.fn(async () => ok(undefined)),
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
    user, sessions: [], selectedSessionId: OTHER, handoff: IDLE_HANDOFF,
    audio: { ...state().audio, state: "idle" }, capture: { phase: "idle" },
  });
});

describe("handoff state in the renderer", () => {
  it("READY selects the prepared session", async () => {
    await state().handleHandoffEvent(READY);
    expect(state().handoff).toEqual(READY);
    expect(state().selectedSessionId).toBe(PREPARED);
  });

  it("READY starts no audio and no capture", async () => {
    await state().handleHandoffEvent(READY);
    expect(desktop.audio.start).not.toHaveBeenCalled();
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
    expect(state().audio.state).toBe("idle");
    expect(state().capture.phase).toBe("idle");
  });

  it("redeeming and failed states change no selection", async () => {
    for (const phase of ["redeeming", "failed"] as const) {
      await state().handleHandoffEvent({ phase, session: null, failure: phase === "failed" ? "invalid" : null });
      expect(state().selectedSessionId).toBe(OTHER);
    }
  });

  it("READY signs the renderer in as the handoff user when it had no user (cold start)", async () => {
    useDesktopStore.setState({ user: null, selectedSessionId: null });
    await state().handleHandoffEvent(READY);
    expect(desktop.auth.getState).toHaveBeenCalled();
    expect(state().user).toEqual(user);
    expect(state().selectedSessionId).toBe(PREPARED);
  });

  it("READY with another account replaces the previous account's state", async () => {
    useDesktopStore.setState({
      user: { id: "someone-else", email: "x@example.com", created_at: "x" },
      sessions: [summary("old-session")],
      selectedSessionId: "old-session",
      history: [{ id: "q" } as never],
    });
    await state().handleHandoffEvent(READY);
    expect(state().user).toEqual(user);
    expect(state().history).toEqual([]);
    expect(state().selectedSessionId).toBe(PREPARED);
    expect(state().sessions.some((s) => s.id === "old-session")).toBe(false);
  });

  it("READY with no signed-in user in main changes nothing", async () => {
    desktop.auth.getState.mockResolvedValueOnce(ok({ user: null as never }));
    useDesktopStore.setState({ user: null, selectedSessionId: null });
    await state().handleHandoffEvent(READY);
    expect(state().selectedSessionId).toBeNull();
  });

  it("an unknown session is not selected", async () => {
    await state().handleHandoffEvent({ ...READY, session: { id: "00000000-2222-4333-8444-555555555555", title: null, company_name: "?" } });
    expect(state().selectedSessionId).toBe(OTHER);
  });

  it("dismiss clears the notice locally and in main", async () => {
    await state().handleHandoffEvent(READY);
    await state().dismissHandoff();
    expect(state().handoff).toEqual(IDLE_HANDOFF);
    expect(desktop.handoff.dismiss).toHaveBeenCalledTimes(1);
  });

  it("the state carries a session id and name only, never anything secret", () => {
    expect(Object.keys(READY).sort()).toEqual(["failure", "phase", "session"]);
    expect(Object.keys(READY.session ?? {}).sort()).toEqual(["company_name", "id", "title"]);
  });
});
