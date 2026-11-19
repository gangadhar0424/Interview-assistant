/**
 * M-4 keyboard shortcuts: validation, the ShortcutManager (driven with fake ports), persistence and
 * the renderer store. These are mocked tests: they prove OUR logic, not how Windows behaves. The real
 * registrar, focus rules and conflicts with other applications need the manual checks in
 * docs/DESKTOP_SHORTCUTS.md.
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SHORTCUT_COOLDOWN_MS,
  ShortcutManager,
  type ShortcutRegistrar,
  type WindowActions,
} from "../src/main/shortcuts";
import { SettingsStore } from "../src/main/settings-store";
import {
  DEFAULT_SHORTCUT_CONFIG,
  DEFAULT_SHORTCUTS,
  SHORTCUT_ACTIONS,
  acceleratorFromKeyEvent,
  normalizeAccelerator,
  validateShortcutConfig,
  type ShortcutConfig,
} from "../src/shared/shortcuts";
import type { DesktopResult } from "../src/shared/contracts";
import type { ShortcutState } from "../src/shared/shortcuts";
import { useDesktopStore } from "../src/renderer/src/store";

describe("normalizeAccelerator", () => {
  it("accepts Shift plus exactly one of Ctrl/Alt plus one key, in any case and order, and canonicalises", () => {
    expect(normalizeAccelerator("Alt+Shift+A")).toBe("Alt+Shift+A");
    expect(normalizeAccelerator("shift+alt+a")).toBe("Alt+Shift+A");
    expect(normalizeAccelerator("Ctrl+Shift+7")).toBe("Ctrl+Shift+7");
    expect(normalizeAccelerator("Control+Shift+f9")).toBe("Ctrl+Shift+F9");
    expect(normalizeAccelerator("CommandOrControl+Shift+Space")).toBe("Ctrl+Shift+Space");
    expect(normalizeAccelerator("CmdOrCtrl+Shift+PageDown")).toBe("Ctrl+Shift+PageDown");
    expect(normalizeAccelerator("Alt+Shift+up")).toBe("Alt+Shift+Up");
  });

  it("refuses everything else", () => {
    for (const bad of [
      "", "A", "Shift+A", "Alt+A", "Ctrl+A", "Ctrl+C", "Alt+F4", // too few modifiers
      "Ctrl+Alt+A", "Ctrl+Alt+Shift+A", // Ctrl+Alt is AltGr on many layouts
      "Super+Shift+A", "Meta+Shift+A", "Cmd+Shift+A", "Command+Shift+A", "Win+Shift+A", // system key
      "Alt+Shift", "Alt+Shift+", "+Alt+Shift+A", "Alt++Shift+A", // no key / empty parts
      "Alt+Shift+Alt+A", "Shift+Shift+Alt+A", // repeated modifiers
      "Alt+Shift+AB", "Alt+Shift+F13", "Alt+Shift+Escape", "Alt+Shift+Tab", "Alt+Shift+Delete", "Alt+Shift+Enter", // unknown keys
      "Alt+Shift+A ", " Alt+Shift+A", "Alt+Shift+A;rm", "Alt-Shift-A", "Alt+Shift+Ä", // stray characters
      "A".repeat(41),
    ]) {
      expect(normalizeAccelerator(bad), JSON.stringify(bad)).toBeNull();
    }
    for (const bad of [null, undefined, 1, true, {}, [], ["Alt+Shift+A"]]) {
      expect(normalizeAccelerator(bad)).toBeNull();
    }
  });

  it("the defaults are all valid, unique and already canonical", () => {
    const all = SHORTCUT_ACTIONS.map((a) => DEFAULT_SHORTCUTS[a]);
    expect(new Set(all).size).toBe(all.length);
    for (const accelerator of all) expect(normalizeAccelerator(accelerator)).toBe(accelerator);
  });
});

describe("validateShortcutConfig", () => {
  const good = (): ShortcutConfig => structuredClone(DEFAULT_SHORTCUT_CONFIG);

  it("accepts a complete valid configuration and canonicalises it", () => {
    const config = good();
    config.bindings.toggleWindow = "shift+ctrl+q";
    expect(validateShortcutConfig(config).bindings.toggleWindow).toBe("Ctrl+Shift+Q");
  });

  it("rejects bad shapes, unknown keys and unknown actions", () => {
    for (const bad of [null, undefined, "x", 1, [], {}, { enabled: true }, { enabled: "yes", bindings: {} }]) {
      expect(() => validateShortcutConfig(bad)).toThrow();
    }
    expect(() => validateShortcutConfig({ ...good(), extra: 1 })).toThrow(/Unsupported/);
    expect(() => validateShortcutConfig({ enabled: true, bindings: { ...good().bindings, startAudio: "Alt+Shift+X" } })).toThrow(
      /Unsupported/,
    );
  });

  it("rejects a missing binding, an invalid one and a duplicate, with a message naming the action", () => {
    const missing = good();
    delete (missing.bindings as Partial<Record<string, string>>).hideWindow;
    expect(() => validateShortcutConfig(missing)).toThrow(/Hide the assistant/);
    const invalid = good();
    invalid.bindings.focusAudio = "Ctrl+Alt+L";
    expect(() => validateShortcutConfig(invalid)).toThrow(/Go to audio input/);
    const dup = good();
    dup.bindings.focusScreenshot = "shift+alt+a"; // same as toggleWindow once normalised
    expect(() => validateShortcutConfig(dup)).toThrow(/same shortcut/);
  });

  it("has no way to name a capture action", () => {
    expect([...SHORTCUT_ACTIONS].sort()).toEqual(["focusAudio", "focusScreenshot", "hideWindow", "toggleWindow"]);
  });
});

describe("acceleratorFromKeyEvent", () => {
  const ev = (code: string, mods: Partial<{ ctrlKey: boolean; altKey: boolean; shiftKey: boolean }> = {}) => ({
    key: "", code, ctrlKey: false, altKey: false, shiftKey: false, ...mods,
  });
  it("builds accelerators from key events and ignores modifier-only or unsupported keys", () => {
    expect(acceleratorFromKeyEvent(ev("KeyL", { altKey: true, shiftKey: true }))).toBe("Alt+Shift+L");
    expect(acceleratorFromKeyEvent(ev("Digit5", { ctrlKey: true, shiftKey: true }))).toBe("Ctrl+Shift+5");
    expect(acceleratorFromKeyEvent(ev("F9", { ctrlKey: true, shiftKey: true }))).toBe("Ctrl+Shift+F9");
    expect(acceleratorFromKeyEvent(ev("ArrowUp", { altKey: true, shiftKey: true }))).toBe("Alt+Shift+Up");
    expect(acceleratorFromKeyEvent(ev("ShiftLeft", { shiftKey: true }))).toBeNull();
    expect(acceleratorFromKeyEvent(ev("Escape"))).toBeNull();
    expect(acceleratorFromKeyEvent(ev("Enter", { altKey: true }))).toBeNull();
  });
});

// ------------------------------------------------------------------ ShortcutManager

function harness(options: { refuse?: string[]; throwOn?: string[]; foreign?: string[] } = {}) {
  const registered = new Map<string, () => void>(); // everything the OS "holds", incl. other apps'
  for (const accelerator of options.foreign ?? []) registered.set(accelerator, () => undefined);
  const registerCalls: string[] = [];
  const unregisterCalls: string[] = [];
  const registrar: ShortcutRegistrar = {
    register: (accelerator, callback) => {
      registerCalls.push(accelerator);
      if (options.throwOn?.includes(accelerator)) throw new Error("os failure");
      if (options.refuse?.includes(accelerator) || registered.has(accelerator)) return false;
      registered.set(accelerator, callback);
      return true;
    },
    unregister: (accelerator) => {
      unregisterCalls.push(accelerator);
      registered.delete(accelerator);
    },
  };
  const win = { visible: false, focused: false, minimized: false, throwOnShow: false };
  const calls: string[] = [];
  const window: WindowActions = {
    isVisible: () => win.visible,
    isFocused: () => win.focused,
    isMinimized: () => win.minimized,
    restore: () => {
      calls.push("restore");
      win.minimized = false;
    },
    show: () => {
      if (win.throwOnShow) throw new Error("destroyed");
      calls.push("show");
      win.visible = true;
    },
    focus: () => {
      calls.push("focus");
      win.focused = true;
    },
    hide: () => {
      calls.push("hide");
      win.visible = false;
      win.focused = false;
    },
    showPanel: (panel) => calls.push(`panel:${panel}`),
  };
  let now = 1_000;
  const manager = new ShortcutManager({ registrar, window, now: () => now });
  const press = (accelerator: string) => registered.get(accelerator)?.();
  return {
    manager, registered, registerCalls, unregisterCalls, win, calls, press,
    advance: (ms: number) => (now += ms),
  };
}

const D = DEFAULT_SHORTCUTS;
const config = (over: Partial<ShortcutConfig["bindings"]> = {}, enabled = true): ShortcutConfig => ({
  enabled,
  bindings: { ...D, ...over },
});

describe("ShortcutManager: registration, startup and shutdown", () => {
  it("registers nothing until applied (application startup)", () => {
    const h = harness();
    expect(h.registerCalls).toEqual([]);
    expect(Object.values(h.manager.status()).every((s) => s.kind === "disabled")).toBe(true);
  });

  it("registers the four defaults and reports each as active", () => {
    const h = harness();
    const status = h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    expect([...h.registered.keys()].sort()).toEqual(Object.values(D).sort());
    for (const action of SHORTCUT_ACTIONS) expect(status[action].kind).toBe("active");
  });

  it("registers nothing when shortcuts are turned off, and removes what was registered", () => {
    const h = harness();
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    const status = h.manager.apply(config({}, false));
    expect(h.registered.size).toBe(0);
    for (const action of SHORTCUT_ACTIONS) expect(status[action].kind).toBe("disabled");
  });

  it("re-applying replaces only its own registrations and never touches another application's", () => {
    const h = harness({ foreign: ["Ctrl+Shift+Q"] });
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    h.manager.apply(config({ toggleWindow: "Ctrl+Shift+W" }));
    expect(h.registered.has("Alt+Shift+A")).toBe(false);
    expect(h.registered.has("Ctrl+Shift+W")).toBe(true);
    expect(h.registered.has("Ctrl+Shift+Q")).toBe(true); // foreign: untouched
    expect(h.unregisterCalls).not.toContain("Ctrl+Shift+Q");
  });

  it("dispose unregisters everything it registered, is idempotent, and later applies stay off (shutdown)", () => {
    const h = harness({ foreign: ["Ctrl+Shift+Q"] });
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    h.manager.dispose();
    h.manager.dispose();
    expect([...h.registered.keys()]).toEqual(["Ctrl+Shift+Q"]);
    const status = h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    expect(h.registered.has("Alt+Shift+A")).toBe(false);
    expect(status.toggleWindow.kind).toBe("disabled");
  });
});

describe("ShortcutManager: conflicts, invalid and duplicate definitions", () => {
  it("reports a combination another application owns as unavailable and keeps the rest working", () => {
    const h = harness({ foreign: [D.hideWindow] });
    const status = h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    expect(status.hideWindow.kind).toBe("unavailable");
    expect(status.hideWindow.message).toMatch(/another application/i);
    expect(status.toggleWindow.kind).toBe("active");
    expect(status.focusAudio.kind).toBe("active");
    expect(status.focusScreenshot.kind).toBe("active");
  });

  it("treats a registrar that refuses or throws as unavailable, never as a crash", () => {
    const h = harness({ refuse: [D.focusAudio], throwOn: [D.focusScreenshot] });
    let status = h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    expect(status.focusAudio.kind).toBe("unavailable");
    expect(status.focusScreenshot.kind).toBe("unavailable");
    expect(status.toggleWindow.kind).toBe("active");
    h.registered.delete(D.focusAudio);
    status = h.manager.apply(DEFAULT_SHORTCUT_CONFIG); // a later retry can succeed
    expect(h.registered.has(D.toggleWindow)).toBe(true);
    expect(status.toggleWindow.kind).toBe("active");
  });

  it("never registers an invalid definition (even if it was stored that way)", () => {
    const h = harness();
    const status = h.manager.apply(config({ toggleWindow: "Ctrl+Alt+A", hideWindow: "Q" }));
    expect(status.toggleWindow.kind).toBe("invalid");
    expect(status.hideWindow.kind).toBe("invalid");
    expect(h.registerCalls).not.toContain("Ctrl+Alt+A");
    expect(h.registerCalls).not.toContain("Q");
    expect(status.focusAudio.kind).toBe("active");
  });

  it("registers a duplicate once: the first action wins and the second is reported as a duplicate", () => {
    const h = harness();
    const status = h.manager.apply(config({ hideWindow: "shift+alt+a" })); // same as toggleWindow
    expect(status.toggleWindow.kind).toBe("active");
    expect(status.hideWindow.kind).toBe("duplicate");
    expect(h.registerCalls.filter((a) => a === "Alt+Shift+A")).toHaveLength(1);
  });
});

describe("ShortcutManager: what the actions do to the window", () => {
  it("toggle shows and focuses a hidden window, and hides one that is in front", () => {
    const h = harness();
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    h.press(D.toggleWindow);
    expect(h.calls).toEqual(["show", "focus"]);
    h.advance(SHORTCUT_COOLDOWN_MS + 1);
    h.press(D.toggleWindow);
    expect(h.calls.slice(2)).toEqual(["hide"]);
  });

  it("toggle brings a visible but unfocused window forward instead of hiding it, and restores a minimised one", () => {
    const h = harness();
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    h.win.visible = true;
    h.press(D.toggleWindow);
    expect(h.calls).toEqual(["show", "focus"]);
    h.advance(SHORTCUT_COOLDOWN_MS + 1);
    h.win.visible = true;
    h.win.focused = false;
    h.win.minimized = true;
    h.press(D.toggleWindow);
    expect(h.calls.slice(2)).toEqual(["restore", "show", "focus"]);
  });

  it("hide only hides a visible window", () => {
    const h = harness();
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    h.press(D.hideWindow);
    expect(h.calls).toEqual([]);
    h.win.visible = true;
    h.advance(SHORTCUT_COOLDOWN_MS + 1);
    h.press(D.hideWindow);
    expect(h.calls).toEqual(["hide"]);
  });

  it("the panel shortcuts show and focus the window and only ask the renderer to move to the panel", () => {
    const h = harness();
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    h.press(D.focusAudio);
    h.press(D.focusScreenshot);
    expect(h.calls).toEqual(["show", "focus", "panel:audio", "show", "focus", "panel:screenshot"]);
  });

  it("keeps working after the window is hidden and shown again, and while it is hidden", () => {
    const h = harness();
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    for (let round = 0; round < 3; round += 1) {
      h.advance(SHORTCUT_COOLDOWN_MS + 1);
      h.press(D.toggleWindow); // show
      expect(h.win.visible).toBe(true);
      h.advance(SHORTCUT_COOLDOWN_MS + 1);
      h.press(D.toggleWindow); // hide
      expect(h.win.visible).toBe(false);
    }
    h.advance(SHORTCUT_COOLDOWN_MS + 1);
    h.press(D.focusAudio); // from the tray
    expect(h.win.visible && h.win.focused).toBe(true);
    expect(h.registered.size).toBe(4); // still registered throughout
  });

  it("ignores a repeat within the cooldown and allows it afterwards", () => {
    const h = harness();
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    h.press(D.focusAudio);
    h.press(D.focusAudio);
    h.advance(SHORTCUT_COOLDOWN_MS - 1);
    h.press(D.focusAudio);
    expect(h.calls.filter((c) => c === "panel:audio")).toHaveLength(1);
    h.advance(2);
    h.press(D.focusAudio);
    expect(h.calls.filter((c) => c === "panel:audio")).toHaveLength(2);
  });

  it("never throws into the operating system when the window is gone", () => {
    const h = harness();
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    h.win.throwOnShow = true;
    expect(() => h.press(D.toggleWindow)).not.toThrow();
  });

  it("does nothing after shutdown even if the operating system still delivers the key", () => {
    const h = harness();
    h.manager.apply(DEFAULT_SHORTCUT_CONFIG);
    const stale = h.registered.get(D.toggleWindow);
    h.manager.dispose();
    stale?.();
    expect(h.calls).toEqual([]);
  });
});

// ------------------------------------------------------------------ persistence

describe("shortcut settings persistence", () => {
  let dir: string;
  let file: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "shortcuts-"));
    file = join(dir, "settings.json");
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it("starts with the defaults, on", () => {
    expect(new SettingsStore(file).shortcutConfig()).toEqual(DEFAULT_SHORTCUT_CONFIG);
  });

  it("saves a change, survives a restart, and canonicalises what it stores", () => {
    const store = new SettingsStore(file);
    store.setShortcuts({ enabled: false, bindings: { ...D, toggleWindow: "shift+ctrl+j" } });
    const restarted = new SettingsStore(file);
    restarted.load();
    expect(restarted.shortcutConfig()).toEqual({ enabled: false, bindings: { ...D, toggleWindow: "Ctrl+Shift+J" } });
  });

  it("refuses an invalid or duplicate configuration and leaves the saved one unchanged", () => {
    const store = new SettingsStore(file);
    store.setShortcuts(config({ toggleWindow: "Ctrl+Shift+J" }));
    expect(() => store.setShortcuts(config({ toggleWindow: "Ctrl+Alt+J" }))).toThrow();
    expect(() => store.setShortcuts(config({ hideWindow: D.focusAudio }))).toThrow(/same shortcut/);
    expect(store.shortcutConfig().bindings.toggleWindow).toBe("Ctrl+Shift+J");
    const again = new SettingsStore(file);
    again.load();
    expect(again.shortcutConfig().bindings.toggleWindow).toBe("Ctrl+Shift+J");
  });

  it("writes the section only when it differs from the defaults, so the file is otherwise unchanged", () => {
    const store = new SettingsStore(file);
    store.update({ alwaysOnTop: false });
    expect(Object.keys(JSON.parse(readFileSync(file, "utf8"))).sort()).toEqual(
      ["alwaysOnTop", "audioInputEnabled", "backendBaseUrl", "expanded", "opacity", "screenshotInputEnabled"],
    );
    store.setShortcuts(config({ toggleWindow: "Ctrl+Shift+J" }));
    expect(JSON.parse(readFileSync(file, "utf8")).shortcuts.bindings.toggleWindow).toBe("Ctrl+Shift+J");
    store.setShortcuts(DEFAULT_SHORTCUT_CONFIG);
    expect("shortcuts" in JSON.parse(readFileSync(file, "utf8"))).toBe(false);
  });

  it("loads an older file without the section as the defaults, and repairs one bad binding on its own", () => {
    const base = { backendBaseUrl: "http://127.0.0.1:8000", alwaysOnTop: true, opacity: 0.9, expanded: false };
    writeFileSync(file, JSON.stringify(base));
    const legacy = new SettingsStore(file);
    legacy.load();
    expect(legacy.shortcutConfig()).toEqual(DEFAULT_SHORTCUT_CONFIG);

    writeFileSync(
      file,
      JSON.stringify({ ...base, shortcuts: { enabled: false, bindings: { toggleWindow: "Ctrl+Alt+Z", hideWindow: "Ctrl+Shift+Y" } } }),
    );
    const damaged = new SettingsStore(file);
    damaged.load();
    expect(damaged.shortcutConfig().enabled).toBe(false);
    expect(damaged.shortcutConfig().bindings.toggleWindow).toBe(D.toggleWindow); // fell back
    expect(damaged.shortcutConfig().bindings.hideWindow).toBe("Ctrl+Shift+Y"); // kept
    writeFileSync(file, JSON.stringify({ ...base, shortcuts: "nonsense" }));
    const junk = new SettingsStore(file);
    junk.load();
    expect(junk.shortcutConfig()).toEqual(DEFAULT_SHORTCUT_CONFIG);
  });

  it("coexists with the acceptable-use acknowledgement: neither write erases the other", () => {
    const user = "00000000-0000-4000-8000-000000000001";
    const store = new SettingsStore(file);
    store.acknowledge(user, "2026-11-18T09:00:00.000Z");
    store.setShortcuts(config({ toggleWindow: "Ctrl+Shift+J" }));
    store.update({ expanded: true });
    const reloaded = new SettingsStore(file);
    reloaded.load();
    expect(reloaded.isAcknowledged(user)).toBe(true);
    expect(reloaded.shortcutConfig().bindings.toggleWindow).toBe("Ctrl+Shift+J");
    reloaded.acknowledge(user, "2026-11-19T09:00:00.000Z");
    expect(reloaded.shortcutConfig().bindings.toggleWindow).toBe("Ctrl+Shift+J");
  });
});

// ------------------------------------------------------------------ renderer store

describe("renderer shortcut state", () => {
  const ok = <T,>(data: T): DesktopResult<T> => ({ ok: true, data });
  const state = (): ShortcutState => ({
    config: structuredClone(DEFAULT_SHORTCUT_CONFIG),
    defaults: { ...D },
    status: Object.fromEntries(SHORTCUT_ACTIONS.map((a) => [a, { kind: "active", message: "Active." }])) as ShortcutState["status"],
  });
  let desktop: {
    app: { getShortcuts: ReturnType<typeof vi.fn>; setShortcuts: ReturnType<typeof vi.fn> };
    audio: { start: ReturnType<typeof vi.fn> };
    capture: { start: ReturnType<typeof vi.fn>; listDisplays: ReturnType<typeof vi.fn> };
  };

  beforeEach(() => {
    desktop = {
      app: { getShortcuts: vi.fn(async () => ok(state())), setShortcuts: vi.fn(async () => ok(state())) },
      audio: { start: vi.fn() },
      capture: { start: vi.fn(), listDisplays: vi.fn() },
    };
    vi.stubGlobal("window", { desktop });
    useDesktopStore.setState({ shortcuts: null, shortcutsBusy: false, shortcutsNotice: null, panelFocus: null });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("loads, saves through main, and shows a safe message when main refuses", async () => {
    await useDesktopStore.getState().loadShortcuts();
    expect(useDesktopStore.getState().shortcuts?.config.enabled).toBe(true);
    expect(await useDesktopStore.getState().saveShortcuts(DEFAULT_SHORTCUT_CONFIG)).toBe(true);
    expect(desktop.app.setShortcuts).toHaveBeenCalledWith(DEFAULT_SHORTCUT_CONFIG);
    desktop.app.setShortcuts.mockResolvedValueOnce({
      ok: false,
      error: { kind: "unknown", message: "Those two actions use the same shortcut." },
    });
    expect(await useDesktopStore.getState().saveShortcuts(DEFAULT_SHORTCUT_CONFIG)).toBe(false);
    expect(useDesktopStore.getState().shortcutsNotice).toContain("same shortcut");
    expect(useDesktopStore.getState().shortcutsBusy).toBe(false);
  });

  it("asking to move to a panel only records the request: it starts no audio and no capture", () => {
    useDesktopStore.getState().requestPanelFocus("audio");
    const first = useDesktopStore.getState().panelFocus;
    useDesktopStore.getState().requestPanelFocus("screenshot");
    const second = useDesktopStore.getState().panelFocus;
    expect(first?.panel).toBe("audio");
    expect(second?.panel).toBe("screenshot");
    expect((second?.nonce ?? 0) > (first?.nonce ?? 0)).toBe(true);
    expect(desktop.audio.start).not.toHaveBeenCalled();
    expect(desktop.capture.start).not.toHaveBeenCalled();
    expect(desktop.capture.listDisplays).not.toHaveBeenCalled();
  });
});
