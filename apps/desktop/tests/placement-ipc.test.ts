/**
 * M-6: window placement persistence in settings.json and the display channels through the real
 * registerDesktopIpc (fake ipcMain/window). Mocked Electron: wiring and validation, not real Windows.
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DeepLinkInbox } from "../src/main/deep-link";
import { registerDesktopIpc } from "../src/main/ipc";
import { SettingsStore } from "../src/main/settings-store";
import { DEFAULT_SHORTCUT_CONFIG } from "../src/shared/shortcuts";
import type { SavedPlacement } from "../src/main/window-placement";

const RENDERER = join(tmpdir(), "renderer-fixture", "index.html");
const TRUSTED = pathToFileURL(RENDERER).href;
type Handler = (event: unknown, ...args: unknown[]) => Promise<any>;
const USER = "00000000-0000-4000-8000-000000000001";

const placement = (x: number, y: number, id = 1): SavedPlacement => ({
  x, y, width: 390, height: 520, display: { id, x: 0, y: 0, width: 1920, height: 1080 },
});

describe("window placement in settings.json", () => {
  let dir: string;
  let file: string;
  const fresh = () => {
    dir = mkdtempSync(join(tmpdir(), "placement-"));
    file = join(dir, "settings.json");
  };
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it("starts empty, saves per mode, and survives a restart (application restart)", () => {
    fresh();
    const store = new SettingsStore(file);
    expect(store.windowPlacements()).toEqual({});
    store.setWindowPlacement("compact", placement(100, 120));
    store.setWindowPlacement("expanded", { ...placement(300, 40), width: 800, height: 760 });
    const restarted = new SettingsStore(file);
    restarted.load();
    expect(restarted.windowPlacements().compact).toEqual(placement(100, 120));
    expect(restarted.windowPlacements().expanded).toMatchObject({ x: 300, y: 40, width: 800 });
  });

  it("writes the section only when something was placed, so the file is otherwise unchanged", () => {
    fresh();
    const store = new SettingsStore(file);
    store.update({ alwaysOnTop: false });
    expect("windowPlacement" in JSON.parse(readFileSync(file, "utf8"))).toBe(false);
    store.setWindowPlacement("compact", placement(10, 10));
    expect(Object.keys(JSON.parse(readFileSync(file, "utf8")).windowPlacement)).toEqual(["compact"]);
  });

  it("stores only coordinates, sizes and the display's geometry: nothing else", () => {
    fresh();
    new SettingsStore(file).setWindowPlacement("compact", placement(10, 10, 7));
    const saved = JSON.parse(readFileSync(file, "utf8")).windowPlacement.compact;
    expect(Object.keys(saved).sort()).toEqual(["display", "height", "width", "x", "y"]);
    expect(Object.keys(saved.display).sort()).toEqual(["height", "id", "width", "x", "y"]);
  });

  it("refuses an invalid placement and keeps the old one", () => {
    fresh();
    const store = new SettingsStore(file);
    store.setWindowPlacement("compact", placement(100, 100));
    for (const bad of [null, "x", { ...placement(1, 1), x: -32000, y: -32000 }, { ...placement(1, 1), width: 3 }, { ...placement(1, 1), display: null }]) {
      expect(() => store.setWindowPlacement("compact", bad)).toThrow();
    }
    expect(() => store.setWindowPlacement("sideways" as never, placement(1, 1))).toThrow();
    expect(store.windowPlacements().compact).toEqual(placement(100, 100));
  });

  it("invalid saved coordinates in the file are dropped and the settings still load", () => {
    fresh();
    const base = { backendBaseUrl: "http://127.0.0.1:8000", alwaysOnTop: true, opacity: 0.9, expanded: false };
    writeFileSync(
      file,
      JSON.stringify({
        ...base,
        windowPlacement: { compact: { ...placement(1, 1), x: -32000, y: -32000 }, expanded: placement(50, 60) },
      }),
    );
    const store = new SettingsStore(file);
    expect(store.load().opacity).toBe(0.9);
    expect(store.windowPlacements().compact).toBeUndefined(); // the minimised-window sentinel is never trusted
    expect(store.windowPlacements().expanded).toEqual(placement(50, 60));
    writeFileSync(file, JSON.stringify({ ...base, windowPlacement: "junk" }));
    const junk = new SettingsStore(file);
    junk.load();
    expect(junk.windowPlacements()).toEqual({});
  });

  it("a file from before this feature loads with no placement", () => {
    fresh();
    writeFileSync(file, JSON.stringify({ backendBaseUrl: "http://127.0.0.1:8000", alwaysOnTop: true, opacity: 0.9, expanded: true }));
    const store = new SettingsStore(file);
    expect(store.load().expanded).toBe(true);
    expect(store.windowPlacements()).toEqual({});
  });

  it("coexists with the acknowledgement and the shortcuts: no write erases another section", () => {
    fresh();
    const store = new SettingsStore(file);
    store.acknowledge(USER, "2026-11-18T09:00:00.000Z");
    store.setShortcuts({ ...DEFAULT_SHORTCUT_CONFIG, bindings: { ...DEFAULT_SHORTCUT_CONFIG.bindings, toggleWindow: "Ctrl+Shift+J" } });
    store.setWindowPlacement("compact", placement(5, 5));
    store.update({ expanded: true });
    store.acknowledge(USER, "2026-11-19T09:00:00.000Z");
    const reloaded = new SettingsStore(file);
    reloaded.load();
    expect(reloaded.isAcknowledged(USER)).toBe(true);
    expect(reloaded.shortcutConfig().bindings.toggleWindow).toBe("Ctrl+Shift+J");
    expect(reloaded.windowPlacements().compact).toEqual(placement(5, 5));
    expect(reloaded.current().expanded).toBe(true);
  });
});

// ------------------------------------------------------------------ IPC channels

function setup(withPlacement: boolean) {
  const dir = mkdtempSync(join(tmpdir(), "placement-ipc-"));
  const handlers = new Map<string, Handler>();
  const win = {
    isDestroyed: () => false, isVisible: () => true, isFocused: () => true, isMinimized: () => false,
    minimize: vi.fn(), hide: vi.fn(), show: vi.fn(), restore: vi.fn(), focus: vi.fn(),
    setOpacity: vi.fn(), setAlwaysOnTop: vi.fn(), setMinimumSize: vi.fn(), setSize: vi.fn(), setBounds: vi.fn(),
    getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
    on: vi.fn(), removeListener: vi.fn(),
    webContents: { id: 1, send: vi.fn(), on: vi.fn(), once: vi.fn(), removeListener: vi.fn() },
  };
  const fake = {
    setMode: vi.fn(),
    moveToDisplay: vi.fn((id: number) => {
      if (id !== 1 && id !== 2) throw new Error("That display is no longer available.");
    }),
    listDisplays: vi.fn(() => [
      { id: 1, label: "Display 1 (primary) · 1920×1080 · 100% · at 0, 0", primary: true, current: true, width: 1920, height: 1080, scalePercent: 100 },
      { id: 2, label: "Display 2 · 2560×1440 · 150% · at 1920, 0", primary: false, current: false, width: 2560, height: 1440, scalePercent: 150 },
    ]),
  };
  const store = new SettingsStore(join(dir, "settings.json"));
  const dispose = registerDesktopIpc({
    ipcMain: { handle: (c: string, f: Handler) => handlers.set(c, f), removeHandler: (c: string) => handlers.delete(c), on: vi.fn(), removeListener: vi.fn() } as never,
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
    ...(withPlacement ? { placement: fake } : {}),
  });
  const trusted = { senderFrame: { url: TRUSTED }, sender: { id: 1 } };
  const untrusted = { senderFrame: { url: "https://evil.example/" }, sender: { id: 2 } };
  const call = (channel: string, ...args: unknown[]) => handlers.get(channel)!(trusted, ...args);
  return { handlers, win, fake, call, trusted, untrusted, cleanup: () => { dispose(); rmSync(dir, { recursive: true, force: true }); } };
}

let s: ReturnType<typeof setup>;
afterEach(() => s?.cleanup());

describe("display channels", () => {
  it("lists displays with which one holds the assistant", async () => {
    s = setup(true);
    const result = await s.call("app:get-displays");
    expect(result.ok).toBe(true);
    expect(result.data.displays).toHaveLength(2);
    expect(result.data.displays.filter((d: { current: boolean }) => d.current)).toHaveLength(1);
  });

  it("moves to a chosen display and returns the refreshed list", async () => {
    s = setup(true);
    const result = await s.call("app:move-to-display", 2);
    expect(result.ok).toBe(true);
    expect(s.fake.moveToDisplay).toHaveBeenCalledWith(2);
    expect(result.data.displays).toHaveLength(2);
  });

  it("rejects invalid ids without moving anything", async () => {
    s = setup(true);
    for (const bad of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, "2", null, undefined, {}, [2], true]) {
      expect((await s.call("app:move-to-display", bad)).ok, String(bad)).toBe(false);
    }
    expect(s.fake.moveToDisplay).not.toHaveBeenCalled();
  });

  it("reports a display that was just unplugged as a safe error", async () => {
    s = setup(true);
    const result = await s.call("app:move-to-display", 9);
    expect(result).toMatchObject({ ok: false });
    expect(JSON.stringify(result)).toContain("no longer available");
  });

  it("refuses an untrusted sender on both channels", async () => {
    s = setup(true);
    for (const channel of ["app:get-displays", "app:move-to-display"]) {
      expect(await s.handlers.get(channel)!(s.untrusted, 1)).toMatchObject({ ok: false });
    }
    expect(s.fake.moveToDisplay).not.toHaveBeenCalled();
  });

  it("without a placement manager the list is empty and moving is refused", async () => {
    s = setup(false);
    expect((await s.call("app:get-displays")).data).toEqual({ displays: [] });
    expect((await s.call("app:move-to-display", 1)).ok).toBe(false);
  });
});

describe("compact and expanded routing", () => {
  it("app:set-expanded goes through the placement manager when present", async () => {
    s = setup(true);
    expect((await s.call("app:set-expanded", true)).ok).toBe(true);
    expect(s.fake.setMode).toHaveBeenCalledWith(true);
    expect(s.win.setSize).not.toHaveBeenCalled(); // the old fixed resize is not used
    await s.call("app:set-expanded", false);
    expect(s.fake.setMode).toHaveBeenLastCalledWith(false);
  });

  it("changing expanded through the settings channel is routed the same way", async () => {
    s = setup(true);
    await s.call("app:update-settings", { expanded: true });
    expect(s.fake.setMode).toHaveBeenCalledWith(true);
  });

  it("without a placement manager it falls back to the previous fixed sizes", async () => {
    s = setup(false);
    expect((await s.call("app:set-expanded", true)).ok).toBe(true);
    expect(s.win.setSize).toHaveBeenCalledWith(760, 740, true);
  });

  it("rejects a non-boolean expanded value", async () => {
    s = setup(true);
    expect((await s.call("app:set-expanded", "yes")).ok).toBe(false);
    expect(s.fake.setMode).not.toHaveBeenCalled();
  });

  it("always-on-top and opacity are applied exactly as before (placement never touches them)", async () => {
    s = setup(true);
    await s.call("app:update-settings", { alwaysOnTop: false, opacity: 0.8, expanded: true });
    expect(s.win.setAlwaysOnTop).toHaveBeenLastCalledWith(false);
    expect(s.win.setOpacity).toHaveBeenLastCalledWith(0.8);
    expect(s.fake.setMode).toHaveBeenCalledWith(true);
  });
});
