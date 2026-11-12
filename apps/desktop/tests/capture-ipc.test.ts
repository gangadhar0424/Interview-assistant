/**
 * Phase 11B: the real IPC layer (`registerDesktopIpc`) driven with a fake ipcMain, a fake window,
 * a fake screen and a fake backend (`fetch`). No Electron runtime, no real capture, no network.
 */
import { DeepLinkInbox } from "../src/main/deep-link";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { registerDesktopIpc } from "../src/main/ipc";
import { SettingsStore } from "../src/main/settings-store";
import { COUNTDOWN_SECONDS, MIN_CAPTURE_INTERVAL_MS } from "../src/main/capture-service";
import type { CaptureEvent } from "../src/shared/contracts";

const SESSION = "11111111-2222-4333-8444-555555555555";
const NOT_READY = "66666666-2222-4333-8444-555555555555";
const DISPLAY = "2528732444";
const RENDERER = join(tmpdir(), "renderer-fixture", "index.html");
const TRUSTED = pathToFileURL(RENDERER).href;
const PIXELS = 0x5a;

// Results are inspected loosely in these tests (ok/data/error), so they are typed as `any`.
type Handler = (event: unknown, ...args: unknown[]) => Promise<any>;

const user = { id: "00000000-0000-4000-8000-000000000001", email: "p@example.com", created_at: "2026-01-01T00:00:00Z" };
const sessionRow = (id: string, status: string) => ({
  id,
  type: "interview",
  title: null,
  company_name: "Acme",
  status,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  resume: { id: null, filename: "cv.docx" },
  document_count: 0,
});
const doneFrame =
  'event: answer.done\ndata: {"answer_id":"a1","question_id":"q1","provider":"fake","model":"m","metrics":{"retrieval_ms":1,"first_token_ms":2,"total_ms":3},"fallback_used":false,"input_tokens":1,"output_tokens":1}\n\n';
const createdFrame = 'event: question.created\ndata: {"question_id":"q1","answer_id":"a1"}\n\n';

type FetchCall = { path: string; method: string; headers: Record<string, string>; body: unknown };

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function sse(frames: string): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(frames));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

