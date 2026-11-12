/**
 * Phase 11.5: desktop session creation through the real IPC layer (`registerDesktopIpc`) with a
 * fake ipcMain, a fake window and a fake backend. No Electron runtime and no network.
 */
import { DeepLinkInbox } from "../src/main/deep-link";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { registerDesktopIpc } from "../src/main/ipc";
import { SettingsStore } from "../src/main/settings-store";
import { defaultDraft, SESSION_LIMITS, type SessionDraft } from "../src/shared/session-form";

const RESUME_READY = "11111111-2222-4333-8444-555555555555";
const RESUME_BUSY = "11111111-2222-4333-8444-666666666666";
const DOC_READY = "aaaaaaaa-2222-4333-8444-555555555555";
const DOC_FAILED = "aaaaaaaa-2222-4333-8444-666666666666";
const NEW_SESSION = "77777777-2222-4333-8444-555555555555";
const RENDERER = join(tmpdir(), "renderer-fixture", "index.html");
const TRUSTED = pathToFileURL(RENDERER).href;
const SENTINEL = "JOB-DESCRIPTION-SENTINEL-4412";
const EXTRA_SENTINEL = "EXTRA-INSTRUCTIONS-SENTINEL-8820";

type Handler = (event: unknown, ...args: unknown[]) => Promise<any>;
type Call = { path: string; method: string; headers: Record<string, string>; body: unknown };

const user = { id: "00000000-0000-4000-8000-000000000001", email: "p@example.com", created_at: "2026-01-01T00:00:00Z" };

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

const file = (id: string, name: string, ingestion: string, indexing: string) => ({
  id,
  filename: name,
  content_type: "application/pdf",
  size_bytes: 1234,
  created_at: "2026-01-01T00:00:00Z",
  ingestion: { status: ingestion, error_code: null, error_message: null },
  indexing: { status: indexing, error_code: null, error_message: null },
});

const options = {
  providers: [{ id: "fake", models: [{ id: "fake-echo", label: "Fake", capabilities: ["streaming"], languages: ["en"] }] }],
  languages: ["en", "hi", "te"],
  question_types: ["auto", "behavioral", "coding"],
  formats: ["full_script", "script_bullets", "bullets"],
  lengths: ["short", "balanced", "long"],
  tones: ["simple", "formal"],
};

