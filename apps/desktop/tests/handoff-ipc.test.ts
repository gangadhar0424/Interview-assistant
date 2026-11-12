/**
 * Web -> desktop handoff (D-55/D-56): the real IPC layer driven with a fake ipcMain/window/backend.
 * Proves the desktop is signed in by the handoff itself (no desktop login), cold-start and
 * already-running delivery, the account-switch and capture-switch confirmations, uniform failures,
 * that the renderer never receives the code or any token, and that READY never starts a capture.
 */
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DeepLinkInbox } from "../src/main/deep-link";
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

const USER_A = { id: "00000000-0000-4000-8000-0000000000aa", email: "a@example.com", created_at: "2026-01-01T00:00:00Z" };
const USER_B = { id: "00000000-0000-4000-8000-0000000000bb", email: "b@example.com", created_at: "2026-01-01T00:00:00Z" };
const sessionRow = (id: string, status: string) => ({
  id, type: "interview", title: "Backend", company_name: "Acme", status,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  resume: { id: null, filename: "cv.docx" }, document_count: 0,
});
const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const cookie = (value: string) => ({ "Set-Cookie": `refresh_token=${value}; HttpOnly; Path=/api/v1/auth` });
const tokens = (user: typeof USER_A, access: string) => ({ access_token: access, token_type: "bearer", expires_in: 900, user });
const notFound = () => json({ error: { code: "handoff_invalid", message: "SERVER-SECRET-TEXT" } }, 404);

/** The minted login is for this user; refresh cookies map back to the user they belong to. */
const world = { minted: USER_A, status: "ready" };
const cookieOwner: Record<string, typeof USER_A> = { "refresh-1": USER_A, "refresh-b": USER_B };

function handoffResponse(): Response {
  const owner = world.minted;
  const refresh = owner.id === USER_B.id ? "handoff-refresh-b" : "handoff-refresh-a";
  cookieOwner[refresh] = owner;
  return json(
    { ...tokens(owner, `handoff-access-${owner.id.slice(-2)}`), session: sessionRow(SESSION, world.status), preferences: { language: "en" } },
    200,
    cookie(refresh),
  );
}