function setup() {
  const dir = mkdtempSync(join(tmpdir(), "capture-ipc-"));
  const handlers = new Map<string, Handler>();
  const listeners = new Map<string, Handler>();
  const windowListeners = new Map<string, Array<() => void>>();
  const contentsListeners = new Map<string, Array<() => void>>();
  const sent: Array<{ channel: string; payload: unknown }> = [];
  const state = { visible: true, focused: true, minimized: false };
  const win = {
    isDestroyed: () => false,
    isVisible: () => state.visible,
    isFocused: () => state.focused,
    isMinimized: () => state.minimized,
    minimize: vi.fn(),
    hide: vi.fn(),
    setOpacity: vi.fn(),
    setAlwaysOnTop: vi.fn(),
    setMinimumSize: vi.fn(),
    setSize: vi.fn(),
    getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
    on: (name: string, fn: () => void) => windowListeners.set(name, [...(windowListeners.get(name) ?? []), fn]),
    removeListener: (name: string, fn: () => void) =>
      windowListeners.set(name, (windowListeners.get(name) ?? []).filter((f) => f !== fn)),
    emit: (name: string) => (windowListeners.get(name) ?? []).forEach((fn) => fn()),
    webContents: {
      id: 1,
      send: (channel: string, payload: unknown) => sent.push({ channel, payload }),
      on: (name: string, fn: () => void) => contentsListeners.set(name, [...(contentsListeners.get(name) ?? []), fn]),
      once: vi.fn(),
      removeListener: vi.fn(),
      emit: (name: string) => (contentsListeners.get(name) ?? []).forEach((fn) => fn()),
    },
  };
  const calls: FetchCall[] = [];
  const backend = {
    sessions: [sessionRow(SESSION, "ready"), sessionRow(NOT_READY, "draft")],
    screenshot: (_call: FetchCall, _init: RequestInit): Response | Promise<Response> =>
      json({ extracted_text: "What is a mutex?", truncated: false, provider: "fake", model: "fake-vision", latency_ms: 4 }),
    questions: (_call: FetchCall): Response => sse(createdFrame + doneFrame),
    uploadedBodies: [] as Uint8Array[],
  };
  const fakeFetch = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const call: FetchCall = {
      path: url.pathname,
      method: init.method ?? "GET",
      headers: { ...(init.headers as Record<string, string>) },
      body: init.body,
    };
    calls.push(call);
    if (call.path === "/api/v1/auth/login") {
      return json(
        { access_token: "access-1", expires_in: 900, user },
        200,
        { "Set-Cookie": "refresh_token=refresh-1; HttpOnly; Path=/api/v1/auth" },
      );
    }
    if (call.path === "/api/v1/sessions") return json({ items: backend.sessions, next_cursor: null });
    if (call.path.endsWith("/screenshots")) {
      backend.uploadedBodies.push(init.body as Uint8Array);
      return backend.screenshot(call, init);
    }
    if (call.path.endsWith("/questions")) return backend.questions(call);
    return json({ error: { code: "not_found", message: "no" } }, 404);
  });
  vi.stubGlobal("fetch", fakeFetch);

  const captureBuffers: Buffer[] = [];
  const captureHost = {
    listDisplays: vi.fn(() => [
      { id: DISPLAY, label: "Display 1 (primary) · 1920×1080", primary: true, width: 1920, height: 1080 },
    ]),
    captureDisplay: vi.fn(async () => {
      const buffer = Buffer.alloc(256, PIXELS);
      captureBuffers.push(buffer);
      return buffer;
    }),
    makePreview: vi.fn(() => ({ dataUrl: "data:image/png;base64,THUMB", width: 1920, height: 1080 })),
    setIndicator: vi.fn(),
  };
  const consent = vi.fn(async () => true);
  const stored = new Map<string, Buffer>();
  const dispose = registerDesktopIpc({
    ipcMain: {
      handle: (channel: string, fn: Handler) => handlers.set(channel, fn),
      removeHandler: (channel: string) => handlers.delete(channel),
      on: (channel: string, fn: Handler) => listeners.set(channel, fn),
      removeListener: (channel: string) => listeners.delete(channel),
    } as never,
    window: win as never,
    settings: new SettingsStore(join(dir, "settings.json")),
    safeStorage: {
      isEncryptionAvailable: () => true,
      encryptString: (value: string) => Buffer.from(value),
      decryptString: (value: Buffer) => value.toString(),
    },
    secureTokenPath: join(dir, "refresh-token.bin"),
    rendererHtmlPath: RENDERER,
    devServerUrl: undefined,
    captureHost,
    confirmScreenshotConsent: consent,
    audioHost: { setIndicator: vi.fn() },
    audioGate: { read: () => ({ state: "idle" as const, source: null }) },
    confirmAudioConsent: async () => true,
    deepLinks: new DeepLinkInbox(),
    confirmHandoffSwitch: async () => true,
    confirmHandoffAccountSwitch: async () => true,
    showWindow: () => undefined,
  });
  void stored;

  const trusted = { senderFrame: { url: TRUSTED }, sender: { id: 1 } };
  const untrusted = { senderFrame: { url: "https://evil.example/page" }, sender: { id: 2 } };
  const call = async (channel: string, ...args: unknown[]): Promise<any> => {
    const handler = handlers.get(channel);
    if (!handler) throw new Error(`no handler for ${channel}`);
    return handler(trusted, ...args);
  };
  const captureEvents = (): CaptureEvent[] =>
    sent.filter((item) => item.channel === "capture:event").map((item) => item.payload as CaptureEvent);
  const questionCalls = () => calls.filter((c) => c.path.endsWith("/questions"));
  const screenshotCalls = () => calls.filter((c) => c.path.endsWith("/screenshots"));

  return {
    handlers,
    win,
    state,
    sent,
    calls,
    backend,
    captureHost,
    captureBuffers,
    consent,
    trusted,
    untrusted,
    call,
    captureEvents,
    questionCalls,
    screenshotCalls,
    cleanup: () => {
      dispose();
      rmSync(dir, { recursive: true, force: true });
    },
    login: async () => {
      expect((await call("auth:login", "p@example.com", "long-password-1")).ok).toBe(true);
    },
    enable: async () => {
      const result = await call("app:update-settings", { screenshotInputEnabled: true });
      expect(result.ok).toBe(true);
    },
    countdown: async () => {
      await vi.advanceTimersByTimeAsync(COUNTDOWN_SECONDS * 1000);
    },
  };
}

