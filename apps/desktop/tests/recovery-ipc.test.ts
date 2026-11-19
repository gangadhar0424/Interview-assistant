/**
 * M-5: the recovery and diagnostics channels through the real registerDesktopIpc, with a fake ipcMain,
 * a fake window and a MOCKED clipboard. This proves wiring, redaction and what crosses into the
 * renderer; it does not prove what the real Windows clipboard or dialogs do.
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const writeText = vi.hoisted(() => vi.fn());
vi.mock("electron", () => ({ clipboard: { writeText } }));

import { diagnostics } from "../src/main/diagnostics";
import { DeepLinkInbox } from "../src/main/deep-link";
import { registerDesktopIpc } from "../src/main/ipc";
import { SettingsStore } from "../src/main/settings-store";

const RENDERER = join(tmpdir(), "renderer-fixture", "index.html");
const TRUSTED = pathToFileURL(RENDERER).href;
type Handler = (event: unknown, ...args: unknown[]) => Promise<any>;
const PRIVATE_HOST = "private-host.corp-internal-example";

function setup(recovery?: { takeNotice(): boolean }) {
  const dir = mkdtempSync(join(tmpdir(), "recovery-ipc-"));
  const handlers = new Map<string, Handler>();
  const win = {
    isDestroyed: () => false,
    isVisible: () => true,
    isFocused: () => false,
    isMinimized: () => false,
    minimize: vi.fn(), hide: vi.fn(), show: vi.fn(), restore: vi.fn(), focus: vi.fn(),
    setOpacity: vi.fn(), setAlwaysOnTop: vi.fn(), setMinimumSize: vi.fn(), setSize: vi.fn(),
    getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
    on: vi.fn(), removeListener: vi.fn(),
    webContents: { id: 1, send: vi.fn(), on: vi.fn(), once: vi.fn(), removeListener: vi.fn() },
  };
  const store = new SettingsStore(join(dir, "settings.json"));
  store.update({ backendBaseUrl: `https://${PRIVATE_HOST}` });
  const dispose = registerDesktopIpc({
    ipcMain: {
      handle: (channel: string, fn: Handler) => handlers.set(channel, fn),
      removeHandler: (channel: string) => handlers.delete(channel),
      on: vi.fn(),
      removeListener: vi.fn(),
    } as never,
    window: win as never,
    settings: store,
    safeStorage: { isEncryptionAvailable: () => true, encryptString: (v: string) => Buffer.from(v), decryptString: (v: Buffer) => v.toString() },
    secureTokenPath: join(dir, "refresh-token.bin"),
    rendererHtmlPath: RENDERER,
    devServerUrl: undefined,
    captureHost: { listDisplays: () => [], captureDisplay: async () => null, makePreview: () => ({ dataUrl: "", width: 0, height: 0 }), setIndicator: vi.fn() },
    confirmScreenshotConsent: async () => true,
    audioHost: { setIndicator: vi.fn() },
    audioGate: { read: () => ({ state: "idle", source: null }) },
    confirmAudioConsent: async () => true,
    confirmAcceptableUse: async () => true,
    deepLinks: new DeepLinkInbox(),
    confirmHandoffSwitch: async () => true,
    confirmHandoffAccountSwitch: async () => true,
    showWindow: () => undefined,
    ...(recovery ? { recovery } : {}),
  });
  const trusted = { senderFrame: { url: TRUSTED }, sender: { id: 1 } };
  const untrusted = { senderFrame: { url: "https://evil.example/" }, sender: { id: 2 } };
  const call = (channel: string, ...args: unknown[]) => handlers.get(channel)!(trusted, ...args);
  return {
    handlers, trusted, untrusted, call, dispose,
    cleanup: () => {
      dispose();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

let s: ReturnType<typeof setup>;
beforeEach(() => writeText.mockClear());
afterEach(() => s.cleanup());

describe("app:get-recovery", () => {
  it("reports the notice once, then nothing", async () => {
    let pending = true;
    s = setup({ takeNotice: () => { const was = pending; pending = false; return was; } });
    expect(await s.call("app:get-recovery")).toEqual({ ok: true, data: { notice: "recovered" } });
    expect(await s.call("app:get-recovery")).toEqual({ ok: true, data: { notice: null } });
  });

  it("reports no notice when recovery is not wired", async () => {
    s = setup();
    expect(await s.call("app:get-recovery")).toEqual({ ok: true, data: { notice: null } });
  });

  it("refuses an untrusted sender", async () => {
    s = setup({ takeNotice: () => true });
    expect(await s.handlers.get("app:get-recovery")!(s.untrusted)).toMatchObject({ ok: false });
  });
});

describe("app:copy-diagnostics", () => {
  it("copies a report from the main process and returns only counts, never the text", async () => {
    s = setup();
    const result = await s.call("app:copy-diagnostics");
    expect(result.ok).toBe(true);
    expect(Object.keys(result.data).sort()).toEqual(["characters", "truncated"]);
    expect(writeText).toHaveBeenCalledTimes(1);
    const text = writeText.mock.calls[0]![0] as string;
    expect(result.data.characters).toBe(text.length);
    expect(JSON.stringify(result)).not.toContain("Interview Assistant diagnostics"); // the renderer never sees the text
  });

  it("includes live service state as typed values and leaks no host name, id or content", async () => {
    s = setup();
    await s.call("app:copy-diagnostics");
    const text = writeText.mock.calls[0]![0] as string;
    expect(text).toContain("[services]");
    expect(text).toContain("signed in: no");
    expect(text).toContain("audio: idle");
    expect(text).toContain("backend: remote"); // the host itself is never included
    expect(text).not.toContain(PRIVATE_HOST);
    expect(text).not.toMatch(/https?:\/\//);
    expect(text).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    expect(text).not.toContain("refresh");
  });

  it("records that diagnostics were copied, and keeps the report bounded", async () => {
    s = setup();
    for (let i = 0; i < 300; i += 1) diagnostics.record("renderer_gone", { reason: "crashed", code: i });
    await s.call("app:copy-diagnostics");
    const text = writeText.mock.calls[0]![0] as string;
    expect(text.length).toBeLessThanOrEqual(16_000);
    expect(diagnostics.recent(5).some((e) => e.type === "diagnostics_copied")).toBe(true);
  });

  it("refuses an untrusted sender and copies nothing", async () => {
    s = setup();
    expect(await s.handlers.get("app:copy-diagnostics")!(s.untrusted)).toMatchObject({ ok: false });
    expect(writeText).not.toHaveBeenCalled();
  });

  it("stops reporting service state once the IPC layer is disposed (shutdown)", () => {
    s = setup();
    expect(diagnostics.report().text).toContain("[services]");
    s.dispose();
    expect(diagnostics.report().text).not.toContain("[services]");
  });

  it("exposes a renderer cleanup that is safe to call repeatedly", () => {
    s = setup();
    expect(() => {
      s.dispose.cleanupRenderer();
      s.dispose.cleanupRenderer();
    }).not.toThrow();
  });
});
