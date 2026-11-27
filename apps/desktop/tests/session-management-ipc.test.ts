/**
 * M-9: session management through the real IPC layer (`registerDesktopIpc`) with a fake ipcMain, a
 * fake window, fake native dialogs and a fake backend. No Electron runtime and no network.
 */
import { acknowledgedSettings } from "./ack-fixture";
import { DeepLinkInbox } from "../src/main/deep-link";
import { HANDOFF_LINK, HANDOFF_REDEEM_PATH, settleHandoff } from "./handoff-signin";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { registerDesktopIpc, type SessionFiles } from "../src/main/ipc";
import { defaultDraft, type SessionDraft } from "../src/shared/session-form";

const SID = "11111111-2222-4333-8444-aaaaaaaaaaaa";
const RESUME = "11111111-2222-4333-8444-555555555555";
const RENDERER = join(tmpdir(), "renderer-fixture", "index.html");
const TRUSTED = pathToFileURL(RENDERER).href;
const EXPORT_TEXT = "# Exported\nSECRET-ANSWER-TEXT-7731\n";

type Handler = (event: unknown, ...args: unknown[]) => Promise<any>;
type Call = { path: string; method: string; headers: Record<string, string>; body: unknown };

const user = { id: "00000000-0000-4000-8000-000000000001", email: "p@example.com", created_at: "2026-01-01T00:00:00Z" };
const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

const summary = (over: Record<string, unknown> = {}) => ({
  id: SID, type: "interview", title: null, company_name: "Acme", status: "ready",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  resume: { id: RESUME, filename: "cv.pdf" }, document_count: 0,
  last_activity_at: "2026-01-01T00:00:00Z", question_count: 0, activity_state: "active", ...over,
});

const offered = {
  languages: ["en"], question_types: ["auto"], formats: ["full_script"], lengths: ["balanced"], tones: ["simple"],
  providers: [{ id: "fake", label: "Fake" }], voice_providers: [{ id: "fake", label: "Fake" }],
};

