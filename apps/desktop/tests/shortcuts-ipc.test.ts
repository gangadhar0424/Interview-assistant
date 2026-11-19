/**
 * M-4: the shortcut IPC channels driven through the real registerDesktopIpc with a fake ipcMain and
 * window. Mocked Electron: this proves validation, persistence and wiring, not real Windows behaviour.
 */
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DeepLinkInbox } from "../src/main/deep-link";
import { registerDesktopIpc } from "../src/main/ipc";
import { SettingsStore } from "../src/main/settings-store";
import { ShortcutManager, type ShortcutRegistrar } from "../src/main/shortcuts";
import { DEFAULT_SHORTCUT_CONFIG, DEFAULT_SHORTCUTS, SHORTCUT_ACTIONS } from "../src/shared/shortcuts";

const RENDERER = join(tmpdir(), "renderer-fixture", "index.html");
const TRUSTED = pathToFileURL(RENDERER).href;
type Handler = (event: unknown, ...args: unknown[]) => Promise<any>;
const D = DEFAULT_SHORTCUTS;

function setup(withManager: boolean, foreign: string[] = []) {
  const dir = mkdtempSync(join(tmpdir(), "shortcuts-ipc-"));
  const handlers = new Map<string, Handler>();
  const win = {
    isDestroyed: () => false,
    isVisible: () => true,
    isFocused: () => true,
    isMinimized: () => false,
    minimize: vi.fn(), hide: vi.fn(), show: vi.fn(), restore: vi.fn(), focus: vi.fn(),
    setOpacity: vi.fn(), setAlwaysOnTop: vi.fn(), setMinimumSize: vi.fn(), setSize: vi.fn(),
    getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
    on: vi.fn(), removeListener: vi.fn(),
    webContents: { id: 1, send: vi.fn(), on: vi.fn(), once: vi.fn(), removeListener: vi.fn() },
  };
  const registered = new Map<string, () => void>();
  for (const accelerator of foreign) registered.set(accelerator, () => undefined);
  const registrar: ShortcutRegistrar = {
    register: (accelerator, callback) => {
      if (registered.has(accelerator)) return false;
      registered.set(accelerator, callback);
      return true;
    },
    unregister: (accelerator) => void registered.delete(accelerator),
  };
  // Capture and audio hosts that must NEVER be touched by a shortcut.
  const captureDisplay = vi.fn(async () => null);
  const captureIndicator = vi.fn();
  const audioIndicator = vi.fn();
  const manager = new ShortcutManager({
    registrar,
    window: {
      isVisible: () => win.isVisible(),
      isFocused: () => win.isFocused(),
      isMinimized: () => win.isMinimized(),
      restore: () => win.restore(),
      show: () => win.show(),
      focus: () => win.focus(),
      hide: () => win.hide(),
      showPanel: (panel) => win.webContents.send("shortcut:event", { panel }),
    },
    now: () => Date.now(),
  });
  const store = new SettingsStore(join(dir, "settings.json"));
  const apply = vi.spyOn(manager, "apply");
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
    captureHost: { listDisplays: () => [], captureDisplay, makePreview: () => ({ dataUrl: "", width: 0, height: 0 }), setIndicator: captureIndicator },
    confirmScreenshotConsent: async () => true,
    audioHost: { setIndicator: audioIndicator },
    audioGate: { read: () => ({ state: "idle", source: null }) },
    confirmAudioConsent: async () => true,
    confirmAcceptableUse: async () => true,
    deepLinks: new DeepLinkInbox(),
    confirmHandoffSwitch: async () => true,
    confirmHandoffAccountSwitch: async () => true,
    showWindow: () => undefined,
    ...(withManager ? { shortcuts: manager } : {}),
  });
  const trusted = { senderFrame: { url: TRUSTED }, sender: { id: 1 } };
  const untrusted = { senderFrame: { url: "https://evil.example/" }, sender: { id: 2 } };
  const call = (channel: string, ...args: unknown[]) => {
    const handler = handlers.get(channel);
    if (!handler) throw new Error(`no handler for ${channel}`);
    return handler(trusted, ...args);
  };
  return {
    dir, handlers, win, registered, manager, apply, store, trusted, untrusted, call,
    captureDisplay, captureIndicator, audioIndicator,
    cleanup: () => {
      dispose();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

const cfg = (over: Partial<typeof DEFAULT_SHORTCUTS> = {}, enabled = true) => ({
  enabled,
  bindings: { ...D, ...over },
});

describe("shortcut IPC channels", () => {
  let s: ReturnType<typeof setup>;
  afterEach(() => s.cleanup());

  it("without a registrar it reports the defaults with every shortcut off, and still validates and saves", async () => {
    s = setup(false);
    const got = await s.call("app:get-shortcuts");
    expect(got.ok).toBe(true);
    expect(got.data.config).toEqual(DEFAULT_SHORTCUT_CONFIG);
    expect(got.data.defaults).toEqual(D);
    for (const action of SHORTCUT_ACTIONS) expect(got.data.status[action].kind).toBe("disabled");
    const saved = await s.call("app:set-shortcuts", cfg({ toggleWindow: "Ctrl+Shift+J" }));
    expect(saved.ok).toBe(true);
    expect(saved.data.config.bindings.toggleWindow).toBe("Ctrl+Shift+J");
    expect((await s.call("app:set-shortcuts", cfg({ toggleWindow: "Ctrl+Alt+J" }))).ok).toBe(false);
  });

  it("saves a valid change, registers it with the operating system and reports it as active", async () => {
    s = setup(true);
    s.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    const result = await s.call("app:set-shortcuts", cfg({ toggleWindow: "shift+ctrl+j" }));
    expect(result.ok).toBe(true);
    expect(result.data.config.bindings.toggleWindow).toBe("Ctrl+Shift+J"); // canonical
    expect(result.data.status.toggleWindow.kind).toBe("active");
    expect(s.registered.has("Ctrl+Shift+J")).toBe(true);
    expect(s.registered.has(D.toggleWindow)).toBe(false);
    expect((await s.call("app:get-shortcuts")).data.config.bindings.toggleWindow).toBe("Ctrl+Shift+J");
  });

  it("persists across a restart of the settings store", async () => {
    s = setup(true);
    await s.call("app:set-shortcuts", cfg({ focusAudio: "Alt+Shift+K" }, false));
    const restarted = new SettingsStore(join(s.dir, "settings.json"));
    restarted.load();
    expect(restarted.shortcutConfig()).toEqual({ enabled: false, bindings: { ...D, focusAudio: "Alt+Shift+K" } });
    expect(JSON.parse(readFileSync(join(s.dir, "settings.json"), "utf8")).shortcuts.enabled).toBe(false);
  });

  it("turning shortcuts off unregisters them all", async () => {
    s = setup(true);
    s.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    expect(s.registered.size).toBe(4);
    const result = await s.call("app:set-shortcuts", cfg({}, false));
    expect(result.ok).toBe(true);
    expect(s.registered.size).toBe(0);
    expect(result.data.status.toggleWindow.kind).toBe("disabled");
  });

  it("rejects invalid, duplicate, incomplete and malformed input without saving or registering anything", async () => {
    s = setup(true);
    s.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    s.apply.mockClear();
    const before = s.store.shortcutConfig();
    const incomplete = cfg();
    delete (incomplete.bindings as Partial<typeof D>).hideWindow;
    for (const bad of [
      cfg({ toggleWindow: "Ctrl+Alt+A" }),
      cfg({ toggleWindow: "A" }),
      cfg({ toggleWindow: "Super+Shift+A" }),
      cfg({ hideWindow: D.toggleWindow }),
      incomplete,
      { ...cfg(), extra: true },
      { enabled: "yes", bindings: D },
      { enabled: true, bindings: { ...D, startAudio: "Alt+Shift+X" } },
      null, undefined, "x", 5, [], {},
    ]) {
      expect((await s.call("app:set-shortcuts", bad)).ok, JSON.stringify(bad)).toBe(false);
    }
    expect(s.apply).not.toHaveBeenCalled();
    expect(s.store.shortcutConfig()).toEqual(before);
    expect([...s.registered.keys()].sort()).toEqual(Object.values(D).sort());
  });

  it("surfaces a conflict with another application in the returned state", async () => {
    s = setup(true, ["Ctrl+Shift+J"]);
    const result = await s.call("app:set-shortcuts", cfg({ toggleWindow: "Ctrl+Shift+J" }));
    expect(result.ok).toBe(true); // saved: the preference is valid
    expect(result.data.status.toggleWindow.kind).toBe("unavailable");
    expect(result.data.status.hideWindow.kind).toBe("active");
  });

  it("refuses an untrusted sender on both channels", async () => {
    s = setup(true);
    for (const channel of ["app:get-shortcuts", "app:set-shortcuts"]) {
      expect(await s.handlers.get(channel)?.(s.untrusted, cfg())).toMatchObject({ ok: false });
    }
    expect(s.store.shortcutConfig()).toEqual(DEFAULT_SHORTCUT_CONFIG);
  });
});

describe("shortcuts never reach capture, audio, consent or acknowledgement", () => {
  let s: ReturnType<typeof setup>;
  beforeEach(() => {
    s = setup(true);
  });
  afterEach(() => s.cleanup());

  it("pressing every shortcut touches only the window: no capture, no listening, no indicator", () => {
    s.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    vi.useFakeTimers();
    try {
      for (const action of SHORTCUT_ACTIONS) {
        s.registered.get(D[action])?.();
        vi.advanceTimersByTime(400);
      }
    } finally {
      vi.useRealTimers();
    }
    expect(s.captureDisplay).not.toHaveBeenCalled();
    expect(s.captureIndicator).not.toHaveBeenCalled();
    expect(s.audioIndicator).not.toHaveBeenCalled();
    // The panel shortcuts only told the renderer to move; the channel carries a panel name, nothing else.
    const sent = s.win.webContents.send.mock.calls.filter(([channel]) => channel === "shortcut:event");
    expect(sent.map(([, payload]) => payload)).toEqual([{ panel: "audio" }, { panel: "screenshot" }]);
  });

  it("starting audio or capture still needs the existing channels, which stay refused without a signed-in user", async () => {
    // No shortcut exists for these; the named channels remain the only way and keep their own gates.
    for (const [channel, args] of [
      ["audio:start", ["microphone", "11111111-2222-4333-8444-555555555555"]],
      ["capture:start", ["1", "11111111-2222-4333-8444-555555555555"]],
    ] as const) {
      expect((await s.call(channel, ...args)).ok).toBe(false);
    }
    expect(s.captureDisplay).not.toHaveBeenCalled();
    expect(s.audioIndicator).not.toHaveBeenCalled();
  });
});
