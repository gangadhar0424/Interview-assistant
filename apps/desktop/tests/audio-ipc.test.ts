import { ACCEPTABLE_USE_VERSION } from "../src/shared/acknowledgement";
import { acknowledgedSettings } from "./ack-fixture";
/**
 * Phase 12: the real IPC layer driven with a fake ipcMain/window/backend. Proves the consent gate,
 * sender validation, the gated media permission, the audio -> transcript -> confirmed question
 * flow (input_source = audio), cleanup on window/sign-out, and that no audio or transcript text
 * reaches disk, logs or the renderer settings.
 */
import { DeepLinkInbox } from "../src/main/deep-link";
import { HANDOFF_LINK, HANDOFF_REDEEM_PATH, settleHandoff } from "./handoff-signin";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { registerDesktopIpc } from "../src/main/ipc";
import { SettingsStore } from "../src/main/settings-store";
import { WINDOW_SAMPLES } from "../src/main/audio-vad";
import type { AudioGateState } from "../src/main/media-permissions";
import type { AudioEvent } from "../src/shared/contracts";

const SESSION = "11111111-2222-4333-8444-555555555555";
const NOT_READY = "66666666-2222-4333-8444-555555555555";
const RENDERER = join(tmpdir(), "renderer-fixture", "index.html");
const TRUSTED = pathToFileURL(RENDERER).href;
const TRANSCRIPT = "SENTINEL-TRANSCRIPT what is a mutex";

type Handler = (event: unknown, ...args: unknown[]) => Promise<any>;
type FetchCall = { path: string; method: string; headers: Record<string, string>; body: unknown; query: string };

const user = { id: "00000000-0000-4000-8000-000000000001", email: "p@example.com", created_at: "2026-01-01T00:00:00Z" };
const sessionRow = (id: string, status: string) => ({
  id, type: "interview", title: null, company_name: "Acme", status,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  resume: { id: null, filename: "cv.docx" }, document_count: 0,
});
const doneFrame =
  'event: answer.done\ndata: {"answer_id":"a1","question_id":"q1","provider":"fake","model":"m","metrics":{"retrieval_ms":1,"first_token_ms":2,"total_ms":3},"fallback_used":false,"input_tokens":1,"output_tokens":1}\n\n';