function setup(files: Partial<SessionFiles> | null = {}) {
  const dir = mkdtempSync(join(tmpdir(), "session-mgmt-"));
  const handlers = new Map<string, Handler>();
  const calls: Call[] = [];
  const win = {
    isDestroyed: () => false, isVisible: () => true, isFocused: () => true, isMinimized: () => false,
    on: vi.fn(), removeListener: vi.fn(), setOpacity: vi.fn(), setAlwaysOnTop: vi.fn(), setMinimumSize: vi.fn(),
    setSize: vi.fn(), getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
    webContents: { id: 1, send: vi.fn(), on: vi.fn(), once: vi.fn(), removeListener: vi.fn() },
  };
  const backend = {
    respond: (_call: Call): Response | null => null,
    createStatus: 201,
  };
  const fakeFetch = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const call: Call = { path: url.pathname + url.search, method: init.method ?? "GET", headers: { ...(init.headers as Record<string, string>) }, body: init.body };
    calls.push(call);
    const custom = backend.respond(call);
    if (custom) return custom;
    if (url.pathname === HANDOFF_REDEEM_PATH) {
      return json({ access_token: "access-1", expires_in: 900, user, session: summary(), preferences: { language: "en" } }, 200, { "Set-Cookie": "refresh_token=refresh-1; HttpOnly; Path=/api/v1/auth" });
    }
    if (url.pathname === "/api/v1/auth/refresh") return json({ error: { code: "not_authenticated", message: "No." } }, 401);
    if (url.pathname === "/api/v1/resumes" || url.pathname === "/api/v1/documents") return json({ items: [] });
    if (url.pathname === "/api/v1/sessions" && call.method === "POST") return json(summary(), backend.createStatus);
    if (url.pathname === "/api/v1/sessions") return json({ items: [summary()], next_cursor: null });
    if (url.pathname === `/api/v1/sessions/${SID}/export`) return new Response(EXPORT_TEXT, { status: 200, headers: { "Content-Type": "text/markdown" } });
    if (url.pathname === `/api/v1/sessions/${SID}/end`) return json(summary({ status: "ended", activity_state: "completed" }));
    if (url.pathname === `/api/v1/sessions/${SID}/reopen`) return json(summary());
    if (url.pathname === `/api/v1/sessions/${SID}` && call.method === "PATCH") return json(summary({ title: JSON.parse(String(call.body)).title }));
    if (url.pathname === `/api/v1/sessions/${SID}` && call.method === "DELETE") return new Response(null, { status: 204 });
    return json({ error: { code: "not_found", message: "no" } }, 404);
  });
  vi.stubGlobal("fetch", fakeFetch);
  const sessionFiles: SessionFiles | undefined = files === null
    ? undefined
    : {
        confirmDelete: vi.fn(async () => true),
        chooseExportFile: vi.fn(async () => "C:\\Users\\me\\Documents\\session-aaaaaaaa.md"),
        writeFile: vi.fn(async () => undefined),
        ...files,
      };
  const inbox = new DeepLinkInbox();
  const dispose = registerDesktopIpc({
    ipcMain: { handle: (channel: string, fn: Handler) => handlers.set(channel, fn), removeHandler: (channel: string) => handlers.delete(channel), on: () => undefined, removeListener: () => undefined } as never,
    window: win as never,
    settings: acknowledgedSettings(dir, user.id),
    safeStorage: { isEncryptionAvailable: () => true, encryptString: (v: string) => Buffer.from(v), decryptString: (v: Buffer) => v.toString() },
    secureTokenPath: join(dir, "refresh-token.bin"),
    rendererHtmlPath: RENDERER,
    devServerUrl: undefined,
    captureHost: { listDisplays: () => [], captureDisplay: async () => null, makePreview: () => ({ dataUrl: "", width: 0, height: 0 }), setIndicator: vi.fn() },
    confirmScreenshotConsent: async () => true,
    audioHost: { setIndicator: vi.fn() },
    audioGate: { read: () => ({ state: "idle" as const, source: null }) },
    confirmAudioConsent: async () => true,
    confirmAcceptableUse: async () => true,
    deepLinks: inbox,
    confirmHandoffSwitch: async () => true,
    confirmHandoffAccountSwitch: async () => true,
    showWindow: () => undefined,
    ...(sessionFiles ? { sessionFiles } : {}),
  });
  const trusted = { senderFrame: { url: TRUSTED }, sender: { id: 1 } };
  const untrusted = { senderFrame: { url: "https://evil.example/" }, sender: { id: 2 } };
  const call = async (channel: string, ...args: unknown[]) => (handlers.get(channel) as Handler)(trusted, ...args);
  return {
    handlers, calls, backend, untrusted, call, sessionFiles,
    apiCalls: () => calls.filter((c) => c.path.startsWith("/api/v1/sessions") && !c.path.includes("handoff") && c.path !== "/api/v1/sessions?limit=100"),
    login: async () => {
      inbox.deliver(HANDOFF_LINK);
      await settleHandoff();
      expect((await call("handoff:get-state")).data.phase).toBe("ready");
      calls.length = 0;
    },
    cleanup: () => { dispose(); rmSync(dir, { recursive: true, force: true }); },
  };
}

let s: ReturnType<typeof setup>;
const logSpies: Array<ReturnType<typeof vi.spyOn>> = [];
beforeEach(() => {
  for (const m of ["log", "info", "warn", "error", "debug"] as const) logSpies.push(vi.spyOn(console, m).mockImplementation(() => undefined));
  s = setup();
});
afterEach(() => {
  s.cleanup();
  vi.unstubAllGlobals();
  logSpies.splice(0).forEach((spy) => spy.mockRestore());
});

const CHANNELS: Array<[string, unknown[]]> = [
  ["sessions:rename", [SID, "New"]],
  ["sessions:end", [SID]],
  ["sessions:reopen", [SID]],
  ["sessions:delete", [SID]],
  ["sessions:export", [SID, "markdown"]],
];

describe("channel security", () => {
  it.each(CHANNELS)("%s refuses an untrusted sender with no network call", async (channel, args) => {
    await s.login();
    const result = await (s.handlers.get(channel) as Handler)(s.untrusted, ...args);
    expect(result).toMatchObject({ ok: false, error: { message: "Untrusted IPC sender." } });
    expect(s.calls).toEqual([]);
    expect(s.sessionFiles?.confirmDelete).not.toHaveBeenCalled();
    expect(s.sessionFiles?.chooseExportFile).not.toHaveBeenCalled();
  });

  it.each(CHANNELS)("%s requires a signed-in user", async (channel, args) => {
    expect(await s.call(channel, ...args)).toMatchObject({ ok: false, error: { status: 401 } });
    expect(s.apiCalls()).toEqual([]);
    expect(s.sessionFiles?.writeFile).not.toHaveBeenCalled();
  });

  it.each(CHANNELS)("%s refuses a malformed session id before anything else", async (channel, args) => {
    await s.login();
    for (const bad of ["../x", "", 42, null, `${SID}/export`]) {
      const result = await s.call(channel, bad, ...args.slice(1));
      expect(result.ok).toBe(false);
    }
    expect(s.calls).toEqual([]);
    expect(s.sessionFiles?.confirmDelete).not.toHaveBeenCalled();
    expect(s.sessionFiles?.chooseExportFile).not.toHaveBeenCalled();
  });

  it("exposes no generic request channel", () => {
    expect([...s.handlers.keys()].some((n) => /api|request|fetch|invoke/i.test(n))).toBe(false);
  });
});

