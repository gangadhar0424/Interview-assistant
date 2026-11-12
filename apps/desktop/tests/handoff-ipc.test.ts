/**
 * Web -> desktop handoff (D-55): the real IPC layer driven with a fake ipcMain/window/backend.
 * Proves cold-start and already-running delivery, redemption only as the desktop's own signed-in
 * user, uniform failures, the native confirmation before switching away from a running capture,
 * that the renderer never receives the code, and that READY never starts any capture.
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DeepLinkInbox } from "../src/main/deep-link";
import { HANDOFF_CODE_TTL_MS } from "../src/main/handoff";
import { registerDesktopIpc } from "../src/main/ipc";
import type { AudioGateState } from "../src/main/media-permissions";
import { SettingsStore } from "../src/main/settings-store";
import type { HandoffState } from "../src/shared/contracts";

const SESSION = "11111111-2222-4333-8444-555555555555";
const CODE = "Ab3-_".repeat(8) + "xyz";
const LINK = `interviewassistant://start?h=${CODE}`;
const RENDERER = join(tmpdir(), "renderer-fixture", "index.html");
const TRUSTED = pathToFileURL(RENDERER).href;

type Handler = (event: unknown, ...args: unknown[]) => Promise<any>;
type FetchCall = { path: string; method: string; headers: Record<string, string>; body: unknown };

const user = { id: "00000000-0000-4000-8000-000000000001", email: "p@example.com", created_at: "2026-01-01T00:00:00Z" };
const sessionRow = (id: string, status: string) => ({
  id, type: "interview", title: "Backend", company_name: "Acme", status,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  resume: { id: null, filename: "cv.docx" }, document_count: 0,
});
const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const notFound = () => json({ error: { code: "handoff_invalid", message: "SERVER-SECRET-TEXT" } }, 404);
const redeemed = (status = "ready") =>
  json({ session: sessionRow(SESSION, status), preferences: { language: "en" } });

function setup(redeem: (call: FetchCall) => Response | Promise<Response> = () => redeemed()) {
  const dir = mkdtempSync(join(tmpdir(), "handoff-ipc-"));
  const handlers = new Map<string, Handler>();
  const sent: Array<{ channel: string; payload: unknown }> = [];
  const windowListeners = new Map<string, Array<() => void>>();
  const win = {
    isDestroyed: () => false,
    isVisible: () => true,
    isFocused: () => true,
    isMinimized: () => false,
    minimize: vi.fn(), hide: vi.fn(), setOpacity: vi.fn(), setAlwaysOnTop: vi.fn(), setMinimumSize: vi.fn(), setSize: vi.fn(),
    getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
    on: (name: string, fn: () => void) => windowListeners.set(name, [...(windowListeners.get(name) ?? []), fn]),
    removeListener: vi.fn(),
    webContents: { id: 1, send: (channel: string, payload: unknown) => sent.push({ channel, payload }), on: vi.fn(), once: vi.fn(), removeListener: vi.fn() },
  };
  const calls: FetchCall[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const call: FetchCall = {
      path: url.pathname, method: init.method ?? "GET",
      headers: { ...(init.headers as Record<string, string>) }, body: init.body,
    };
    calls.push(call);
    if (call.path === "/api/v1/auth/login") {
      return json({ access_token: "access-1", expires_in: 900, user }, 200, { "Set-Cookie": "refresh_token=refresh-1; HttpOnly; Path=/api/v1/auth" });
    }
    if (call.path === "/api/v1/auth/logout") return json({}, 200);
    if (call.path === "/api/v1/sessions") return json({ items: [sessionRow(SESSION, "ready")], next_cursor: null });
    if (call.path === "/api/v1/sessions/handoff/redeem") return redeem(call);
    return json({ error: { code: "not_found", message: "no" } }, 404);
  }));

  const inbox = new DeepLinkInbox();
  const gate = { read: (): AudioGateState => ({ state: "idle", source: null }) };
  const indicator = vi.fn();
  const confirmSwitch = vi.fn(async () => true);
  const showWindow = vi.fn();
  const attach = () =>
    registerDesktopIpc({
      ipcMain: {
        handle: (channel: string, fn: Handler) => handlers.set(channel, fn),
        removeHandler: (channel: string) => handlers.delete(channel),
        on: vi.fn(),
        removeListener: vi.fn(),
      } as never,
      window: win as never,
      settings: new SettingsStore(join(dir, "settings.json")),
      safeStorage: { isEncryptionAvailable: () => true, encryptString: (v: string) => Buffer.from(v), decryptString: (v: Buffer) => v.toString() },
      secureTokenPath: join(dir, "refresh-token.bin"),
      rendererHtmlPath: RENDERER,
      devServerUrl: undefined,
      captureHost: { listDisplays: () => [], captureDisplay: async () => null, makePreview: () => ({ dataUrl: "", width: 0, height: 0 }), setIndicator: vi.fn() },
      confirmScreenshotConsent: async () => true,
      audioHost: { setIndicator: indicator },
      audioGate: gate,
      confirmAudioConsent: async () => true,
      deepLinks: inbox,
      confirmHandoffSwitch: confirmSwitch,
      showWindow,
    });
  const disposers = [attach()];
  const trusted = { senderFrame: { url: TRUSTED }, sender: { id: 1 } };
  const untrusted = { senderFrame: { url: "https://evil.example/" }, sender: { id: 2 } };
  const call = async (channel: string, ...args: unknown[]): Promise<any> => {
    const handler = handlers.get(channel);
    if (!handler) throw new Error(`no handler for ${channel}`);
    return handler(trusted, ...args);
  };
  const flush = async () => {
    for (let i = 0; i < 8; i += 1) await vi.advanceTimersByTimeAsync(0);
  };
  return {
    handlers, sent, calls, inbox, gate, indicator, confirmSwitch, showWindow, trusted, untrusted, call, flush,
    redeemCalls: () => calls.filter((c) => c.path === "/api/v1/sessions/handoff/redeem"),
    states: () => sent.filter((s) => s.channel === "handoff:event").map((s) => s.payload as HandoffState),
    login: async () => expect((await call("auth:login", "p@example.com", "long-password-1")).ok).toBe(true),
    state: async () => (await call("handoff:get-state")).data as HandoffState,
    // Registers the IPC layer again WITHOUT disposing the first (a held link must survive until attach).
    reattach: () => { disposers.push(attach()); },
    cleanup: () => { for (const dispose of disposers.reverse()) dispose(); rmSync(dir, { recursive: true, force: true }); },
  };
}
type Setup = ReturnType<typeof setup>;

let s: Setup;
const start = (redeem?: (call: FetchCall) => Response | Promise<Response>) => {
  s = setup(redeem);
};
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  s?.cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the already-running app (second instance)", () => {
  it("redeems immediately as the signed-in user and enters READY", async () => {
    start();
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.redeemCalls()).toHaveLength(1);
    const redeemCall = s.redeemCalls()[0] as FetchCall;
    expect(redeemCall.method).toBe("POST");
    expect(redeemCall.headers.Authorization).toBe("Bearer access-1"); // the desktop's own session
    expect(JSON.parse(String(redeemCall.body))).toEqual({ code: CODE });
    expect(s.showWindow).toHaveBeenCalled();
    expect(s.states().map((x) => x.phase)).toEqual(["redeeming", "ready"]);
    expect(await s.state()).toEqual({
      phase: "ready",
      session: { id: SESSION, title: "Backend", company_name: "Acme" },
      failure: null,
    });
  });
});

describe("the cold start (link arrives before the window and IPC exist)", () => {
  // The OS delivers the link before the window and the IPC layer are ready: it is held in memory,
  // then replayed when the controller attaches.
  const coldStart = async () => {
    start();
    s.inbox.detach();
    s.inbox.deliver(LINK);
    expect(s.redeemCalls()).toHaveLength(0);
    s.reattach();
    await s.flush();
  };

  it("holds the link, waits for sign-in, then redeems after a successful login", async () => {
    await coldStart();
    expect((await s.state()).phase).toBe("waiting_signin");
    expect(s.redeemCalls()).toHaveLength(0); // never redeemed without the desktop's own sign-in
    await s.login();
    await s.flush();
    expect(s.redeemCalls()).toHaveLength(1);
    expect((await s.state()).phase).toBe("ready");
  });

  it("a code that outlived its lifetime while waiting is never sent", async () => {
    await coldStart();
    await vi.advanceTimersByTimeAsync(HANDOFF_CODE_TTL_MS + 1_000);
    expect((await s.state()).failure).toBe("expired");
    await s.login();
    await s.flush();
    expect(s.redeemCalls()).toHaveLength(0);
  });
});

describe("READY starts nothing", () => {
  it("makes no audio, transcription, question or screenshot request and changes no setting", async () => {
    start();
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.state()).phase).toBe("ready");
    const paths = s.calls.map((c) => c.path);
    expect(paths.some((p) => /transcribe|questions|extract|screenshot|audio/.test(p))).toBe(false);
    expect(s.gate.read()).toEqual({ state: "idle", source: null });
    expect(s.indicator).not.toHaveBeenCalled();
    expect(s.sent.some((m) => m.channel === "audio:event" || m.channel === "capture:event")).toBe(false);
    const settings = (await s.call("app:get-settings")).data;
    expect(settings.audioInputEnabled).toBe(false);
    expect(settings.screenshotInputEnabled).toBe(false);
  });

  it("the session status is never touched by the desktop (no PATCH or start request)", async () => {
    start();
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.calls.filter((c) => c.method !== "GET" && !c.path.includes("/auth/") && !c.path.includes("handoff"))).toEqual([]);
  });
});

describe("failures are uniform and leak nothing", () => {
  it.each([
    ["expired, reused, malformed or another user's code", notFound(), "invalid"],
    ["too many attempts", json({ error: { code: "rate_limited", message: "SERVER-SECRET-TEXT" } }, 429), "rate_limited"],
    ["the server is unavailable", json({ error: { code: "handoff_unavailable", message: "SERVER-SECRET-TEXT" } }, 503), "unavailable"],
  ])("%s", async (_name, response, failure) => {
    start(() => response.clone());
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    expect(await s.state()).toEqual({ phase: "failed", session: null, failure });
    expect(JSON.stringify(s.sent)).not.toContain("SERVER-SECRET-TEXT");
  });

  it("another account's code looks exactly like an expired one", async () => {
    start(() => notFound());
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    const first = await s.state();
    s.inbox.deliver(LINK);
    await s.flush();
    expect(await s.state()).toEqual(first);
  });

  it("a network error is a fixed 'unavailable'", async () => {
    start(() => { throw new TypeError("fetch failed: ECONNREFUSED 10.0.0.5"); });
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.state()).failure).toBe("unavailable");
    expect(JSON.stringify(s.sent)).not.toContain("ECONNREFUSED");
  });

  it("a malformed link never reaches the server", async () => {
    start();
    await s.login();
    for (const bad of ["interviewassistant://start?h=short", "interviewassistant://start?h=" + CODE + "&x=1", "https://example.com", ""]) {
      s.inbox.deliver(bad);
      await s.flush();
      expect((await s.state()).failure).toBe("invalid");
    }
    expect(s.redeemCalls()).toHaveLength(0);
  });

  it("a session that is no longer ready is refused", async () => {
    start(() => redeemed("ended"));
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.state()).failure).toBe("invalid");
  });
});

describe("the renderer never receives the code", () => {
  it("no message, state or handler result contains it, and no channel could return it", async () => {
    start();
    s.inbox.deliver(LINK);
    await s.flush();
    await s.login();
    await s.flush();
    const everything = JSON.stringify([s.sent, await s.state(), (await s.call("sessions:list")).data]);
    expect(everything).not.toContain(CODE);
    expect(everything).not.toContain("access-1");
    expect([...s.handlers.keys()].filter((c) => c.startsWith("handoff:")).sort()).toEqual(["handoff:dismiss", "handoff:get-state"]);
  });

  it("only the trusted renderer can read or dismiss the handoff state", async () => {
    start();
    for (const channel of ["handoff:get-state", "handoff:dismiss"]) {
      const denied = await (s.handlers.get(channel) as Handler)(s.untrusted);
      expect(denied.ok, channel).toBe(false);
    }
  });

  it("dismiss clears a ready notice", async () => {
    start();
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.call("handoff:dismiss")).ok).toBe(true);
    expect((await s.state()).phase).toBe("idle");
  });
});

describe("sign-out", () => {
  it("clears a ready handoff", async () => {
    start();
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    await s.call("auth:logout");
    expect((await s.state()).phase).toBe("idle");
  });

  it("a link received after sign-out waits for a new sign-in instead of using the old session", async () => {
    start();
    await s.login();
    await s.call("auth:logout");
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.state()).phase).toBe("waiting_signin");
    expect(s.redeemCalls()).toHaveLength(0);
  });
});

describe("a running capture needs a native confirmation before switching", () => {
  const armAudio = async () => {
    expect((await s.call("app:update-settings", { audioInputEnabled: true })).ok).toBe(true);
    expect((await s.call("audio:start", "microphone", SESSION)).ok).toBe(true);
    expect(s.gate.read().state).toBe("arming");
  };

  it("declining keeps the capture running and never redeems", async () => {
    start();
    await s.login();
    await armAudio();
    s.confirmSwitch.mockResolvedValueOnce(false);
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.confirmSwitch).toHaveBeenCalledTimes(1);
    expect(s.redeemCalls()).toHaveLength(0);
    expect(s.gate.read().state).toBe("arming"); // still the user's own capture
    expect((await s.state()).failure).toBe("declined");
  });

  it("accepting redeems, stops the capture and does not start a new one", async () => {
    start();
    await s.login();
    await armAudio();
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.confirmSwitch).toHaveBeenCalledTimes(1);
    expect(s.redeemCalls()).toHaveLength(1);
    expect(s.gate.read()).toEqual({ state: "idle", source: null });
    expect((await s.state()).phase).toBe("ready");
  });

  it("asks nothing when nothing is running", async () => {
    start();
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.confirmSwitch).not.toHaveBeenCalled();
  });
});