type Setup = ReturnType<typeof setup>;
let s: Setup;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(new Date("2026-11-10T10:00:00Z"));
  s = setup();
});
afterEach(() => {
  s.cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function readyForCapture(): Promise<void> {
  await s.login();
  await s.enable();
}

async function capturedId(): Promise<string> {
  const started = await s.call("capture:start", DISPLAY, SESSION);
  expect(started.ok).toBe(true);
  await s.countdown();
  const event = [...s.captureEvents()].reverse().find((e) => e.type === "captured");
  if (event?.type !== "captured") throw new Error("expected a captured event");
  return event.captureId;
}

describe("screenshot input is opt-in", () => {
  it("is off by default and every capture channel refuses while off", async () => {
    await s.login();
    const settings = await s.call("app:get-settings");
    expect(settings).toMatchObject({ ok: true, data: { screenshotInputEnabled: false } });
    for (const [channel, args] of [
      ["capture:list-displays", []],
      ["capture:start", [DISPLAY, SESSION]],
    ] as const) {
      const result = await s.call(channel, ...args);
      expect(result).toMatchObject({ ok: false, error: { code: "screenshot_disabled" } });
    }
    expect(s.captureHost.captureDisplay).not.toHaveBeenCalled();
  });

  it("needs a native confirmation to be turned on; a refusal leaves it off", async () => {
    s.consent.mockResolvedValueOnce(false);
    const refused = await s.call("app:update-settings", { screenshotInputEnabled: true });
    expect(refused).toMatchObject({ ok: false });
    expect((await s.call("app:get-settings")).data.screenshotInputEnabled).toBe(false);
    const allowed = await s.call("app:update-settings", { screenshotInputEnabled: true });
    expect(allowed).toMatchObject({ ok: true, data: { screenshotInputEnabled: true } });
    expect(s.consent).toHaveBeenCalledTimes(2);
  });

  it("turning it off needs no confirmation and discards a held image", async () => {
    await readyForCapture();
    await capturedId();
    const buffer = s.captureBuffers[0] as Buffer;
    s.consent.mockClear();
    const off = await s.call("app:update-settings", { screenshotInputEnabled: false });
    expect(off.ok).toBe(true);
    expect(s.consent).not.toHaveBeenCalled();
    expect(buffer.every((byte) => byte === 0)).toBe(true);
    expect(s.captureEvents().at(-1)).toMatchObject({ type: "cancelled", reason: "disabled" });
  });

  it("rejects a non-boolean setting and unknown keys", async () => {
    expect(await s.call("app:update-settings", { screenshotInputEnabled: "yes" })).toMatchObject({ ok: false });
    expect(await s.call("app:update-settings", { screenCapture: true })).toMatchObject({ ok: false });
  });
});

describe("IPC sender validation and the renderer's capabilities", () => {
  const channels: Array<[string, unknown[]]> = [
    ["capture:list-displays", []],
    ["capture:start", [DISPLAY, SESSION]],
    ["capture:cancel", []],
    ["capture:extract", ["99999999-2222-4333-8444-555555555555"]],
    ["sessions:stream-start", ["99999999-2222-4333-8444-555555555555", SESSION, "hi", "99999999-2222-4333-8444-555555555555"]],
    ["app:update-settings", [{ screenshotInputEnabled: true }]],
  ];

  it.each(channels)("%s refuses an untrusted sender and does nothing", async (channel, args) => {
    await readyForCapture();
    s.calls.length = 0;
    s.consent.mockClear();
    const handler = s.handlers.get(channel) as Handler;
    const result = await handler(s.untrusted, ...args);
    expect(result).toMatchObject({ ok: false, error: { message: "Untrusted IPC sender." } });
    expect(s.captureHost.captureDisplay).not.toHaveBeenCalled();
    expect(s.captureHost.listDisplays).not.toHaveBeenCalled();
    expect(s.calls).toEqual([]); // no network request on behalf of an untrusted sender
    expect(s.consent).not.toHaveBeenCalled();
  });

  it("exposes only the four named capture channels (no generic capture)", () => {
    const capture = [...s.handlers.keys()].filter((name) => name.startsWith("capture:")).sort();
    expect(capture).toEqual(["capture:cancel", "capture:extract", "capture:list-displays", "capture:start"]);
  });

  it("validates arguments strictly", async () => {
    await readyForCapture();
    for (const args of [["abc", SESSION], ["12; drop", SESSION], [DISPLAY, "not-a-uuid"], [123, SESSION], [DISPLAY]]) {
      expect(await s.call("capture:start", ...args)).toMatchObject({ ok: false, error: { kind: "invalid_input" } });
    }
    for (const arg of ["x", 5, null, "../../etc"]) {
      expect(await s.call("capture:extract", arg)).toMatchObject({ ok: false, error: { kind: "invalid_input" } });
    }
    expect(s.captureHost.captureDisplay).not.toHaveBeenCalled();
  });

  it("the renderer can only observe a thumbnail: events never include full-image bytes or credentials", async () => {
    await readyForCapture();
    await capturedId();
    const serialized = JSON.stringify(s.sent);
    expect(serialized).not.toContain("access-1");
    expect(serialized).not.toContain("refresh-1");
    expect(serialized).not.toContain(String(PIXELS).repeat(20));
    const captured = s.captureEvents().find((e) => e.type === "captured");
    expect(captured).toMatchObject({ previewDataUrl: "data:image/png;base64,THUMB" });
  });
});

describe("the complete flow: capture, preview, confirm, extract, review, ask", () => {
  it("runs end to end and marks only this question as a screenshot question", async () => {
    await readyForCapture();
    const id = await capturedId();
    // Preview before send: the captured image exists but nothing has gone to the backend yet.
    expect(s.screenshotCalls()).toEqual([]);
    expect(s.questionCalls()).toEqual([]);

    const extracted = await s.call("capture:extract", id);
    expect(extracted).toMatchObject({ ok: true, data: { text: "What is a mutex?", truncated: false } });
    expect(s.screenshotCalls()).toHaveLength(1);
    const upload = s.screenshotCalls()[0] as FetchCall;
    expect(upload.method).toBe("POST");
    expect(upload.path).toBe(`/api/v1/sessions/${SESSION}/screenshots`);
    expect(upload.headers["Content-Type"]).toBe("image/png"); // a raw body, never multipart
    expect(upload.headers.Authorization).toBe("Bearer access-1"); // the token is added in main only
    expect(upload.body).not.toBeInstanceOf(FormData);
    // The image is gone from memory once extraction finished, and no question was started yet.
    expect((s.captureBuffers[0] as Buffer).every((byte) => byte === 0)).toBe(true);
    expect(s.questionCalls()).toEqual([]);

    // The user reviews/edits the text and presses Ask: only now does the existing SSE path run.
    const requestId = "77777777-2222-4333-8444-555555555555";
    const started = await s.call("sessions:stream-start", requestId, SESSION, "What is a mutex? (edited)", extracted.data.extractionId);
    expect(started.ok).toBe(true);
    await vi.waitFor(() => expect(s.questionCalls()).toHaveLength(1));
    const question = s.questionCalls()[0] as FetchCall;
    expect(question.headers.Accept).toBe("text/event-stream");
    expect(JSON.parse(String(question.body))).toEqual({
      text: "What is a mutex? (edited)",
      input_source: "screenshot",
    });
    await vi.waitFor(() =>
      expect(s.sent.some((m) => m.channel === "stream:message" && JSON.stringify(m.payload).includes("answer.done"))).toBe(true),
    );
  });

  it("a typed question is unchanged: exactly {text}, no input_source", async () => {
    await readyForCapture();
    const started = await s.call("sessions:stream-start", "77777777-2222-4333-8444-555555555555", SESSION, "Typed question");
    expect(started.ok).toBe(true);
    await vi.waitFor(() => expect(s.questionCalls()).toHaveLength(1));
    expect(JSON.parse(String((s.questionCalls()[0] as FetchCall).body))).toEqual({ text: "Typed question" });
    expect(s.screenshotCalls()).toEqual([]);
  });

  it("an extraction id cannot be forged, replayed, reused for another session, or invented by the renderer", async () => {
    await readyForCapture();
    const id = await capturedId();
    const { data } = await s.call("capture:extract", id);
    const request = (n: number) => `7777777${n}-2222-4333-8444-555555555555`;

    for (const forged of ["00000000-0000-4000-8000-000000000000", "x", 7]) {
      const refused = await s.call("sessions:stream-start", request(1), SESSION, "q", forged);
      expect(refused).toMatchObject({ ok: false });
    }
    expect(s.questionCalls()).toEqual([]);

    expect((await s.call("sessions:stream-start", request(3), SESSION, "q", data.extractionId)).ok).toBe(true);
    await vi.waitFor(() => expect(s.questionCalls()).toHaveLength(1));
    await vi.waitFor(() => expect(s.sent.some((m) => JSON.stringify(m.payload).includes("answer.done"))).toBe(true));
    // single use: a second question cannot reuse the same screenshot flow
    expect(await s.call("sessions:stream-start", request(4), SESSION, "q again", data.extractionId)).toMatchObject({ ok: false });
  });

  it("a screenshot token cannot be used for a different session", async () => {
    await readyForCapture();
    const { data } = await s.call("capture:extract", await capturedId());
    const refused = await s.call("sessions:stream-start", "77777777-2222-4333-8444-555555555559", NOT_READY, "q", data.extractionId);
    expect(refused).toMatchObject({ ok: false });
    expect(s.questionCalls()).toEqual([]);
    // the mismatched attempt invalidates the token: it cannot then be used for the right session either
    expect(await s.call("sessions:stream-start", "77777777-2222-4333-8444-555555555558", SESSION, "q", data.extractionId)).toMatchObject({ ok: false });
  });

  it("keeps the screenshot token if the question could not start, so the user can retry", async () => {
    await readyForCapture();
    const { data } = await s.call("capture:extract", await capturedId());
    s.backend.questions = () => json({ error: { code: "rate_limited", message: "Slow down." } }, 429);
    expect((await s.call("sessions:stream-start", "77777777-2222-4333-8444-555555555551", SESSION, "q", data.extractionId)).ok).toBe(true);
    await vi.waitFor(() => expect(s.questionCalls()).toHaveLength(1));
    await vi.waitFor(() => expect(JSON.stringify(s.sent)).toContain("rate_limited"));
    s.backend.questions = () => sse(createdFrame + doneFrame);
    expect((await s.call("sessions:stream-start", "77777777-2222-4333-8444-555555555552", SESSION, "q", data.extractionId)).ok).toBe(true);
    await vi.waitFor(() => expect(s.questionCalls()).toHaveLength(2));
    expect(JSON.parse(String((s.questionCalls()[1] as FetchCall).body)).input_source).toBe("screenshot");
  });
});

describe("conditions are enforced in the main process", () => {
  it("rejects when no valid session is selected, or it is not ready", async () => {
    await readyForCapture();
    for (const id of [NOT_READY, "00000000-0000-4000-8000-000000000000"]) {
      expect(await s.call("capture:start", DISPLAY, id)).toMatchObject({
        ok: false,
        error: { code: "session_not_ready" },
      });
    }
    expect(s.captureHost.captureDisplay).not.toHaveBeenCalled();
  });

  it("rejects when signed out", async () => {
    await s.enable().catch(() => undefined);
    expect(await s.call("capture:start", DISPLAY, SESSION)).toMatchObject({ ok: false });
  });

  it.each([
    ["hidden", () => (s.state.visible = false)],
    ["unfocused", () => (s.state.focused = false)],
    ["minimised", () => (s.state.minimized = true)],
  ])("rejects when the window is %s", async (_name, arrange) => {
    await readyForCapture();
    arrange();
    expect(await s.call("capture:start", DISPLAY, SESSION)).toMatchObject({
      ok: false,
      error: { code: "window_not_ready" },
    });
    expect(s.captureHost.captureDisplay).not.toHaveBeenCalled();
  });

  it("does not capture if the window is hidden during the countdown", async () => {
    await readyForCapture();
    expect((await s.call("capture:start", DISPLAY, SESSION)).ok).toBe(true);
    s.state.visible = false;
    await s.countdown();
    expect(s.captureHost.captureDisplay).not.toHaveBeenCalled();
    expect(s.captureEvents().at(-1)).toMatchObject({ type: "cancelled", reason: "window" });
  });

  it("enforces the minimum interval between captures", async () => {
    await readyForCapture();
    await capturedId();
    await s.call("capture:cancel");
    expect(await s.call("capture:start", DISPLAY, SESSION)).toMatchObject({ ok: false, error: { code: "too_soon" } });
    await vi.advanceTimersByTimeAsync(MIN_CAPTURE_INTERVAL_MS);
    expect((await s.call("capture:start", DISPLAY, SESSION)).ok).toBe(true);
  });
});

describe("discard on success, failure, cancellation, navigation and window cleanup", () => {
  it("discards after a failed upload and shows the server's safe message only", async () => {
    await readyForCapture();
    s.backend.screenshot = () => json({ error: { code: "invalid_image", message: "The image is not a valid PNG within the allowed size." } }, 422);
    const id = await capturedId();
    const failed = await s.call("capture:extract", id);
    expect(failed).toMatchObject({ ok: false, error: { status: 422, code: "invalid_image" } });
    expect((s.captureBuffers[0] as Buffer).every((byte) => byte === 0)).toBe(true);
    expect(await s.call("capture:extract", id)).toMatchObject({ ok: false });
  });

  it.each([
    ["vision disabled", 503, "vision_unavailable"],
    ["rate limited", 429, "rate_limited"],
    ["too large", 413, "screenshot_too_large"],
  ])("maps a backend refusal (%s) to a safe error and still discards the image", async (_name, status, code) => {
    await readyForCapture();
    s.backend.screenshot = () => json({ error: { code, message: "Safe message." } }, status);
    const failed = await s.call("capture:extract", await capturedId());
    expect(failed).toMatchObject({ ok: false, error: { status, code } });
    expect((s.captureBuffers[0] as Buffer).every((byte) => byte === 0)).toBe(true);
  });

  it("discards when the backend is unreachable, with the generic safe message", async () => {
    await readyForCapture();
    s.backend.screenshot = () => {
      throw new TypeError("socket detail 10.0.0.5");
    };
    const failed = await s.call("capture:extract", await capturedId());
    expect(failed).toMatchObject({ ok: false, error: { code: "network_error" } });
    expect(JSON.stringify(failed)).not.toContain("10.0.0.5");
    expect((s.captureBuffers[0] as Buffer).every((byte) => byte === 0)).toBe(true);
  });

  it("cancelling during the upload aborts the request and wipes the image", async () => {
    await readyForCapture();
    let aborted = false;
    s.backend.screenshot = (_call, init) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          aborted = true;
          reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
        });
      });
    const id = await capturedId();
    const pending = s.call("capture:extract", id);
    await vi.advanceTimersByTimeAsync(0);
    expect((await s.call("capture:cancel")).ok).toBe(true);
    expect((await pending).ok).toBe(false);
    expect(aborted).toBe(true);
    expect((s.captureBuffers[0] as Buffer).every((byte) => byte === 0)).toBe(true);
  });

  it("cancelling during the countdown never captures", async () => {
    await readyForCapture();
    await s.call("capture:start", DISPLAY, SESSION);
    await vi.advanceTimersByTimeAsync(1500);
    await s.call("capture:cancel");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(s.captureHost.captureDisplay).not.toHaveBeenCalled();
  });

  it.each([
    ["the window is hidden (close to tray)", (x: Setup) => x.win.emit("hide")],
    ["the window is minimised", (x: Setup) => x.win.emit("minimize")],
    ["the page navigates or reloads", (x: Setup) => x.win.webContents.emit("did-start-navigation")],
    ["the renderer process dies", (x: Setup) => x.win.webContents.emit("render-process-gone")],
    ["the window is closed", (x: Setup) => x.win.emit("closed")],
  ])("a held image is wiped when %s", async (_name, trigger) => {
    await readyForCapture();
    await capturedId();
    const buffer = s.captureBuffers[0] as Buffer;
    expect(buffer.every((byte) => byte === 0)).toBe(false);
    trigger(s);
    expect(buffer.every((byte) => byte === 0)).toBe(true);
  });

  it("a held image is wiped on sign-out and when the backend changes", async () => {
    await readyForCapture();
    await capturedId();
    const first = s.captureBuffers[0] as Buffer;
    await s.call("auth:logout");
    expect(first.every((byte) => byte === 0)).toBe(true);

    await readyForCapture();
    await vi.advanceTimersByTimeAsync(MIN_CAPTURE_INTERVAL_MS);
    await capturedId();
    const second = s.captureBuffers[1] as Buffer;
    await s.call("app:update-settings", { backendBaseUrl: "http://127.0.0.1:9000" });
    expect(second.every((byte) => byte === 0)).toBe(true);
  });

  it("a held image is wiped when the session expires (401)", async () => {
    await readyForCapture();
    const id = await capturedId();
    s.backend.screenshot = () => json({ error: { code: "not_authenticated", message: "No." } }, 401);
    const result = await s.call("capture:extract", id);
    expect(result).toMatchObject({ ok: false });
    expect((s.captureBuffers[0] as Buffer).every((byte) => byte === 0)).toBe(true);
  });

  it("dispose (app quit) wipes a held image", async () => {
    await readyForCapture();
    await capturedId();
    const buffer = s.captureBuffers[0] as Buffer;
    s.cleanup();
    expect(buffer.every((byte) => byte === 0)).toBe(true);
    s = setup(); // keep the shared afterEach happy
  });
});