describe("rename", () => {
  it("sends only the trimmed title", async () => {
    await s.login();
    const result = await s.call("sessions:rename", SID, "  Round two  ");
    expect(result).toMatchObject({ ok: true, data: { title: "Round two" } });
    const patch = s.calls.find((c) => c.method === "PATCH");
    expect(patch?.path).toBe(`/api/v1/sessions/${SID}`);
    expect(JSON.parse(String(patch?.body))).toEqual({ title: "Round two" });
  });

  it("refuses a non-string or over-long title without a request", async () => {
    await s.login();
    expect((await s.call("sessions:rename", SID, 5)).ok).toBe(false);
    expect((await s.call("sessions:rename", SID, "x".repeat(201))).ok).toBe(false);
    expect(s.calls).toEqual([]);
  });
});

describe("complete and reopen", () => {
  it("posts to end and reopen with no body", async () => {
    await s.login();
    expect(await s.call("sessions:end", SID)).toMatchObject({ ok: true, data: { status: "ended" } });
    expect(await s.call("sessions:reopen", SID)).toMatchObject({ ok: true, data: { status: "ready" } });
    expect(s.calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      `POST /api/v1/sessions/${SID}/end`,
      `POST /api/v1/sessions/${SID}/reopen`,
    ]);
    expect(s.calls.every((c) => c.body === undefined)).toBe(true);
  });

  it("passes the server's refusal on as a safe error", async () => {
    await s.login();
    s.backend.respond = (c) =>
      c.path.endsWith("/end") ? json({ error: { code: "session_not_transitionable", message: "This session cannot be completed." } }, 409) : null;
    expect(await s.call("sessions:end", SID)).toMatchObject({
      ok: false,
      error: { status: 409, code: "session_not_transitionable" },
    });
  });
});

describe("delete", () => {
  it("is refused when the host has no native confirmation", async () => {
    s.cleanup();
    s = setup(null);
    await s.login();
    expect(await s.call("sessions:delete", SID)).toMatchObject({ ok: false });
    expect(s.calls).toEqual([]);
  });

  it("does nothing when the user declines", async () => {
    s.cleanup();
    s = setup({ confirmDelete: vi.fn(async () => false) });
    await s.login();
    expect(await s.call("sessions:delete", SID)).toEqual({ ok: true, data: { deleted: false } });
    expect(s.calls).toEqual([]);
  });

  it("deletes only after the native confirmation", async () => {
    await s.login();
    expect(await s.call("sessions:delete", SID)).toEqual({ ok: true, data: { deleted: true } });
    expect(s.sessionFiles?.confirmDelete).toHaveBeenCalledTimes(1);
    expect(s.calls.map((c) => `${c.method} ${c.path}`)).toEqual([`DELETE /api/v1/sessions/${SID}`]);
  });
});