function setup() {
  const dir = mkdtempSync(join(tmpdir(), "session-create-"));
  const handlers = new Map<string, Handler>();
  const sent: Array<{ channel: string; payload: unknown }> = [];
  const calls: Call[] = [];
  const win = {
    isDestroyed: () => false,
    isVisible: () => true,
    isFocused: () => true,
    isMinimized: () => false,
    on: vi.fn(),
    removeListener: vi.fn(),
    setOpacity: vi.fn(),
    setAlwaysOnTop: vi.fn(),
    setMinimumSize: vi.fn(),
    setSize: vi.fn(),
    getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
    webContents: { id: 1, send: (channel: string, payload: unknown) => sent.push({ channel, payload }), on: vi.fn(), once: vi.fn(), removeListener: vi.fn() },
  };
  const backend = {
    resumes: [file(RESUME_READY, "cv.pdf", "completed", "completed"), file(RESUME_BUSY, "new-cv.pdf", "completed", "processing")],
    documents: [file(DOC_READY, "notes.pdf", "completed", "completed"), file(DOC_FAILED, "bad.pdf", "failed", "pending")],
    create: (_body: Record<string, unknown>): Response =>
      json(
        {
          id: NEW_SESSION, type: "interview", title: null, company_name: "Acme", status: "ready",
          created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
          resume: { id: RESUME_READY, filename: "cv.pdf" }, document_count: 0,
          job_description: SENTINEL, // the detail contains the description; it must not travel on
        },
        201,
      ),
    refreshOk: false,
  };
  const fakeFetch = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const call: Call = { path: url.pathname, method: init.method ?? "GET", headers: { ...(init.headers as Record<string, string>) }, body: init.body };
    calls.push(call);
    if (call.path === "/api/v1/auth/login") {
      return json({ access_token: "access-1", expires_in: 900, user }, 200, { "Set-Cookie": "refresh_token=refresh-1; HttpOnly; Path=/api/v1/auth" });
    }
    if (call.path === "/api/v1/auth/refresh") {
      return backend.refreshOk
        ? json({ access_token: "access-2", expires_in: 900, user }, 200, { "Set-Cookie": "refresh_token=refresh-2; HttpOnly; Path=/api/v1/auth" })
        : json({ error: { code: "not_authenticated", message: "No." } }, 401);
    }
    if (call.path === "/api/v1/resumes") return json({ items: backend.resumes });
    if (call.path === "/api/v1/documents") return json({ items: backend.documents });
    if (call.path === "/api/v1/ai/options") return json(options);
    if (call.path === "/api/v1/sessions" && call.method === "POST") return backend.create(JSON.parse(String(call.body)));
    if (call.path === "/api/v1/sessions") return json({ items: [], next_cursor: null });
    return json({ error: { code: "not_found", message: "no" } }, 404);
  });
  vi.stubGlobal("fetch", fakeFetch);
  const dispose = registerDesktopIpc({
    ipcMain: { handle: (channel: string, fn: Handler) => handlers.set(channel, fn), removeHandler: (channel: string) => handlers.delete(channel), on: () => undefined, removeListener: () => undefined } as never,
    window: win as never,
    settings: new SettingsStore(join(dir, "settings.json")),
    safeStorage: { isEncryptionAvailable: () => true, encryptString: (v: string) => Buffer.from(v), decryptString: (v: Buffer) => v.toString() },
    secureTokenPath: join(dir, "refresh-token.bin"),
    rendererHtmlPath: RENDERER,
    devServerUrl: undefined,
    captureHost: { listDisplays: () => [], captureDisplay: async () => null, makePreview: () => ({ dataUrl: "", width: 0, height: 0 }), setIndicator: vi.fn() },
    confirmScreenshotConsent: async () => true,
    audioHost: { setIndicator: vi.fn() },
    audioGate: { read: () => ({ state: "idle" as const, source: null }) },
    confirmAudioConsent: async () => true,
    deepLinks: new DeepLinkInbox(),
    confirmHandoffSwitch: async () => true,
    confirmHandoffAccountSwitch: async () => true,
    showWindow: () => undefined,
  });
  const trusted = { senderFrame: { url: TRUSTED }, sender: { id: 1 } };
  const untrusted = { senderFrame: { url: "https://evil.example/" }, sender: { id: 2 } };
  const call = async (channel: string, ...args: unknown[]) => (handlers.get(channel) as Handler)(trusted, ...args);
  return {
    handlers, win, sent, calls, backend, trusted, untrusted, call,
    posts: () => calls.filter((c) => c.path === "/api/v1/sessions" && c.method === "POST"),
    login: async () => expect((await call("auth:login", "p@example.com", "long-password-1")).ok).toBe(true),
    cleanup: () => { dispose(); rmSync(dir, { recursive: true, force: true }); },
  };
}

type Setup = ReturnType<typeof setup>;
let s: Setup;
const logSpies: Array<ReturnType<typeof vi.spyOn>> = [];

beforeEach(() => {
  for (const method of ["log", "info", "warn", "error", "debug"] as const) logSpies.push(vi.spyOn(console, method).mockImplementation(() => undefined));
  s = setup();
});
afterEach(() => {
  s.cleanup();
  vi.unstubAllGlobals();
  logSpies.splice(0).forEach((spy) => spy.mockRestore());
});

function draft(over: Partial<SessionDraft> = {}): SessionDraft {
  return { ...defaultDraft(options), company: "Acme", description: "Backend engineer, Python.", resumeId: RESUME_READY, ...over };
}

describe("sender validation", () => {
  it.each([
    ["sessions:create-options", []],
    ["sessions:create", [draft()]],
  ])("%s refuses an untrusted sender and causes no network call", async (channel, args) => {
    await s.login();
    s.calls.length = 0;
    const result = await (s.handlers.get(channel) as Handler)(s.untrusted, ...args);
    expect(result).toMatchObject({ ok: false, error: { message: "Untrusted IPC sender." } });
    expect(s.calls).toEqual([]);
  });

  it("adds exactly two channels and no generic API channel", () => {
    expect([...s.handlers.keys()].filter((n) => n.startsWith("sessions:create")).sort()).toEqual(["sessions:create", "sessions:create-options"]);
    expect([...s.handlers.keys()].some((n) => /api|request|fetch|invoke/i.test(n))).toBe(false);
  });

  it("requires a signed-in user", async () => {
    expect(await s.call("sessions:create-options")).toMatchObject({ ok: false, error: { status: 401 } });
    expect(await s.call("sessions:create", draft())).toMatchObject({ ok: false, error: { status: 401 } });
    expect(s.posts()).toEqual([]);
  });
});

