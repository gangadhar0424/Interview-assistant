/** Phase 11.5 renderer state: session creation and the connection-status fix (11.5-F). */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CreateSessionOptions, DesktopResult, SessionSummary } from "../src/shared/contracts";
import { defaultDraft } from "../src/shared/session-form";
import { useDesktopStore } from "../src/renderer/src/store";

const RESUME = "11111111-2222-4333-8444-555555555555";
const BUSY = "11111111-2222-4333-8444-666666666666";
const DOC = "aaaaaaaa-2222-4333-8444-555555555555";
const NEW_ID = "77777777-2222-4333-8444-555555555555";

const ok = <T,>(data: T): DesktopResult<T> => ({ ok: true, data });
const fail = (kind: "network" | "http" | "invalid_input", message: string, extra: Record<string, unknown> = {}): DesktopResult<never> => ({
  ok: false,
  error: { kind, message, ...extra } as never,
});
const NETWORK = fail("network", "Could not reach the backend. Check your connection and try again.", { code: "network_error" });

const loaded: CreateSessionOptions = {
  resumes: [
    { id: RESUME, filename: "cv.pdf", size_bytes: 1, created_at: "x", status: "ready" },
    { id: BUSY, filename: "new.pdf", size_bytes: 1, created_at: "x", status: "processing" },
  ],
  documents: [{ id: DOC, filename: "notes.pdf", size_bytes: 1, created_at: "x", status: "ready" }],
  options: {
    languages: ["en", "hi", "te"],
    question_types: ["auto", "behavioral"],
    formats: ["full_script", "bullets"],
    lengths: ["short", "balanced"],
    tones: ["simple", "formal"],
    providers: [{ id: "groq", label: "Groq" }, { id: "anthropic", label: "Claude" }],
    voice_providers: [{ id: "sarvam", label: "Sarvam" }],
  },
};

const summary = (id: string, status = "ready"): SessionSummary => ({
  id, type: "interview", title: null, company_name: "Acme", status,
  created_at: "x", updated_at: "x", resume: { id: RESUME, filename: "cv.pdf" }, document_count: 0,
});

function bridge() {
  return {
    app: { checkBackend: vi.fn(async () => ok({ status: "connected" as const })), copyText: vi.fn(async () => ok(undefined)) },
    auth: { logout: vi.fn(async () => ok(undefined)) },
    sessions: {
      list: vi.fn(async () => ok([summary("existing")])),
      questions: vi.fn(async () => ok([])),
      createOptions: vi.fn(async () => ok(loaded)),
      create: vi.fn(async () => ok(summary(NEW_ID))),
      startQuestion: vi.fn(async () => ok(undefined)),
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
    sessions: [],
    selectedSessionId: null,
    history: [],
    connection: "connected",
    error: null,
    errorKind: null,
    createOptions: null,
    createDraft: defaultDraft(null),
    createStatus: "idle",
    createError: null,
    createFieldErrors: {},
    liveAnswer: null,
    capture: { phase: "idle" },
    captureNotice: null,
  });
});

describe("refresh sessions fixes the stale connection state (11.5-F)", () => {
  async function goUnavailable() {
    desktop.sessions.list.mockResolvedValueOnce(NETWORK);
    await state().refreshSessions();
    expect(state().connection).toBe("unavailable");
    expect(state().error).toMatch(/Could not reach the backend/);
  }

  it("a successful refresh clears the stale network error and shows connected immediately", async () => {
    await goUnavailable();
    await state().refreshSessions(); // no timers, no header click
    expect(state().error).toBeNull();
    expect(state().errorKind).toBeNull();
    expect(state().connection).toBe("connected");
    expect(state().sessions).toHaveLength(1);
  });

  it("a failed refresh never claims the backend is connected", async () => {
    await goUnavailable();
    desktop.sessions.list.mockResolvedValueOnce(NETWORK);
    await state().refreshSessions();
    expect(state().connection).toBe("unavailable");
    expect(state().error).toMatch(/Could not reach the backend/);
    // and a connected indicator turns unavailable when the next refresh cannot reach the backend
    useDesktopStore.setState({ connection: "connected" });
    desktop.sessions.list.mockResolvedValueOnce(NETWORK);
    await state().refreshSessions();
    expect(state().connection).toBe("unavailable");
  });

  it("an HTTP error means the server answered: the indicator is not flipped to unavailable", async () => {
    desktop.sessions.list.mockResolvedValueOnce(fail("http", "The server could not complete the request.", { status: 500 }));
    await state().refreshSessions();
    expect(state().connection).toBe("connected");
    expect(state().error).toBe("The server could not complete the request.");
    expect(state().errorKind).toBe("other");
  });

  it("a successful refresh does not silently clear an unrelated error", async () => {
    useDesktopStore.setState({ error: "The answer could not be completed.", errorKind: "other" });
    await state().refreshSessions();
    expect(state().error).toBe("The answer could not be completed.");
    expect(state().connection).toBe("connected");
  });

  it("history loading also proves the backend is reachable", async () => {
    useDesktopStore.setState({ connection: "unavailable", error: "Could not reach the backend.", errorKind: "network", selectedSessionId: "existing" });
    await state().loadHistory();
    expect(state().connection).toBe("connected");
    expect(state().error).toBeNull();
  });

  it("the periodic/header health check keeps working in both directions", async () => {
    desktop.app.checkBackend.mockResolvedValueOnce(NETWORK as never);
    await state().checkBackend();
    expect(state().connection).toBe("unavailable");
    useDesktopStore.setState({ error: "Could not reach the backend.", errorKind: "network" });
    await state().checkBackend();
    expect(state().connection).toBe("connected");
    expect(state().error).toBeNull();
  });
});