describe("export", () => {
  it("is refused when the host has no file dialogs", async () => {
    s.cleanup();
    s = setup(null);
    await s.login();
    expect((await s.call("sessions:export", SID, "markdown")).ok).toBe(false);
    expect(s.calls).toEqual([]);
  });

  it("refuses an unknown format", async () => {
    await s.login();
    for (const bad of ["json", "", undefined, 1]) expect((await s.call("sessions:export", SID, bad)).ok).toBe(false);
    expect(s.calls).toEqual([]);
    expect(s.sessionFiles?.chooseExportFile).not.toHaveBeenCalled();
  });

  it("asks where to save first and fetches nothing when the user cancels", async () => {
    s.cleanup();
    s = setup({ chooseExportFile: vi.fn(async () => null) });
    await s.login();
    expect(await s.call("sessions:export", SID, "pdf")).toEqual({ ok: true, data: { saved: false, fileName: null } });
    expect(s.calls).toEqual([]);
    expect(s.sessionFiles?.writeFile).not.toHaveBeenCalled();
  });

  it("writes the server's text to the chosen path and returns only the file name", async () => {
    await s.login();
    const result = await s.call("sessions:export", SID, "markdown");
    expect(result).toEqual({ ok: true, data: { saved: true, fileName: "session-aaaaaaaa.md" } });
    expect(JSON.stringify(result)).not.toContain("SECRET-ANSWER-TEXT-7731"); // never to the renderer
    expect(s.sessionFiles?.chooseExportFile).toHaveBeenCalledWith("session-11111111.md", "markdown");
    expect(s.sessionFiles?.writeFile).toHaveBeenCalledWith("C:\\Users\\me\\Documents\\session-aaaaaaaa.md", EXPORT_TEXT);
    const fetched = s.calls.find((c) => c.path.includes("/export"));
    expect(fetched?.path).toBe(`/api/v1/sessions/${SID}/export?format=markdown`);
    expect(fetched?.headers.Authorization).toBe("Bearer access-1");
  });

  it("writes a PDF as bytes, not text, and returns only the file name", async () => {
    await s.login();
    const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37, 0x0a, 0x00, 0xff]);
    s.backend.respond = (c) =>
      c.path.includes("/export")
        ? new Response(bytes, { status: 200, headers: { "Content-Type": "application/pdf" } })
        : null;
    const result = await s.call("sessions:export", SID, "pdf");
    expect(result).toEqual({ ok: true, data: { saved: true, fileName: "session-aaaaaaaa.md" } });
    expect(s.sessionFiles?.chooseExportFile).toHaveBeenCalledWith("session-11111111.pdf", "pdf");
    const fetched = s.calls.find((c) => c.path.includes("/export"));
    expect(fetched?.path).toBe(`/api/v1/sessions/${SID}/export?format=pdf`);
    const written = vi.mocked(s.sessionFiles!.writeFile).mock.calls[0]?.[1];
    expect(written).toBeInstanceOf(Uint8Array);
    expect(Array.from(written as Uint8Array)).toEqual(Array.from(bytes));
  });

  it("writes nothing when the export request fails", async () => {
    await s.login();
    s.backend.respond = (c) =>
      c.path.includes("/export") ? json({ error: { code: "session_not_found", message: "Session not found." } }, 404) : null;
    expect(await s.call("sessions:export", SID, "pdf")).toMatchObject({ ok: false, error: { status: 404 } });
    expect(s.sessionFiles?.writeFile).not.toHaveBeenCalled();
  });

  it("a network failure writes nothing and is reported", async () => {
    await s.login();
    s.backend.respond = (c) => {
      if (c.path.includes("/export")) throw new TypeError("fetch failed");
      return null;
    };
    expect((await s.call("sessions:export", SID, "pdf")).ok).toBe(false);
    expect(s.sessionFiles?.writeFile).not.toHaveBeenCalled();
  });
});

describe("duplicate session creation", () => {
  const draft = (over: Partial<SessionDraft> = {}): SessionDraft => ({
    ...defaultDraft(offered), company: "Acme", description: "Backend engineer.", resumeId: RESUME, ...over,
  });
  const keys = () => s.calls.filter((c) => c.path === "/api/v1/sessions" && c.method === "POST").map((c) => c.headers["Idempotency-Key"]);

  it("sends an Idempotency-Key and reuses it when the same draft is retried after a failure", async () => {
    await s.login();
    s.backend.respond = (c) => (c.method === "POST" && c.path === "/api/v1/sessions" ? (() => { throw new TypeError("fetch failed"); })() : null);
    expect((await s.call("sessions:create", draft())).ok).toBe(false);
    s.backend.respond = () => null;
    expect((await s.call("sessions:create", draft())).ok).toBe(true);
    const [first, second] = keys();
    expect(first).toMatch(/^[0-9a-f-]{36}$/);
    expect(second).toBe(first);
  });

  it("uses a new key for a changed draft and for the next session after a success", async () => {
    await s.login();
    s.backend.respond = (c) => (c.method === "POST" && c.path === "/api/v1/sessions" ? (() => { throw new TypeError("fetch failed"); })() : null);
    await s.call("sessions:create", draft());
    await s.call("sessions:create", draft({ company: "Other" }));
    s.backend.respond = () => null;
    await s.call("sessions:create", draft({ company: "Other" }));
    await s.call("sessions:create", draft({ company: "Other" }));
    const [a, b, c, d] = keys();
    expect(new Set([a, b]).size).toBe(2);
    expect(c).toBe(b);
    expect(d).not.toBe(c);
  });
});