describe("loading the form options", () => {
  it("returns ready/processing/failed files and the answer-style values only", async () => {
    await s.login();
    const result = await s.call("sessions:create-options");
    expect(result.ok).toBe(true);
    expect(result.data.resumes).toEqual([
      expect.objectContaining({ id: RESUME_READY, filename: "cv.pdf", status: "ready" }),
      expect.objectContaining({ id: RESUME_BUSY, filename: "new-cv.pdf", status: "processing" }),
    ]);
    expect(result.data.documents.map((d: { status: string }) => d.status)).toEqual(["ready", "failed"]);
    expect(Object.keys(result.data.options).sort()).toEqual(["formats", "languages", "lengths", "question_types", "tones"]);
    const serialized = JSON.stringify(result);
    expect(serialized).not.toMatch(/access-1|refresh-1|providers|content_type|error_message/);
    const reads = s.calls.filter((c) => c.path.startsWith("/api/v1/") && !c.path.includes("auth"));
    expect(reads.every((c) => c.headers.Authorization === "Bearer access-1")).toBe(true);
    expect(s.calls.find((c) => c.path === "/api/v1/resumes")).toBeDefined();
  });

  it("the no-ready-resume case is returned as data so the UI can guide the user", async () => {
    await s.login();
    s.backend.resumes = [];
    const none = await s.call("sessions:create-options");
    expect(none).toMatchObject({ ok: true, data: { resumes: [] } });
    s.backend.resumes = [file(RESUME_BUSY, "new-cv.pdf", "pending", "pending")];
    const busy = await s.call("sessions:create-options");
    expect(busy.data.resumes.every((r: { status: string }) => r.status !== "ready")).toBe(true);
  });
});

describe("creating a session", () => {
  it("sends exactly the backend contract and returns only the summary", async () => {
    await s.login();
    const result = await s.call("sessions:create", draft({ title: "Backend Engineer", documentIds: [DOC_READY], extra: EXTRA_SENTINEL, description: SENTINEL }));
    expect(result.ok).toBe(true);
    expect(result.data).toMatchObject({ id: NEW_SESSION, status: "ready", company_name: "Acme" });
    expect(JSON.stringify(result)).not.toContain(SENTINEL); // the job description is not sent back to the renderer
    const [post] = s.posts();
    expect(post?.headers).toMatchObject({ Authorization: "Bearer access-1", "Content-Type": "application/json" });
    expect(JSON.parse(String(post?.body))).toEqual({
      type: "interview",
      company_name: "Acme",
      title: "Backend Engineer",
      job_description: SENTINEL,
      resume_id: RESUME_READY,
      document_ids: [DOC_READY],
      preferences: { language: "en", question_type: "auto", answer_format: "full_script", answer_length: "balanced", answer_tone: "simple", extra_instructions: EXTRA_SENTINEL },
    });
  });

  it.each([
    ["blank company", { company: "  " }],
    ["blank description", { description: "\n" }],
    ["no resume", { resumeId: null }],
    ["resume id not a uuid", { resumeId: "abc" }],
    ["description over the limit", { description: "x".repeat(SESSION_LIMITS.description + 1) }],
    ["company over the limit", { company: "c".repeat(SESSION_LIMITS.name + 1) }],
    ["title over the limit", { title: "t".repeat(SESSION_LIMITS.name + 1) }],
    ["extra instructions over the limit", { extra: "e".repeat(SESSION_LIMITS.extra + 1) }],
    ["too many documents", { documentIds: Array.from({ length: 11 }, (_, i) => `00000000-2222-4333-8444-${String(i).padStart(12, "0")}`) }],
    ["duplicate documents", { documentIds: [DOC_READY, DOC_READY] }],
    ["bad document id", { documentIds: ["nope"] }],
    ["malformed enum value", { language: "../etc/passwd" }],
  ])("refuses %s before any request is made", async (_name, over) => {
    await s.login();
    s.calls.length = 0;
    const result = await s.call("sessions:create", draft(over as Partial<SessionDraft>));
    expect(result).toMatchObject({ ok: false, error: { kind: "invalid_input" } });
    expect(s.calls).toEqual([]);
  });

  it.each([
    ["an unknown key", { ...draft(), user_id: "someone" }],
    ["a status field", { ...draft(), status: "ready" }],
    ["wrong types", { ...draft(), company: 5 }],
    ["not an object", "hello"],
    ["null", null],
    ["an array", []],
  ])("refuses a payload with %s", async (_name, payload) => {
    await s.login();
    s.calls.length = 0;
    expect(await s.call("sessions:create", payload)).toMatchObject({ ok: false });
    expect(s.calls).toEqual([]);
  });
});