describe("creating a session", () => {
  it("loads the options and keeps the web defaults", async () => {
    await state().loadCreateOptions();
    expect(state().createStatus).toBe("ready");
    expect(state().createOptions).toEqual(loaded);
    expect(state().createDraft).toMatchObject({ language: "en", questionType: "auto", format: "full_script", length: "balanced", tone: "simple" });
  });

  it("preselects a provider only when the server offers exactly one (D-57)", async () => {
    await state().loadCreateOptions();
    expect(state().createDraft).toMatchObject({ provider: "", sttProvider: "sarvam" });
  });

  it("keeps a chosen provider while it is offered and drops it when it is not", async () => {
    useDesktopStore.setState({ createDraft: { ...defaultDraft(null), provider: "anthropic", sttProvider: "gone" } });
    await state().loadCreateOptions();
    expect(state().createDraft).toMatchObject({ provider: "anthropic", sttProvider: "sarvam" });
    useDesktopStore.setState({ createDraft: { ...defaultDraft(null), provider: "openai" } });
    await state().loadCreateOptions();
    expect(state().createDraft.provider).toBe(""); // not offered here: the user must choose again
  });

  it("will not submit without both choices, and shows which one is missing", async () => {
    await state().loadCreateOptions();
    useDesktopStore.setState({ createDraft: { ...state().createDraft, company: "Acme", description: "Role", resumeId: RESUME } });
    expect(await state().submitCreate()).toBeNull();
    expect(desktop.sessions.create).not.toHaveBeenCalled();
    expect(state().createFieldErrors.provider).toBe("Choose a reasoning model.");
    expect(state().createFieldErrors.sttProvider).toBeUndefined(); // the single voice choice was preselected
  });

  it("maps a rejected provider selection onto the right field", async () => {
    await state().loadCreateOptions();
    useDesktopStore.setState({
      createDraft: { ...state().createDraft, company: "Acme", description: "Role", resumeId: RESUME, provider: "groq" },
    });
    desktop.sessions.create.mockResolvedValueOnce(
      fail("http", "x", { code: "validation_error", fieldErrors: [{ field: "preferences.stt_provider", message: "Choose one." }] }),
    );
    expect(await state().submitCreate()).toBeNull();
    expect(state().createFieldErrors.sttProvider).toBe("Choose one.");
  });

  it("exposes the no-ready-resume case as data", async () => {
    desktop.sessions.createOptions.mockResolvedValueOnce(ok({ ...loaded, resumes: [loaded.resumes[1] as never] }));
    await state().loadCreateOptions();
    expect(state().createOptions?.resumes.filter((r) => r.status === "ready")).toHaveLength(0);
  });

  it("a load failure shows a message and a network failure marks the backend unavailable", async () => {
    desktop.sessions.createOptions.mockResolvedValueOnce(NETWORK);
    await state().loadCreateOptions();
    expect(state().createStatus).toBe("idle");
    expect(state().createError).toMatch(/Could not reach/);
    expect(state().connection).toBe("unavailable");
  });

  it("drops a remembered choice that is no longer ready", async () => {
    useDesktopStore.setState({ createDraft: { ...defaultDraft(null), resumeId: BUSY, documentIds: [DOC, "gone"] } });
    await state().loadCreateOptions();
    expect(state().createDraft.resumeId).toBeNull();
    expect(state().createDraft.documentIds).toEqual([DOC]);
  });

  it("does not call the main process when the form is invalid, and shows field messages", async () => {
    await state().loadCreateOptions();
    expect(await state().submitCreate()).toBeNull();
    expect(desktop.sessions.create).not.toHaveBeenCalled();
    expect(Object.keys(state().createFieldErrors).sort()).toEqual(["company", "description", "provider", "resumeId"]);
    state().updateDraft({ company: "Acme" });
    expect(state().createFieldErrors.company).toBeUndefined(); // editing clears that field's message
  });

  it("will not select a resume that is still processing", async () => {
    await state().loadCreateOptions();
    state().updateDraft({ company: "Acme", description: "Role", resumeId: BUSY });
    expect(await state().submitCreate()).toBeNull();
    expect(state().createFieldErrors.resumeId).toBe("Choose a resume that is ready.");
    expect(desktop.sessions.create).not.toHaveBeenCalled();
  });

  it("creates, refreshes the list, selects the new session and clears the draft", async () => {
    await state().loadCreateOptions();
    state().updateDraft({ company: "Acme", description: "Backend role.", resumeId: RESUME, documentIds: [DOC], provider: "groq" });
    desktop.sessions.list.mockResolvedValueOnce(ok([summary(NEW_ID), summary("existing")]));
    const id = await state().submitCreate();
    expect(id).toBe(NEW_ID);
    expect(desktop.sessions.create).toHaveBeenCalledTimes(1);
    expect(desktop.sessions.create).toHaveBeenCalledWith(expect.objectContaining({ company: "Acme", resumeId: RESUME, documentIds: [DOC] }));
    expect(state().sessions.map((x) => x.id)).toEqual([NEW_ID, "existing"]);
    expect(state().selectedSessionId).toBe(NEW_ID);
    expect(state().createDraft.company).toBe("");
    expect(state().createDraft.description).toBe("");
    expect(state().createStatus).toBe("ready");
  });

  it("keeps the draft and explains a backend refusal, mapping field hints", async () => {
    await state().loadCreateOptions();
    state().updateDraft({ company: "Acme", description: "Backend role.", resumeId: RESUME, provider: "groq" });
    desktop.sessions.create.mockResolvedValueOnce(
      fail("http", "The request is invalid.", { status: 422, code: "validation_error", fieldErrors: [{ field: "job_description", message: "String should have at most 20000 characters" }] }),
    );
    expect(await state().submitCreate()).toBeNull();
    expect(state().createError).toBe("Some details were not accepted. Check the highlighted fields.");
    expect(state().createFieldErrors.description).toBe("String should have at most 20000 characters");
    expect(state().createDraft.description).toBe("Backend role."); // nothing the user typed is lost
    expect(state().createStatus).toBe("ready");
  });

  it.each([
    ["material_not_ready", "still being processed"],
    ["material_not_found", "no longer available"],
    ["too_many_documents", "more documents"],
  ])("maps %s to a plain message", async (code, text) => {
    await state().loadCreateOptions();
    state().updateDraft({ company: "Acme", description: "Role", resumeId: RESUME, provider: "groq" });
    desktop.sessions.create.mockResolvedValueOnce(fail("http", "backend text", { status: 422, code }));
    await state().submitCreate();
    expect(state().createError).toContain(text);
  });

  it("a network failure keeps the draft and marks the backend unavailable", async () => {
    await state().loadCreateOptions();
    state().updateDraft({ company: "Acme", description: "Role", resumeId: RESUME, provider: "groq" });
    desktop.sessions.create.mockResolvedValueOnce(NETWORK);
    expect(await state().submitCreate()).toBeNull();
    expect(state().connection).toBe("unavailable");
    expect(state().createDraft.company).toBe("Acme");
  });

  it("cancel, sign-out and session expiry drop the draft (memory only)", async () => {
    await state().loadCreateOptions();
    state().updateDraft({ company: "Acme", description: "Role", resumeId: RESUME, provider: "groq" });
    state().resetCreate();
    expect(state().createDraft.company).toBe("");

    state().updateDraft({ company: "Acme" });
    await state().logout();
    expect(state().createDraft.company).toBe("");
    expect(state().createOptions).toBeNull();

    state().updateDraft({ description: "Another" });
    state().expireSession();
    expect(state().createDraft.description).toBe("");
  });
});

describe("existing flows are unchanged", () => {
  it("a typed question still passes exactly three arguments", async () => {
    useDesktopStore.setState({ selectedSessionId: "existing" });
    vi.stubGlobal("crypto", { randomUUID: () => "44444444-2222-4333-8444-555555555555" });
    await state().ask("Tell me about yourself");
    expect(desktop.sessions.startQuestion.mock.calls[0]).toEqual(["44444444-2222-4333-8444-555555555555", "existing", "Tell me about yourself"]);
  });
});