function setup(redeem: (call: FetchCall) => Response | Promise<Response> = () => handoffResponse()) {
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
  world.minted = USER_A;
  world.status = "ready";
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const call: FetchCall = {
      path: url.pathname, method: init.method ?? "GET",
      headers: { ...(init.headers as Record<string, string>) }, body: init.body,
    };
    calls.push(call);
    if (call.path === "/api/v1/auth/login") return json(tokens(USER_A, "access-1"), 200, cookie("refresh-1"));
    if (call.path === "/api/v1/auth/refresh") {
      const presented = /refresh_token=([^;]*)/.exec(call.headers.Cookie ?? "")?.[1] ?? "";
      const owner = cookieOwner[presented];
      return owner ? json(tokens(owner, `access-for-${owner.id.slice(-2)}`), 200, cookie(`rotated-${presented}`)) : json({ error: { code: "invalid_refresh_token", message: "no" } }, 401);
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
  const confirmAccountSwitch = vi.fn(async () => true);
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
      confirmHandoffAccountSwitch: confirmAccountSwitch,
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
    for (let i = 0; i < 10; i += 1) await vi.advanceTimersByTimeAsync(0);
  };
  return {
    handlers, sent, calls, inbox, gate, indicator, confirmSwitch, confirmAccountSwitch, showWindow, trusted, untrusted, call, flush, dir,
    redeemCalls: () => calls.filter((c) => c.path === "/api/v1/sessions/handoff/redeem"),
    logoutCalls: () => calls.filter((c) => c.path === "/api/v1/auth/logout"),
    states: () => sent.filter((s) => s.channel === "handoff:event").map((s) => s.payload as HandoffState),
    login: async () => expect((await call("auth:login", "a@example.com", "long-password-1")).ok).toBe(true),
    state: async () => (await call("handoff:get-state")).data as HandoffState,
    whoAmI: async () => (await call("auth:get-state")).data.user as typeof USER_A | null,
    storedRefresh: () => readFileSync(join(dir, "refresh-token.bin"), "utf8"),
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

describe("the desktop is signed in by the handoff itself (no desktop login)", () => {
  it("a signed-out desktop redeems the code, becomes the web user and enters READY", async () => {
    start();
    expect(await s.whoAmI()).toBeNull(); // nobody signed in on the desktop
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.redeemCalls()).toHaveLength(1);
    const redeemCall = s.redeemCalls()[0] as FetchCall;
    expect(redeemCall.method).toBe("POST");
    expect(redeemCall.headers.Authorization).toBeUndefined(); // public: the code is the credential
    expect(JSON.parse(String(redeemCall.body))).toEqual({ code: CODE });
    expect(s.states().map((x) => x.phase)).toEqual(["redeeming", "ready"]);
    expect(await s.state()).toEqual({
      phase: "ready",
      session: { id: SESSION, title: "Backend", company_name: "Acme" },
      failure: null,
    });
    // The minted login is now the desktop's own: it persists and restores without any password.
    expect(s.storedRefresh()).toBe("handoff-refresh-a");
    expect((await s.whoAmI())?.id).toBe(USER_A.id);
    expect(s.calls.some((c) => c.path === "/api/v1/auth/login")).toBe(false);
  });

  it("the window is brought forward so the result is visible", async () => {
    start();
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.showWindow).toHaveBeenCalled();
  });

  it("a desktop already signed in as the same user keeps its login and drops the spare", async () => {
    start();
    await s.login();
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.confirmAccountSwitch).not.toHaveBeenCalled();
    expect(s.storedRefresh()).toBe("refresh-1"); // not replaced
    expect(s.logoutCalls()).toHaveLength(1); // the spare minted login was revoked
    expect(s.logoutCalls()[0]?.headers.Cookie).toBe("refresh_token=handoff-refresh-a");
    expect((await s.state()).phase).toBe("ready");
  });
});

describe("the cold start (link arrives before the window and IPC exist)", () => {
  it("holds the link in memory and redeems it the moment the controller attaches", async () => {
    start();
    s.inbox.detach();
    s.inbox.deliver(LINK);
    expect(s.redeemCalls()).toHaveLength(0);
    s.reattach();
    await s.flush();
    expect(s.redeemCalls()).toHaveLength(1);
    expect((await s.state()).phase).toBe("ready");
    expect((await s.whoAmI())?.id).toBe(USER_A.id);
  });
});

describe("a handoff for a different account than the one signed in", () => {
  const asOtherUser = async () => {
    start();
    await s.login(); // signed in as A
    world.minted = USER_B; // the web user who pressed Start Interview is B
  };

  it("asks first, then switches the account and ends the old account's work", async () => {
    await asOtherUser();
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.confirmAccountSwitch).toHaveBeenCalledTimes(1);
    expect(s.storedRefresh()).toBe("handoff-refresh-b");
    expect((await s.whoAmI())?.id).toBe(USER_B.id);
    expect((await s.state()).phase).toBe("ready");
  });

  it("declining keeps account A and revokes the minted login for B", async () => {
    await asOtherUser();
    s.confirmAccountSwitch.mockResolvedValueOnce(false);
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.storedRefresh()).toBe("refresh-1");
    expect((await s.whoAmI())?.id).toBe(USER_A.id);
    expect(s.logoutCalls().some((c) => c.headers.Cookie === "refresh_token=handoff-refresh-b")).toBe(true);
    expect(await s.state()).toEqual({ phase: "failed", session: null, failure: "declined" });
  });
});

describe("READY starts nothing", () => {
  it("makes no audio, transcription, question or screenshot request and changes no setting", async () => {
    start();
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
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.calls.filter((c) => c.method !== "GET" && !c.path.includes("/auth/") && !c.path.includes("handoff"))).toEqual([]);
  });
});