describe("backend errors are mapped to safe results", () => {
  const cases: Array<[string, number, string]> = [
    ["not ready", 422, "material_not_ready"],
    ["not found", 404, "material_not_found"],
    ["duplicate", 422, "duplicate_material"],
    ["too many", 422, "too_many_documents"],
    ["provider", 422, "invalid_provider_selection"],
  ];
  it.each(cases)("preserves the code for %s", async (_name, status, code) => {
    await s.login();
    s.backend.create = () => json({ error: { code, message: `fixed ${code}` } }, status);
    expect(await s.call("sessions:create", draft())).toMatchObject({ ok: false, error: { kind: "http", status, code, message: `fixed ${code}` } });
  });

  it("passes the backend's field hints for validation errors", async () => {
    await s.login();
    s.backend.create = () =>
      json({ error: { code: "validation_error", message: "The request is invalid.", details: [{ field: "company_name", message: "String should have at least 1 character" }, { field: "x", message: 5 }] } }, 422);
    const result = await s.call("sessions:create", draft());
    expect(result.error.fieldErrors).toEqual([{ field: "company_name", message: "String should have at least 1 character" }]);
  });

  it("keeps the retry hint of a rate limit", async () => {
    await s.login();
    s.backend.create = () => json({ error: { code: "rate_limited", message: "Slow down." } }, 429, { "Retry-After": "9" });
    expect(await s.call("sessions:create", draft())).toMatchObject({ ok: false, error: { status: 429, retryAfterSeconds: 9 } });
  });

  it("reports an unreachable backend with the generic message and nothing else", async () => {
    await s.login();
    s.backend.create = () => {
      throw new TypeError("connect ECONNREFUSED 10.1.2.3:8000");
    };
    const result = await s.call("sessions:create", draft());
    expect(result).toMatchObject({ ok: false, error: { kind: "network", code: "network_error" } });
    expect(JSON.stringify(result)).not.toContain("10.1.2.3");
  });

  it("a 401 refreshes once and then reports the expiry to the window", async () => {
    await s.login();
    s.backend.create = () => json({ error: { code: "not_authenticated", message: "No." } }, 401);
    s.backend.refreshOk = true;
    expect(await s.call("sessions:create", draft())).toMatchObject({ ok: false, error: { status: 401 } });
    expect(s.calls.filter((c) => c.path === "/api/v1/auth/refresh")).toHaveLength(1);
    expect(s.posts()).toHaveLength(2); // one retry with the rotated token
    expect(s.sent.some((m) => m.channel === "auth:expired")).toBe(true);
  });
});

describe("no sensitive content or credentials leave the main process", () => {
  it("never logs form content, and the renderer only ever sees safe payloads", async () => {
    await s.login();
    await s.call("sessions:create-options");
    await s.call("sessions:create", draft({ description: SENTINEL, extra: EXTRA_SENTINEL }));
    s.backend.create = () => json({ error: { code: "validation_error", message: "The request is invalid." } }, 422);
    await s.call("sessions:create", draft({ description: SENTINEL, extra: EXTRA_SENTINEL }));
    const logged = JSON.stringify(logSpies.flatMap((spy) => spy.mock.calls));
    for (const secret of [SENTINEL, EXTRA_SENTINEL, "access-1", "refresh-1"]) expect(logged).not.toContain(secret);
    const toRenderer = JSON.stringify(s.sent);
    for (const secret of [SENTINEL, EXTRA_SENTINEL, "access-1", "refresh-1"]) expect(toRenderer).not.toContain(secret);
  });

  it("the typed question path is untouched by this feature", async () => {
    await s.login();
    expect(s.handlers.has("sessions:stream-start")).toBe(true);
    expect(s.handlers.has("capture:start")).toBe(true);
  });
});