const createdFrame = 'event: question.created\ndata: {"question_id":"q1","answer_id":"a1"}\n\n';

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const sse = (frames: string) => {
  const enc = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({ start(c) { c.enqueue(enc.encode(frames)); c.close(); } }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
};

const tone = (windows: number, amplitude: number) => {
  const samples = new Int16Array(windows * WINDOW_SAMPLES);
  for (let i = 0; i < samples.length; i += 1) samples[i] = Math.round(Math.sin(i / 5) * amplitude);
  return new Uint8Array(samples.buffer);
};

function setup(opts: { acknowledged?: boolean; updates?: UpdatesStub } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "audio-ipc-"));
  const handlers = new Map<string, Handler>();
  const listeners = new Map<string, (event: unknown, data: unknown) => void>();
  const windowListeners = new Map<string, Array<() => void>>();
  const webListeners = new Map<string, Array<(...args: unknown[]) => void>>();
  const questionSignals: AbortSignal[] = [];
  const sent: Array<{ channel: string; payload: unknown }> = [];
  const state = { visible: true, focused: true, minimized: false };
  const win = {
    isDestroyed: () => false,
    isVisible: () => state.visible,
    isFocused: () => state.focused,
    isMinimized: () => state.minimized,
    minimize: vi.fn(), hide: vi.fn(), setOpacity: vi.fn(), setAlwaysOnTop: vi.fn(), setMinimumSize: vi.fn(), setSize: vi.fn(),
    getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
    on: (name: string, fn: () => void) => windowListeners.set(name, [...(windowListeners.get(name) ?? []), fn]),
    removeListener: vi.fn(),
    emit: (name: string) => (windowListeners.get(name) ?? []).forEach((fn) => fn()),
    webContents: {
      id: 1,
      send: (channel: string, payload: unknown) => sent.push({ channel, payload }),
      on: (name: string, fn: (...args: unknown[]) => void) => webListeners.set(name, [...(webListeners.get(name) ?? []), fn]),
      once: vi.fn(),
      removeListener: vi.fn(),
      emit: (name: string, ...args: unknown[]) => (webListeners.get(name) ?? []).forEach((fn) => fn(undefined, ...args)),
    },
  };
  const calls: FetchCall[] = [];
  const backend = {
    voice: "fake" as string | null,
    /** When true the answer stream never finishes, so a test can see it being aborted. */
    hold: false,
    transcribe: (): Response =>
      json({ text: TRANSCRIPT, truncated: false, is_question: true, confidence: 0.9, provider: "fake", model: "m", latency_ms: 3 }),
  };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const call: FetchCall = {
      path: url.pathname, query: url.search, method: init.method ?? "GET",
      headers: { ...(init.headers as Record<string, string>) }, body: init.body,
    };
    calls.push(call);
    if (call.path === HANDOFF_REDEEM_PATH) {
      return json({ access_token: "access-1", expires_in: 900, user, session: sessionRow(SESSION, "ready"), preferences: { language: "en" } }, 200, { "Set-Cookie": "refresh_token=refresh-1; HttpOnly; Path=/api/v1/auth" });
    }
    if (call.path === "/api/v1/auth/login") {
      return json({ access_token: "access-1", expires_in: 900, user }, 200, { "Set-Cookie": "refresh_token=refresh-1; HttpOnly; Path=/api/v1/auth" });
    }
    if (call.path === "/api/v1/auth/logout") return json({}, 200);
    if (call.path === "/api/v1/sessions") return json({ items: [sessionRow(SESSION, "ready"), sessionRow(NOT_READY, "draft")], next_cursor: null });
    if (call.path === `/api/v1/sessions/${SESSION}` && call.method === "GET") {
      return json({ ...sessionRow(SESSION, "ready"), preferences: { stt_provider: backend.voice } });
    }
    if (call.path === `/api/v1/sessions/${SESSION}` && call.method === "PATCH") {
      backend.voice = (JSON.parse(String(call.body)) as { preferences: { stt_provider: string } }).preferences.stt_provider;
      return json({ ...sessionRow(SESSION, "ready"), preferences: { stt_provider: backend.voice } });
    }
    if (call.path.endsWith("/transcribe")) return backend.transcribe();
    if (call.path.endsWith("/questions")) {
      if (backend.hold) {
        if (init.signal) questionSignals.push(init.signal);
        return new Response(new ReadableStream<Uint8Array>({ start() {} }), { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      return sse(createdFrame + doneFrame);
    }
    return json({ error: { code: "not_found", message: "no" } }, 404);
  }));

  const store =
    opts.acknowledged === false ? new SettingsStore(join(dir, "settings.json")) : acknowledgedSettings(dir, user.id);
  const acceptUse = vi.fn(async () => true);
  const gate = { read: (): AudioGateState => ({ state: "idle", source: null }) };
  const indicator = vi.fn();
  const consent = vi.fn(async () => true);
  const inbox = new DeepLinkInbox();
  const dispose = registerDesktopIpc({
    ipcMain: {
      handle: (channel: string, fn: Handler) => handlers.set(channel, fn),
      removeHandler: (channel: string) => handlers.delete(channel),
      on: (channel: string, fn: (event: unknown, data: unknown) => void) => listeners.set(channel, fn),
      removeListener: (channel: string) => listeners.delete(channel),
    } as never,
    window: win as never,
    settings: store,
    safeStorage: { isEncryptionAvailable: () => true, encryptString: (v: string) => Buffer.from(v), decryptString: (v: Buffer) => v.toString() },
    secureTokenPath: join(dir, "refresh-token.bin"),
    rendererHtmlPath: RENDERER,
    devServerUrl: undefined,
    captureHost: { listDisplays: () => [], captureDisplay: async () => null, makePreview: () => ({ dataUrl: "", width: 0, height: 0 }), setIndicator: vi.fn() },
    confirmScreenshotConsent: async () => true,
    audioHost: { setIndicator: indicator },
    audioGate: gate,
    confirmAudioConsent: consent,
    confirmAcceptableUse: acceptUse,
    deepLinks: inbox,
    confirmHandoffSwitch: async () => true,
    confirmHandoffAccountSwitch: async () => true,
    showWindow: () => undefined,
    ...(opts.updates ? { updates: opts.updates as never } : {}),
  });

  const trusted = { senderFrame: { url: TRUSTED }, sender: { id: 1 } };
  const untrusted = { senderFrame: { url: "https://evil.example/" }, sender: { id: 2 } };
  const call = async (channel: string, ...args: unknown[]): Promise<any> => {
    const handler = handlers.get(channel);
    if (!handler) throw new Error(`no handler for ${channel}`);
    return handler(trusted, ...args);
  };
  const chunk = (data: unknown, event: unknown = trusted) => listeners.get("audio:chunk")?.(event, data);
  const audioEvents = () => sent.filter((s) => s.channel === "audio:event").map((s) => s.payload as AudioEvent);
  const frames = async (bytes: Uint8Array) => {
    for (let offset = 0; offset < bytes.length; offset += 3_200) {
      chunk(bytes.slice(offset, offset + 3_200));
      await vi.advanceTimersByTimeAsync(100);
    }
  };
  return {
    dir, store, acceptUse, questionSignals, handlers, listeners, win, state, sent, calls, gate, indicator, consent, trusted, untrusted, call, chunk, audioEvents, frames,
    transcribeCalls: () => calls.filter((c) => c.path.endsWith("/transcribe")),
    questionCalls: () => calls.filter((c) => c.path.endsWith("/questions")),
    backend,
    cleanup: () => { dispose(); rmSync(dir, { recursive: true, force: true }); },
    login: async () => {
      inbox.deliver(HANDOFF_LINK);
      await settleHandoff();
      expect((await call("handoff:get-state")).data.phase).toBe("ready");    },
    enable: async () => expect((await call("app:update-settings", { audioInputEnabled: true })).ok).toBe(true),
    speakOnePhrase: async () => {
      await frames(tone(40, 12_000));
      await frames(new Uint8Array(60 * WINDOW_SAMPLES * 2));
    },
  };
}
type Setup = ReturnType<typeof setup>;
type UpdatesStub = {
  getState: () => unknown;
  check: (trigger?: string) => Promise<unknown>;
  download: () => Promise<unknown>;
  installAndRestart: () => Promise<unknown>;
};