describe("failures are uniform, leak nothing and sign nobody in", () => {
  it.each([
    ["expired, reused or malformed code", () => notFound(), "invalid"],
    ["too many attempts", () => json({ error: { code: "rate_limited", message: "SERVER-SECRET-TEXT" } }, 429), "rate_limited"],
    ["the server is unavailable", () => json({ error: { code: "handoff_unavailable", message: "SERVER-SECRET-TEXT" } }, 503), "unavailable"],
  ])("%s", async (_name, make, failure) => {
    start(() => make());
    s.inbox.deliver(LINK);
    await s.flush();
    expect(await s.state()).toEqual({ phase: "failed", session: null, failure });
    expect(JSON.stringify(s.sent)).not.toContain("SERVER-SECRET-TEXT");
    expect(await s.whoAmI()).toBeNull(); // a failed handoff signs nobody in
  });

  it("a network error is a fixed 'unavailable'", async () => {
    start(() => { throw new TypeError("fetch failed: ECONNREFUSED 10.0.0.5"); });
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.state()).failure).toBe("unavailable");
    expect(JSON.stringify(s.sent)).not.toContain("ECONNREFUSED");
  });

  it("a malformed link never reaches the server", async () => {
    start();
    for (const bad of ["interviewassistant://start?h=short", `interviewassistant://start?h=${CODE}&x=1`, "https://example.com", ""]) {
      s.inbox.deliver(bad);
      await s.flush();
      expect((await s.state()).failure).toBe("invalid");
    }
    expect(s.redeemCalls()).toHaveLength(0);
  });

  it("a session that is no longer ready is refused and its login is revoked, not adopted", async () => {
    start();
    world.status = "ended";
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.state()).failure).toBe("invalid");
    expect(await s.whoAmI()).toBeNull();
    expect(s.logoutCalls()).toHaveLength(1);
  });

  it("a response without the refresh cookie is refused as a whole", async () => {
    start(() => json({ ...tokens(USER_A, "x"), session: sessionRow(SESSION, "ready"), preferences: {} }, 200));
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.state()).phase).toBe("failed");
    expect(await s.whoAmI()).toBeNull();
  });
});

describe("the renderer never receives the code or any token", () => {
  it("no message, state or handler result contains them, and no channel could return them", async () => {
    start();
    s.inbox.deliver(LINK);
    await s.flush();
    const everything = JSON.stringify([s.sent, await s.state(), await s.whoAmI(), (await s.call("sessions:list")).data]);
    expect(everything).not.toContain(CODE);
    expect(everything).not.toMatch(/handoff-access|handoff-refresh|refresh_token|access_token/);
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
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.call("handoff:dismiss")).ok).toBe(true);
    expect((await s.state()).phase).toBe("idle");
  });
});

describe("sign-out", () => {
  it("clears a ready handoff and the minted login with it", async () => {
    start();
    s.inbox.deliver(LINK);
    await s.flush();
    expect((await s.call("auth:logout")).ok).toBe(true);
    expect((await s.state()).phase).toBe("idle");
    expect(s.logoutCalls().some((c) => c.headers.Cookie === "refresh_token=handoff-refresh-a")).toBe(true);
    expect(await s.whoAmI()).toBeNull();
  });
});

describe("a running capture needs a native confirmation before switching", () => {
  const armAudio = async () => {
    expect((await s.call("app:update-settings", { audioInputEnabled: true })).ok).toBe(true);
    expect((await s.call("audio:start", "microphone", SESSION)).ok).toBe(true);
    expect(s.gate.read().state).toBe("arming");
  };

  it("declining keeps the capture running and never redeems (the code is not consumed)", async () => {
    start();
    await s.login();
    await armAudio();
    s.confirmSwitch.mockResolvedValueOnce(false);
    s.inbox.deliver(LINK);
    await s.flush();
    expect(s.confirmSwitch).toHaveBeenCalledTimes(1);
    expect(s.redeemCalls()).toHaveLength(0);
    expect(s.gate.read().state).toBe("arming");
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
