/**
 * Phase 12: the real IPC layer driven with a fake ipcMain/window/backend. Proves the consent gate,
 * sender validation, the gated media permission, the audio -> transcript -> confirmed question
 * flow (input_source = audio), cleanup on window/sign-out, and that no audio or transcript text
 * reaches disk, logs or the renderer settings.
 */
import { DeepLinkInbox } from "../src/main/deep-link";
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

function setup() {
  const dir = mkdtempSync(join(tmpdir(), "audio-ipc-"));
  const handlers = new Map<string, Handler>();
  const listeners = new Map<string, (event: unknown, data: unknown) => void>();
  const windowListeners = new Map<string, Array<() => void>>();
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
    webContents: { id: 1, send: (channel: string, payload: unknown) => sent.push({ channel, payload }), on: vi.fn(), once: vi.fn(), removeListener: vi.fn() },
  };
  const calls: FetchCall[] = [];
  const backend = {
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
    if (call.path === "/api/v1/auth/login") {
      return json({ access_token: "access-1", expires_in: 900, user }, 200, { "Set-Cookie": "refresh_token=refresh-1; HttpOnly; Path=/api/v1/auth" });
    }
    if (call.path === "/api/v1/auth/logout") return json({}, 200);
    if (call.path === "/api/v1/sessions") return json({ items: [sessionRow(SESSION, "ready"), sessionRow(NOT_READY, "draft")], next_cursor: null });
    if (call.path.endsWith("/transcribe")) return backend.transcribe();
    if (call.path.endsWith("/questions")) return sse(createdFrame + doneFrame);
    return json({ error: { code: "not_found", message: "no" } }, 404);
  }));

  const gate = { read: (): AudioGateState => ({ state: "idle", source: null }) };
  const indicator = vi.fn();
  const consent = vi.fn(async () => true);
  const dispose = registerDesktopIpc({
    ipcMain: {
      handle: (channel: string, fn: Handler) => handlers.set(channel, fn),
      removeHandler: (channel: string) => handlers.delete(channel),
      on: (channel: string, fn: (event: unknown, data: unknown) => void) => listeners.set(channel, fn),
      removeListener: (channel: string) => listeners.delete(channel),
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
    confirmAudioConsent: consent,
    deepLinks: new DeepLinkInbox(),
    confirmHandoffSwitch: async () => true,
    confirmHandoffAccountSwitch: async () => true,
    showWindow: () => undefined,
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
    dir, handlers, listeners, win, state, sent, calls, gate, indicator, consent, trusted, untrusted, call, chunk, audioEvents, frames,
    transcribeCalls: () => calls.filter((c) => c.path.endsWith("/transcribe")),
    questionCalls: () => calls.filter((c) => c.path.endsWith("/questions")),
    backend,
    cleanup: () => { dispose(); rmSync(dir, { recursive: true, force: true }); },
    login: async () => expect((await call("auth:login", "p@example.com", "long-password-1")).ok).toBe(true),
    enable: async () => expect((await call("app:update-settings", { audioInputEnabled: true })).ok).toBe(true),
    speakOnePhrase: async () => {
      await frames(tone(40, 12_000));
      await frames(new Uint8Array(60 * WINDOW_SAMPLES * 2));
    },
  };
}
type Setup = ReturnType<typeof setup>;

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