describe("audio IPC", () => {
  let s: Setup;
  beforeEach(() => {
    vi.useFakeTimers();
    s = setup();
  });
  afterEach(() => {
    s.cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("is off by default and start is refused until it is enabled", async () => {
    await s.login();
    expect((await s.call("app:get-settings")).data.audioInputEnabled).toBe(false);
    const result = await s.call("audio:start", "microphone", SESSION);
    expect(result.ok).toBe(false);
    expect(s.gate.read()).toEqual({ state: "idle", source: null });
    expect(s.indicator).not.toHaveBeenCalled();
  });

  it("turning it on needs the native confirmation, and cancelling keeps it off", async () => {
    await s.login();
    s.consent.mockResolvedValueOnce(false);
    expect((await s.call("app:update-settings", { audioInputEnabled: true })).ok).toBe(false);
    expect((await s.call("app:get-settings")).data.audioInputEnabled).toBe(false);
    await s.enable();
    expect(s.consent).toHaveBeenCalledTimes(2);
    expect((await s.call("app:get-settings")).data.audioInputEnabled).toBe(true);
  });

  it("only the trusted renderer can use the audio channels, and an untrusted chunk is ignored", async () => {
    await s.login();
    await s.enable();
    const denied = await (s.handlers.get("audio:start") as Handler)(s.untrusted, "microphone", SESSION);
    expect(denied.ok).toBe(false);
    expect(s.gate.read().state).toBe("idle");
    expect((await s.call("audio:start", "microphone", SESSION)).ok).toBe(true);
    s.chunk(tone(5, 12_000), s.untrusted);
    expect(s.gate.read().state).toBe("arming"); // the untrusted chunk did not move it to listening
    s.chunk(tone(5, 12_000));
    expect(s.gate.read().state).toBe("listening");
  });

  it("validates its arguments", async () => {
    await s.login();
    await s.enable();
    for (const args of [["camera", SESSION], ["microphone", "not-a-uuid"], [undefined, undefined], [{ x: 1 }, SESSION]]) {
      expect((await s.call("audio:start", ...args)).ok, JSON.stringify(args)).toBe(false);
    }
    expect((await s.call("app:update-settings", { audioInputEnabled: "yes" })).ok).toBe(false);
    expect(s.gate.read().state).toBe("idle");
  });

  it("refuses a session that is not ready", async () => {
    await s.login();
    await s.enable();
    expect((await s.call("audio:start", "microphone", NOT_READY)).ok).toBe(false);
    expect(s.gate.read().state).toBe("idle");
  });

  it("opens the permission gate only for the armed source and closes it on Stop", async () => {
    await s.login();
    await s.enable();
    await s.call("audio:start", "system", SESSION);
    expect(s.gate.read()).toEqual({ state: "arming", source: "system" });
    expect((await s.call("audio:stop")).ok).toBe(true);
    expect(s.gate.read()).toEqual({ state: "idle", source: null });
  });

  it("chunks sent when nothing was started are ignored", async () => {
    await s.login();
    await s.enable();
    s.chunk(tone(40, 12_000));
    await s.frames(new Uint8Array(60 * WINDOW_SAMPLES * 2));
    expect(s.transcribeCalls()).toEqual([]);
    expect(s.audioEvents()).toEqual([]);
  });

  it("speech becomes a transcript line, a raw audio/wav upload, and no question yet", async () => {
    await s.login();
    await s.enable();
    await s.call("audio:start", "microphone", SESSION);
    await s.speakOnePhrase();
    const upload = s.transcribeCalls();
    expect(upload).toHaveLength(1);
    expect(upload[0]?.headers["Content-Type"]).toBe("audio/wav");
    expect(upload[0]?.headers.Authorization).toBe("Bearer access-1");
    expect(s.audioEvents().some((e) => e.type === "utterance" && e.text === TRANSCRIPT && e.isQuestion)).toBe(true);
    expect(s.questionCalls()).toEqual([]); // auto-answer is off: nothing is asked without the user
  });

  it("asking from an utterance token sends input_source = audio, once", async () => {
    await s.login();
    await s.enable();
    await s.call("audio:start", "microphone", SESSION);
    await s.speakOnePhrase();
    const line = s.audioEvents().find((e) => e.type === "utterance") as Extract<AudioEvent, { type: "utterance" }>;
    const first = await s.call("sessions:stream-start", "aaaaaaaa-1111-4222-8333-444444444441", SESSION, line.text, line.id);
    expect(first.ok).toBe(true);
    await vi.advanceTimersByTimeAsync(50);
    const questions = s.questionCalls();
    expect(questions).toHaveLength(1);
    expect(String(questions[0]?.body)).toContain('"input_source":"audio"');
    const replay = await s.call("sessions:stream-start", "aaaaaaaa-1111-4222-8333-444444444442", SESSION, line.text, line.id);
    expect(replay.ok).toBe(false);
  });

  it("an unknown or foreign-session token cannot be used to mark a question as audio", async () => {
    await s.login();
    await s.enable();
    const forged = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
    expect((await s.call("sessions:stream-start", "aaaaaaaa-1111-4222-8333-444444444441", SESSION, "typed text", forged)).ok).toBe(false);
    expect(s.questionCalls()).toEqual([]);
  });

  it("hiding or minimising the window stops listening and drops the phrase in progress", async () => {
    await s.login();
    await s.enable();
    await s.call("audio:start", "microphone", SESSION);
    await s.frames(tone(40, 12_000));
    s.win.emit("hide");
    await vi.advanceTimersByTimeAsync(50);
    expect(s.gate.read().state).toBe("idle");
    expect(s.transcribeCalls()).toEqual([]);
    expect(s.audioEvents().at(-1)).toMatchObject({ type: "stopped", reason: "window" });
  });

  it("signing out stops listening", async () => {
    await s.login();
    await s.enable();
    await s.call("audio:start", "microphone", SESSION);
    await s.call("auth:logout");
    expect(s.gate.read().state).toBe("idle");
  });

  it("turning the setting off stops listening", async () => {
    await s.login();
    await s.enable();
    await s.call("audio:start", "microphone", SESSION);
    await s.call("app:update-settings", { audioInputEnabled: false });
    expect(s.gate.read().state).toBe("idle");
  });

  it("persists only the setting: no audio or transcript on disk", async () => {
    await s.login();
    await s.enable();
    await s.call("audio:start", "microphone", SESSION);
    await s.speakOnePhrase();
    await s.call("audio:stop");
    for (const name of readdirSync(s.dir)) {
      const text = readFileSync(join(s.dir, name)).toString("latin1");
      expect(text, name).not.toContain("SENTINEL");
      expect(text, name).not.toContain("RIFF");
    }
    expect(readdirSync(s.dir).sort()).toEqual(["refresh-token.bin", "settings.json"]);
  });

  it("transcript text is never logged", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    await s.login();
    await s.enable();
    await s.call("audio:start", "microphone", SESSION);
    await s.speakOnePhrase();
    await s.call("audio:stop");
    for (const spy of spies) expect(JSON.stringify(spy.mock.calls)).not.toContain("SENTINEL");
    spies.forEach((spy) => spy.mockRestore());
  });

  it("a server that has audio unavailable stops listening with a clear message", async () => {
    s.backend.transcribe = () => json({ error: { code: "stt_unavailable", message: "Audio transcription is not available." } }, 503);
    await s.login();
    await s.enable();
    await s.call("audio:start", "microphone", SESSION);
    await s.speakOnePhrase();
    expect(s.gate.read().state).toBe("idle");
    expect(s.audioEvents().at(-1)).toMatchObject({ type: "stopped", reason: "unavailable" });
  });
});

describe("acceptable-use acknowledgement (M-1, D-68): audio", () => {
  let s: Setup;
  beforeEach(() => {
    vi.useFakeTimers();
    s = setup({ acknowledged: false });
  });
  afterEach(() => {
    s.cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("first use: not accepted, and audio start is refused in main even when everything else is ready", async () => {
    await s.login();
    await s.enable();
    expect((await s.call("app:get-acknowledgement")).data).toEqual({ accepted: false, version: ACCEPTABLE_USE_VERSION, acceptedAt: null });
    const result = await s.call("audio:start", "microphone", SESSION);
    expect(result).toMatchObject({ ok: false, error: { code: "acknowledgement_required" } });
    expect(s.gate.read()).toEqual({ state: "idle", source: null });
    expect(s.indicator).not.toHaveBeenCalled();
    // Audio pushed anyway is ignored: nothing was started, so nothing is uploaded.
    await s.speakOnePhrase();
    expect(s.transcribeCalls()).toEqual([]);
  });

  it("accepting goes through the native dialog; cancelling leaves it unaccepted and audio blocked", async () => {
    await s.login();
    await s.enable();
    s.acceptUse.mockResolvedValueOnce(false);
    expect((await s.call("app:accept-acknowledgement", ACCEPTABLE_USE_VERSION)).ok).toBe(false);
    expect((await s.call("app:get-acknowledgement")).data.accepted).toBe(false);
    expect((await s.call("audio:start", "microphone", SESSION)).ok).toBe(false);

    const accepted = await s.call("app:accept-acknowledgement", ACCEPTABLE_USE_VERSION);
    expect(accepted).toMatchObject({ ok: true, data: { accepted: true, version: ACCEPTABLE_USE_VERSION } });
    expect(s.acceptUse).toHaveBeenCalledTimes(2);
    // Allowed now, and the existing explicit start / indicator behaviour is unchanged.
    expect((await s.call("audio:start", "microphone", SESSION)).ok).toBe(true);
    expect(s.gate.read().state).toBe("arming");
    expect(s.indicator).toHaveBeenCalledWith("arming", "microphone");
  });

  it("accepting again does not show the dialog again", async () => {
    await s.login();
    await s.call("app:accept-acknowledgement", ACCEPTABLE_USE_VERSION);
    await s.call("app:accept-acknowledgement", ACCEPTABLE_USE_VERSION);
    expect(s.acceptUse).toHaveBeenCalledTimes(1);
  });

  it("rejects invalid inputs without opening the dialog", async () => {
    await s.login();
    for (const bad of [0, ACCEPTABLE_USE_VERSION + 1, -1, 1.5, Number.NaN, "1", null, undefined, {}, [1], true]) {
      expect((await s.call("app:accept-acknowledgement", bad)).ok, String(bad)).toBe(false);
    }
    expect((await s.call("app:accept-acknowledgement")).ok).toBe(false);
    expect(s.acceptUse).not.toHaveBeenCalled();
    expect((await s.call("app:get-acknowledgement")).data.accepted).toBe(false);
  });

  it("cannot be accepted while signed out, and an untrusted sender can neither read nor accept it", async () => {
    expect((await s.call("app:accept-acknowledgement", ACCEPTABLE_USE_VERSION)).ok).toBe(false);
    expect(s.acceptUse).not.toHaveBeenCalled();
    await s.login();
    for (const channel of ["app:get-acknowledgement", "app:accept-acknowledgement"]) {
      const handler = s.handlers.get(channel);
      expect(await handler?.(s.untrusted, 1)).toMatchObject({ ok: false });
    }
    expect(s.acceptUse).not.toHaveBeenCalled();
  });

  it("the renderer cannot record acceptance through the settings channel", async () => {
    await s.login();
    const forged = { version: ACCEPTABLE_USE_VERSION, userId: user.id, acceptedAt: "2026-11-18T00:00:00Z" };
    expect((await s.call("app:update-settings", { acceptableUse: forged })).ok).toBe(false);
    expect((await s.call("app:update-settings", { acknowledged: true })).ok).toBe(false);
    expect((await s.call("app:get-acknowledgement")).data.accepted).toBe(false);
  });

  it("is stored locally, survives a restart and a settings change, and holds no content", async () => {
    await s.login();
    await s.enable();
    await s.call("app:accept-acknowledgement", ACCEPTABLE_USE_VERSION);
    await s.call("app:update-settings", { alwaysOnTop: false }); // a later save must not erase it
    const file = join(s.dir, "settings.json");
    const restarted = new SettingsStore(file);
    restarted.load();
    expect(restarted.acknowledgement(user.id).accepted).toBe(true);
    expect(restarted.acknowledgement("00000000-0000-4000-8000-0000000000ff").accepted).toBe(false);
    expect(restarted.acknowledgement(null).accepted).toBe(false);
    const body = readFileSync(file, "utf8");
    expect(Object.keys(JSON.parse(body).acceptableUse).sort()).toEqual(["acceptedAt", "userId", "version"]);
    for (const secret of ["access-1", "refresh-1", TRANSCRIPT, "p@example.com"]) expect(body).not.toContain(secret);
  });
});

describe("renderer crash (M-5, D-70): the main process stops what the dead window owned", () => {
  let s: Setup;
  const crash = (reason = "crashed") => s.win.webContents.emit("render-process-gone", { reason, exitCode: 9 });
  beforeEach(() => {
    vi.useFakeTimers();
    s = setup();
  });
  afterEach(() => {
    s.cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("stops listening, releases the media gate and ignores audio that still arrives", async () => {
    await s.login();
    await s.enable();
    expect((await s.call("audio:start", "microphone", SESSION)).ok).toBe(true);
    expect(s.gate.read).toBeDefined();
    s.chunk(tone(5, 12_000)); // first audio: listening
    await vi.advanceTimersByTimeAsync(100);
    s.indicator.mockClear();
    crash();
    expect(s.indicator).toHaveBeenCalledWith("idle", null);
    const before = s.transcribeCalls().length;
    await s.speakOnePhrase();
    expect(s.transcribeCalls().length).toBe(before); // nothing uploads after the crash
  });

  it("aborts an answer that was still streaming to the dead window", async () => {
    await s.login();
    s.backend.hold = true;
    expect((await s.call("sessions:stream-start", "aaaaaaaa-1111-4222-8333-444444444444", SESSION, "What is a mutex?")).ok).toBe(true);
    await vi.advanceTimersByTimeAsync(50);
    expect(s.questionSignals).toHaveLength(1);
    expect(s.questionSignals[0]?.aborted).toBe(false);
    crash();
    expect(s.questionSignals[0]?.aborted).toBe(true);
  });

  it("is safe to repeat (the recovery module calls the same cleanup again)", async () => {
    await s.login();
    await s.enable();
    await s.call("audio:start", "microphone", SESSION);
    expect(() => {
      crash();
      crash("oom");
    }).not.toThrow();
  });

  it("keeps the account signed in and starts nothing by itself; a deliberate start still works", async () => {
    await s.login();
    await s.enable();
    crash();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(s.gate.read()).toEqual({ state: "idle", source: null });
    expect(s.indicator).not.toHaveBeenCalledWith("arming", expect.anything());
    expect(s.transcribeCalls()).toEqual([]);
    expect(s.questionCalls()).toEqual([]);
    expect((await s.call("sessions:list")).ok).toBe(true); // still signed in: main kept the account
    expect((await s.call("audio:start", "microphone", SESSION)).ok).toBe(true); // explicit, as before
  });

  it("the acknowledgement and shortcut protections are unchanged after a crash", async () => {
    const t = setup({ acknowledged: false });
    try {
      await t.login();
      await t.enable();
      t.win.webContents.emit("render-process-gone", { reason: "crashed" });
      const refused = await t.call("audio:start", "microphone", SESSION);
      expect(refused).toMatchObject({ ok: false, error: { code: "acknowledgement_required" } });
    } finally {
      t.cleanup();
    }
  });
});

describe("updates never start, and always stop, capture (M-11)", () => {
  const downloaded = { status: "downloaded", version: "0.2.0" };

  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("without an updater the four channels answer 'disabled' and do nothing", async () => {
    const t = setup();
    try {
      await t.login();
      for (const channel of ["updates:get-state", "updates:check", "updates:download", "updates:install"]) {
        expect(await t.call(channel)).toEqual({ ok: true, data: { status: "disabled", reason: "development" } });
      }
    } finally {
      t.cleanup();
    }
  });

  it("refuses a caller that is not the app's own window", async () => {
    const updates = { getState: vi.fn(() => downloaded), check: vi.fn(), download: vi.fn(), installAndRestart: vi.fn() };
    const t = setup({ updates: updates as never });
    try {
      for (const channel of ["updates:get-state", "updates:check", "updates:download", "updates:install"]) {
        const result = await (t.handlers.get(channel) as Handler)(t.untrusted);
        expect(result.ok, channel).toBe(false);
      }
      expect(updates.check).not.toHaveBeenCalled();
      expect(updates.download).not.toHaveBeenCalled();
      expect(updates.installAndRestart).not.toHaveBeenCalled();
    } finally {
      t.cleanup();
    }
  });

  it("check and download are delegated to the manager; a check is always a manual one", async () => {
    const updates = {
      getState: vi.fn(() => ({ status: "idle", lastCheckedAt: null })),
      check: vi.fn(async () => ({ status: "none", checkedAt: "2026-11-22T00:00:00.000Z" })),
      download: vi.fn(async () => ({ status: "downloaded", version: "0.2.0" })),
      installAndRestart: vi.fn(),
    };
    const t = setup({ updates: updates as never });
    try {
      expect((await t.call("updates:get-state")).data.status).toBe("idle");
      await t.call("updates:check", "periodic", "ignored-extra-argument");
      expect(updates.check).toHaveBeenCalledWith("manual");
      await t.call("updates:download", { version: "9.9.9" });
      expect(updates.download).toHaveBeenCalledWith();
    } finally {
      t.cleanup();
    }
  });

  it("installing stops listening BEFORE the app is asked to quit, and nothing starts it again", async () => {
    const seen: string[] = [];
    let t!: Setup;
    const updates = {
      getState: vi.fn(() => downloaded),
      check: vi.fn(),
      download: vi.fn(),
      installAndRestart: vi.fn(async () => {
        seen.push(t.gate.read().state);
        return { status: "installing", version: "0.2.0" };
      }),
    };
    t = setup({ updates: updates as never });
    try {
      await t.login();
      await t.enable();
      expect((await t.call("audio:start", "microphone", SESSION)).ok).toBe(true);
      expect(t.gate.read().state).toBe("arming");
      const result = await t.call("updates:install");
      expect(result).toEqual({ ok: true, data: { status: "installing", version: "0.2.0" } });
      expect(seen).toEqual(["idle"]); // listening was already stopped when the install was requested
      await vi.advanceTimersByTimeAsync(60_000);
      expect(t.gate.read().state).toBe("idle");
      expect(t.transcribeCalls()).toHaveLength(0);
      expect(t.calls.filter((c) => c.path.includes("/screenshots"))).toHaveLength(0);
    } finally {
      t.cleanup();
    }
  });

  it("while an install is in progress audio and capture cannot be started; a failed install releases them", async () => {
    // Regression for the M-11 review: dispose() alone left audio:start/capture:start callable.
    let state: { status: string; version?: string; code?: string } = { status: "installing", version: "0.2.0" };
    const updates = { getState: vi.fn(() => state), check: vi.fn(), download: vi.fn(), installAndRestart: vi.fn() };
    const t = setup({ updates: updates as never });
    try {
      await t.login();
      await t.enable();
      const audio = await t.call("audio:start", "microphone", SESSION);
      expect(audio.ok).toBe(false);
      expect(JSON.stringify(audio)).toContain("update is being installed");
      expect(t.gate.read().state).toBe("idle");
      const shot = await t.call("capture:start", "display:1", SESSION);
      expect(shot.ok).toBe(false);
      expect(JSON.stringify(shot)).toContain("update is being installed");
      expect(t.calls.filter((c) => c.path.includes("/screenshots"))).toHaveLength(0);
      state = { status: "error", code: "install_failed" };
      expect((await t.call("audio:start", "microphone", SESSION)).ok).toBe(true);
    } finally {
      t.cleanup();
    }
  });

  it("an install request outside the 'downloaded' state touches nothing: listening continues", async () => {
    const updates = {
      getState: vi.fn(() => ({ status: "available", version: "0.2.0", sizeBytes: 1 })),
      check: vi.fn(),
      download: vi.fn(),
      installAndRestart: vi.fn(),
    };
    const t = setup({ updates: updates as never });
    try {
      await t.login();
      await t.enable();
      await t.call("audio:start", "microphone", SESSION);
      const result = await t.call("updates:install");
      expect(result.data.status).toBe("available");
      expect(updates.installAndRestart).not.toHaveBeenCalled();
      expect(t.gate.read().state).toBe("arming");
    } finally {
      t.cleanup();
    }
  });

  it("a failed restart can be retried, and stops capture again first", async () => {
    const updates = {
      getState: vi.fn(() => ({ status: "error", code: "install_failed", message: "x", retryable: false })),
      check: vi.fn(),
      download: vi.fn(),
      installAndRestart: vi.fn(async () => ({ status: "installing", version: "0.2.0" })),
    };
    const t = setup({ updates: updates as never });
    try {
      await t.login();
      await t.enable();
      await t.call("audio:start", "microphone", SESSION);
      await t.call("updates:install");
      expect(updates.installAndRestart).toHaveBeenCalledTimes(1);
      expect(t.gate.read().state).toBe("idle");
    } finally {
      t.cleanup();
    }
  });
});
