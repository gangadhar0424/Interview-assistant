/**
 * M-15: window capture privacy. Fake window and fake platform: these tests prove the manager makes
 * (or does not make) the `setContentProtection` call at the right moments and reports honestly.
 * They do NOT prove any capture tool is blocked; that is the manual matrix in
 * docs/WINDOW_CAPTURE_PRIVACY.md.
 */
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CapturePrivacyManager, type ProtectableWindow } from "../src/main/capture-privacy";
import { DeepLinkInbox } from "../src/main/deep-link";
import { registerDesktopIpc } from "../src/main/ipc";
import { SettingsStore } from "../src/main/settings-store";
import {
  CAPTURE_PRIVACY_CONSENT_VERSION,
  CAPTURE_PRIVACY_EXPLANATION,
  CAPTURE_PRIVACY_INDICATOR,
  CAPTURE_PRIVACY_MESSAGES,
  isCapturePrivacySupported,
  MIN_SUPPORTED_WINDOWS_BUILD,
  parseWindowsBuild,
} from "../src/shared/capture-privacy";

const WIN11 = "10.0.26300";
const AT_CONSENT = "2026-11-22T10:00:00.000Z";
const WIN10_2004 = "10.0.19041";
const WIN10_1909 = "10.0.18363";

class FakeWindow implements ProtectableWindow {
  destroyed = false;
  calls: boolean[] = [];
  throwOnTrue = false;
  throwOnFalse = false;
  /** When set, the read-back reports this instead of the last value set (a window that "lies"). */
  reportAs: boolean | null = null;
  isContentProtected() {
    return this.reportAs ?? this.protectedNow;
  }
  isDestroyed() {
    return this.destroyed;
  }
  setContentProtection(enabled: boolean) {
    if ((enabled && this.throwOnTrue) || (!enabled && this.throwOnFalse)) throw new Error("native failure");
    this.calls.push(enabled);
  }
  get protectedNow() {
    return this.calls.length > 0 && this.calls[this.calls.length - 1] === true;
  }
}

function memoryStore(initial = false) {
  let value = initial;
  let consent: { version: number; acceptedAt: string } | null = initial
    ? { version: CAPTURE_PRIVACY_CONSENT_VERSION, acceptedAt: "2026-11-22T00:00:00.000Z" }
    : null;
  return {
    capturePrivacyEnabled: () => value,
    capturePrivacyConsent: () => consent,
    setCapturePrivacy: vi.fn((next: boolean, given?: { version: number; acceptedAt: string }) => {
      value = next;
      consent = next ? given ?? null : null;
    }),
  };
}

function make(
  over: Partial<{
    platform: string;
    release: string;
    confirm: () => Promise<boolean>;
    initial: boolean;
    window: FakeWindow | null;
  }> = {},
) {
  const holder = { window: over.window === undefined ? new FakeWindow() : over.window };
  const store = memoryStore(over.initial ?? false);
  const confirm = vi.fn(over.confirm ?? (async () => true));
  const manager = new CapturePrivacyManager({
    getWindow: () => holder.window,
    store,
    platform: over.platform ?? "win32",
    osRelease: over.release ?? WIN11,
    confirm,
  });
  return { holder, store, confirm, manager };
}

describe("support detection", () => {
  it("parses Windows builds and rejects nonsense", () => {
    expect(parseWindowsBuild("10.0.26300")).toBe(26300);
    expect(parseWindowsBuild("10.0.19041")).toBe(19041);
    for (const bad of ["", "abc", "10.0", "6.1.7601", "10.0.-5", "10.0.x"]) expect(parseWindowsBuild(bad)).toBeNull();
  });

  it("supports Windows 10 2004 (19041) and later on win32 only", () => {
    expect(MIN_SUPPORTED_WINDOWS_BUILD).toBe(19041);
    expect(isCapturePrivacySupported("win32", WIN11)).toBe(true);
    expect(isCapturePrivacySupported("win32", WIN10_2004)).toBe(true);
    expect(isCapturePrivacySupported("win32", WIN10_1909)).toBe(false);
    expect(isCapturePrivacySupported("win32", "garbage")).toBe(false);
    expect(isCapturePrivacySupported("linux", WIN11)).toBe(false);
    expect(isCapturePrivacySupported("darwin", WIN11)).toBe(false);
  });
});

describe("default and explicit activation", () => {
  it("is off by default and makes no native call from construction, state() or reapply()", () => {
    const { manager, holder } = make();
    expect(manager.state()).toMatchObject({ enabled: false, status: "off", supported: true });
    manager.reapply();
    manager.reapply();
    expect(holder.window?.calls).toEqual([]);
  });

  it("refuses to turn on until the acceptable-use notice is accepted, without asking or calling Windows", async () => {
    const { manager, confirm, store, holder } = make();
    await expect(manager.enable(false)).rejects.toThrow(/acceptable-use/i);
    expect(confirm).not.toHaveBeenCalled();
    expect(store.setCapturePrivacy).not.toHaveBeenCalled();
    expect(holder.window?.calls).toEqual([]);
  });

  it("a declined confirmation changes nothing", async () => {
    const { manager, store, holder } = make({ confirm: async () => false });
    const state = await manager.enable(true);
    expect(state).toMatchObject({ enabled: false, status: "off" });
    expect(store.setCapturePrivacy).not.toHaveBeenCalled();
    expect(holder.window?.calls).toEqual([]);
  });

  it("turns on only after acknowledgement and confirmation, saves the choice, and reports 'requested' (never 'protected')", async () => {
    const { manager, store, holder } = make();
    const state = await manager.enable(true);
    expect(state).toMatchObject({ enabled: true, status: "requested" });
    expect(state.message).toBe(CAPTURE_PRIVACY_MESSAGES.requested);
    expect(state.message).toMatch(/not a guarantee/i);
    expect(store.setCapturePrivacy).toHaveBeenCalledWith(true, {
      version: CAPTURE_PRIVACY_CONSENT_VERSION,
      acceptedAt: expect.stringMatching(/^\d{4}-\d{2}-\d{2}T/),
    });
    expect(state.consent).toMatchObject({ version: CAPTURE_PRIVACY_CONSENT_VERSION });
    expect(holder.window?.protectedNow).toBe(true);
  });

  it("refuses a second confirmation while one is open", async () => {
    let release: (value: boolean) => void = () => undefined;
    const { manager } = make({ confirm: () => new Promise<boolean>((resolve) => (release = resolve)) });
    const first = manager.enable(true);
    await expect(manager.enable(true)).rejects.toThrow(/already open/i);
    release(false);
    await first;
  });

  it("turning on twice does not repeat the dialog", async () => {
    const { manager, confirm } = make();
    await manager.enable(true);
    await manager.enable(true);
    expect(confirm).toHaveBeenCalledTimes(1);
  });
});

describe("explicit deactivation", () => {
  it("saves off first, then asks Windows to stop, and reports off", async () => {
    const { manager, store, holder } = make();
    await manager.enable(true);
    const state = manager.disable();
    expect(state).toMatchObject({ enabled: false, status: "off" });
    expect(store.setCapturePrivacy).toHaveBeenLastCalledWith(false);
    expect(holder.window?.protectedNow).toBe(false);
  });

  it("stays off afterwards: later re-applies do nothing", async () => {
    const { manager, holder } = make();
    await manager.enable(true);
    manager.disable();
    const before = holder.window?.calls.length;
    manager.reapply();
    expect(holder.window?.calls.length).toBe(before);
  });

  it("when Windows cannot confirm removal it says so and still saves off", async () => {
    const { manager, store, holder } = make();
    await manager.enable(true);
    (holder.window as FakeWindow).throwOnFalse = true;
    const state = manager.disable();
    expect(state.enabled).toBe(false);
    expect(state.status).toBe("failed");
    expect(state.message).toMatch(/restart/i);
    expect(store.setCapturePrivacy).toHaveBeenLastCalledWith(false);
  });

  it("turning off with no window just saves off", () => {
    const { manager, store } = make({ window: null, initial: true });
    expect(manager.disable()).toMatchObject({ enabled: false, status: "off" });
    expect(store.setCapturePrivacy).toHaveBeenLastCalledWith(false);
  });
});

describe("window recreation, crash recovery, layout and display changes", () => {
  it("re-applies the saved choice to a recreated window, and a destroyed old window is never touched", async () => {
    const { manager, holder } = make();
    await manager.enable(true);
    const old = holder.window as FakeWindow;
    old.destroyed = true;
    const fresh = new FakeWindow();
    holder.window = fresh;
    expect(fresh.calls).toEqual([]);
    manager.reapply();
    expect(fresh.protectedNow).toBe(true);
    expect(old.calls).toEqual([true]);
  });

  it("does nothing when there is no window or it is destroyed", async () => {
    const { manager, holder } = make();
    await manager.enable(true);
    (holder.window as FakeWindow).destroyed = true;
    expect(() => manager.reapply()).not.toThrow();
    holder.window = null;
    expect(() => manager.reapply()).not.toThrow();
  });

  it("re-applies after a renderer crash and reload (did-finish-load) and again on show/restore", async () => {
    const { manager, holder } = make();
    await manager.enable(true);
    const win = holder.window as FakeWindow;
    win.calls.length = 0;
    for (let i = 0; i < 3; i += 1) manager.reapply(); // load, show, restore
    expect(win.calls).toEqual([true, true, true]);
    expect(manager.state().status).toBe("requested");
  });

  it("does not turn on after recovery or window recreation when the user never chose it", () => {
    const { manager, holder } = make();
    const fresh = new FakeWindow();
    holder.window = fresh;
    for (let i = 0; i < 5; i += 1) manager.reapply();
    expect(fresh.calls).toEqual([]);
    expect(manager.state().enabled).toBe(false);
  });

  it("holds across compact and expanded layouts and display changes (repeated, idempotent re-applies)", async () => {
    const { manager, holder } = make();
    await manager.enable(true);
    const win = holder.window as FakeWindow;
    for (const _event of ["expand", "display-added", "display-metrics-changed", "compact", "display-removed"]) {
      manager.reapply();
    }
    expect(win.calls.every((c) => c === true)).toBe(true);
    expect(manager.state()).toMatchObject({ enabled: true, status: "requested" });
  });
});

describe("unsupported platform and native failure", () => {
  it.each([
    ["not Windows", "linux", WIN11],
    ["older than Windows 10 2004", "win32", WIN10_1909],
    ["an unrecognised Windows release", "win32", "weird"],
  ])("%s: reports unsupported, never calls Windows, never saves on", async (_name, platform, release) => {
    const { manager, store, holder } = make({ platform, release });
    expect(manager.state()).toMatchObject({ enabled: false, status: "unsupported", supported: false });
    const state = await manager.enable(true);
    expect(state).toMatchObject({ enabled: false, status: "unsupported" });
    expect(state.message).toMatch(/Windows 10 version 2004/);
    manager.reapply();
    expect(holder.window?.calls).toEqual([]);
    expect(store.setCapturePrivacy).not.toHaveBeenCalled();
  });

  it("a saved 'on' on a device that cannot do it reports unsupported and calls nothing", () => {
    const { manager, holder } = make({ platform: "win32", release: WIN10_1909, initial: true });
    expect(manager.state().status).toBe("unsupported");
    manager.reapply();
    expect(holder.window?.calls).toEqual([]);
  });

  it("a native failure while turning on reports failed, saves nothing, and tries to remove protection", async () => {
    const { manager, store, holder } = make();
    (holder.window as FakeWindow).throwOnTrue = true;
    const state = await manager.enable(true);
    expect(state).toMatchObject({ enabled: false, status: "failed" });
    expect(state.message).toBe(CAPTURE_PRIVACY_MESSAGES.failed);
    expect(store.setCapturePrivacy).not.toHaveBeenCalled();
    expect(holder.window?.calls).toEqual([false]);
  });

  it("a native failure on re-apply is shown as failed, not as protected, and the choice is kept", async () => {
    const { manager, holder } = make();
    await manager.enable(true);
    (holder.window as FakeWindow).throwOnTrue = true;
    const state = manager.reapply();
    expect(state).toMatchObject({ enabled: true, status: "failed" });
    // and it recovers on the next successful re-apply
    (holder.window as FakeWindow).throwOnTrue = false;
    expect(manager.reapply().status).toBe("requested");
  });

  it("a window that reads back a different state than requested counts as failed, not as protected", async () => {
    const { manager, store, holder } = make();
    (holder.window as FakeWindow).reportAs = false;
    const state = await manager.enable(true);
    expect(state).toMatchObject({ enabled: false, status: "failed" });
    expect(store.setCapturePrivacy).not.toHaveBeenCalled();
  });

  it("a read-back mismatch on re-apply is shown as failed", async () => {
    const { manager, holder } = make();
    await manager.enable(true);
    (holder.window as FakeWindow).reportAs = false;
    expect(manager.reapply().status).toBe("failed");
  });

  it("a read-back mismatch when turning off is reported, with the setting saved as off", async () => {
    const { manager, store, holder } = make();
    await manager.enable(true);
    (holder.window as FakeWindow).reportAs = true;
    const state = manager.disable();
    expect(state).toMatchObject({ enabled: false, status: "failed" });
    expect(store.setCapturePrivacy).toHaveBeenLastCalledWith(false);
  });

  it("if the choice cannot be saved, protection is removed again and the error is raised", async () => {
    const { manager, store, holder } = make();
    store.setCapturePrivacy.mockImplementation(() => {
      throw new Error("disk full");
    });
    await expect(manager.enable(true)).rejects.toThrow("disk full");
    expect(holder.window?.protectedNow).toBe(false);
    expect(manager.state().status).toBe("off");
  });

  it("with no window yet, turning on fails safely", async () => {
    const { manager, store } = make({ window: null });
    expect(await manager.enable(true)).toMatchObject({ enabled: false, status: "failed" });
    expect(store.setCapturePrivacy).not.toHaveBeenCalled();
  });
});

describe("persistence and application restart (real settings file)", () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "capture-privacy-"));
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));
  const file = () => join(dir, "settings.json");

  it("a new or default settings file has no capture-privacy entry and loads as off", () => {
    const store = new SettingsStore(file());
    store.load();
    expect(store.capturePrivacyEnabled()).toBe(false);
    store.update({ opacity: 0.9 });
    expect(JSON.parse(readFileSync(file(), "utf8"))).not.toHaveProperty("capturePrivacy");
  });

  it("is saved and survives a restart; the new process re-applies it without any user action", async () => {
    const first = new SettingsStore(file());
    first.load();
    const win1 = new FakeWindow();
    const m1 = new CapturePrivacyManager({
      getWindow: () => win1, store: first, platform: "win32", osRelease: WIN11, confirm: async () => true,
    });
    await m1.enable(true);
    expect(JSON.parse(readFileSync(file(), "utf8")).capturePrivacy).toBe(true);

    // "restart": a fresh store and manager read the same file
    const second = new SettingsStore(file());
    second.load();
    expect(second.capturePrivacyEnabled()).toBe(true);
    const win2 = new FakeWindow();
    const confirm = vi.fn(async () => true);
    const m2 = new CapturePrivacyManager({
      getWindow: () => win2, store: second, platform: "win32", osRelease: WIN11, confirm,
    });
    expect(win2.calls).toEqual([]); // construction alone applies nothing
    m2.reapply();
    expect(win2.protectedNow).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
    expect(m2.state()).toMatchObject({ enabled: true, status: "requested" });
  });

  it("turning off is saved and survives a restart", async () => {
    const store = new SettingsStore(file());
    store.load();
    const m = new CapturePrivacyManager({
      getWindow: () => new FakeWindow(), store, platform: "win32", osRelease: WIN11, confirm: async () => true,
    });
    await m.enable(true);
    m.disable();
    const again = new SettingsStore(file());
    again.load();
    expect(again.capturePrivacyEnabled()).toBe(false);
    expect(JSON.parse(readFileSync(file(), "utf8"))).not.toHaveProperty("capturePrivacy");
  });

  it("only an exact boolean true counts: damaged values load as off", () => {
    const base = { backendBaseUrl: "http://127.0.0.1:8000", alwaysOnTop: true, opacity: 0.9, expanded: false };
    for (const bad of ["true", 1, "yes", null, {}, [true], "on"]) {
      writeFileSync(file(), JSON.stringify({ ...base, capturePrivacy: bad }));
      const store = new SettingsStore(file());
      store.load();
      expect(store.capturePrivacyEnabled()).toBe(false);
    }
  });

  it("other settings and saved sections are preserved when the choice changes", async () => {
    const store = new SettingsStore(file());
    store.load();
    store.update({ opacity: 0.8 });
    store.acknowledge("11111111-1111-4111-8111-111111111111", "2026-11-22T00:00:00.000Z");
    store.setCapturePrivacy(true, { version: CAPTURE_PRIVACY_CONSENT_VERSION, acceptedAt: AT_CONSENT });
    const stored = JSON.parse(readFileSync(file(), "utf8"));
    expect(stored.opacity).toBe(0.8);
    expect(stored.acceptableUse.userId).toBe("11111111-1111-4111-8111-111111111111");
    expect(stored.capturePrivacy).toBe(true);
    expect(() => store.setCapturePrivacy("yes" as never)).toThrow();
  });
});

describe("dedicated consent record (separate from the acceptable-use notice)", () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "capture-privacy-consent-"));
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));
  const file = () => join(dir, "settings.json");
  const base = { backendBaseUrl: "http://127.0.0.1:8000", alwaysOnTop: true, opacity: 0.9, expanded: false };
  const consent = { version: CAPTURE_PRIVACY_CONSENT_VERSION, acceptedAt: AT_CONSENT };

  it("turning on records the wording version and time, and survives a restart", async () => {
    const store = new SettingsStore(file());
    store.load();
    const m = new CapturePrivacyManager({
      getWindow: () => new FakeWindow(), store, platform: "win32", osRelease: WIN11,
      confirm: async () => true, now: () => AT_CONSENT,
    });
    const state = await m.enable(true);
    expect(state.consent).toEqual(consent);
    const stored = JSON.parse(readFileSync(file(), "utf8"));
    expect(stored.capturePrivacyConsent).toEqual(consent);
    const again = new SettingsStore(file());
    again.load();
    expect(again.capturePrivacyConsent()).toEqual(consent);
  });

  it("withdrawing consent (turning off) deletes the record", async () => {
    const store = new SettingsStore(file());
    store.load();
    const m = new CapturePrivacyManager({
      getWindow: () => new FakeWindow(), store, platform: "win32", osRelease: WIN11, confirm: async () => true,
    });
    await m.enable(true);
    expect(m.disable().consent).toBeNull();
    expect(JSON.parse(readFileSync(file(), "utf8"))).not.toHaveProperty("capturePrivacyConsent");
    const again = new SettingsStore(file());
    again.load();
    expect(again.capturePrivacyConsent()).toBeNull();
  });

  it("fails closed: a saved 'on' without a valid consent record for the current version loads as off", () => {
    const cases: unknown[] = [
      undefined, null, {}, "yes", { version: CAPTURE_PRIVACY_CONSENT_VERSION },
      { acceptedAt: AT_CONSENT }, { version: CAPTURE_PRIVACY_CONSENT_VERSION + 1, acceptedAt: AT_CONSENT },
      { version: CAPTURE_PRIVACY_CONSENT_VERSION - 1, acceptedAt: AT_CONSENT },
      { version: CAPTURE_PRIVACY_CONSENT_VERSION, acceptedAt: "never" },
      { version: "1", acceptedAt: AT_CONSENT },
    ];
    for (const bad of cases) {
      writeFileSync(file(), JSON.stringify({ ...base, capturePrivacy: true, capturePrivacyConsent: bad }));
      const store = new SettingsStore(file());
      store.load();
      expect(store.capturePrivacyEnabled(), JSON.stringify(bad)).toBe(false);
      expect(store.capturePrivacyConsent()).toBeNull();
    }
  });

  it("a consent record alone (flag missing) does not turn it on", () => {
    writeFileSync(file(), JSON.stringify({ ...base, capturePrivacyConsent: consent }));
    const store = new SettingsStore(file());
    store.load();
    expect(store.capturePrivacyEnabled()).toBe(false);
    expect(store.capturePrivacyConsent()).toBeNull();
  });

  it("the store refuses to save 'on' without a valid consent", () => {
    const store = new SettingsStore(file());
    store.load();
    expect(() => store.setCapturePrivacy(true)).toThrow();
    expect(() => store.setCapturePrivacy(true, { version: 99, acceptedAt: AT_CONSENT })).toThrow();
    expect(store.capturePrivacyEnabled()).toBe(false);
  });

  it("a stale saved choice is never re-applied to the window", () => {
    writeFileSync(file(), JSON.stringify({ ...base, capturePrivacy: true, capturePrivacyConsent: { version: 0, acceptedAt: AT_CONSENT } }));
    const store = new SettingsStore(file());
    store.load();
    const win = new FakeWindow();
    const m = new CapturePrivacyManager({
      getWindow: () => win, store, platform: "win32", osRelease: WIN11, confirm: async () => true,
    });
    m.reapply();
    expect(win.calls).toEqual([]);
    expect(m.state()).toMatchObject({ enabled: false, status: "off", consent: null });
  });
});

describe("IPC", () => {
  const RENDERER = join(tmpdir(), "renderer-fixture", "index.html");
  const TRUSTED = pathToFileURL(RENDERER).href;
  type Handler = (event: unknown, ...args: unknown[]) => Promise<any>;

  function setup(withManager: boolean) {
    const dir = mkdtempSync(join(tmpdir(), "capture-privacy-ipc-"));
    const handlers = new Map<string, Handler>();
    const win = {
      isDestroyed: () => false, isVisible: () => true, isFocused: () => true, isMinimized: () => false,
      minimize: vi.fn(), hide: vi.fn(), show: vi.fn(), restore: vi.fn(), focus: vi.fn(),
      setOpacity: vi.fn(), setAlwaysOnTop: vi.fn(), setMinimumSize: vi.fn(), setSize: vi.fn(),
      getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
      on: vi.fn(), removeListener: vi.fn(),
      webContents: { id: 1, send: vi.fn(), on: vi.fn(), once: vi.fn(), removeListener: vi.fn() },
    };
    const store = new SettingsStore(join(dir, "settings.json"));
    const fake = new FakeWindow();
    const manager = new CapturePrivacyManager({
      getWindow: () => fake, store, platform: "win32", osRelease: WIN11, confirm: async () => true,
    });
    const dispose = registerDesktopIpc({
      ipcMain: {
        handle: (channel: string, fn: Handler) => handlers.set(channel, fn),
        removeHandler: (channel: string) => handlers.delete(channel),
        on: vi.fn(), removeListener: vi.fn(),
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
      ...(withManager ? { capturePrivacy: manager } : {}),
    });
    const trusted = { senderFrame: { url: TRUSTED }, sender: { id: 1 } };
    const untrusted = { senderFrame: { url: "https://evil.example/" }, sender: { id: 2 } };
    return {
      handlers, fake, store, trusted, untrusted,
      call: (channel: string, ...args: unknown[]) => handlers.get(channel)!(trusted, ...args),
      cleanup: () => {
        dispose();
        rmSync(dir, { recursive: true, force: true });
      },
    };
  }

  let s: ReturnType<typeof setup>;
  afterEach(() => s.cleanup());

  it("reports off by default", async () => {
    s = setup(true);
    const got = await s.call("app:get-capture-privacy");
    expect(got).toMatchObject({ ok: true, data: { enabled: false, status: "off", supported: true } });
  });

  it("refuses to turn on while nobody has accepted the notice (no signed-in user): no native call, nothing saved", async () => {
    s = setup(true);
    const result = await s.call("app:set-capture-privacy", true);
    expect(result.ok).toBe(false);
    expect(s.fake.calls).toEqual([]);
    expect(s.store.capturePrivacyEnabled()).toBe(false);
  });

  it("rejects anything that is not a boolean", async () => {
    s = setup(true);
    for (const bad of ["true", 1, 0, null, undefined, {}, [], { enabled: true }]) {
      expect((await s.call("app:set-capture-privacy", bad)).ok).toBe(false);
    }
    expect(s.fake.calls).toEqual([]);
  });

  it("turning off over IPC works without any prompt", async () => {
    s = setup(true);
    const result = await s.call("app:set-capture-privacy", false);
    expect(result).toMatchObject({ ok: true, data: { enabled: false, status: "off" } });
  });

  it("refuses an untrusted sender on both channels", async () => {
    s = setup(true);
    for (const channel of ["app:get-capture-privacy", "app:set-capture-privacy"]) {
      expect(await s.handlers.get(channel)!(s.untrusted, false)).toMatchObject({ ok: false });
    }
    expect(s.fake.calls).toEqual([]);
  });

  it("without a manager both channels report 'not available'", async () => {
    s = setup(false);
    expect((await s.call("app:get-capture-privacy")).ok).toBe(false);
    expect((await s.call("app:set-capture-privacy", false)).ok).toBe(false);
  });
});

describe("source guards: visible indicator, tray and taskbar, no automatic activation", () => {
  const src = join(__dirname, "..", "src");
  const walk = (dir: string): string[] =>
    readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      return statSync(full).isDirectory() ? walk(full) : /\.(ts|tsx)$/.test(name) ? [full] : [];
    });
  const files = walk(src).map((path) => ({ path: path.replace(src, "").replace(/\\/g, "/"), text: readFileSync(path, "utf8") }));
  const textOf = (suffix: string) => files.find((f) => f.path.endsWith(suffix))?.text ?? "";

  it("only main/capture-privacy.ts calls setContentProtection", () => {
    const users = files.filter((f) => /setContentProtection\(/.test(f.text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, ""))).map((f) => f.path);
    expect(users).toEqual(["/main/capture-privacy.ts"]);
  });

  it("nothing hides the taskbar entry, the menu bar entry or the tray, or prevents the window from being listed", () => {
    for (const f of files) {
      expect(f.text, f.path).not.toMatch(/skipTaskbar|setSkipTaskbar|setVisibleOnAllWorkspaces|app\.dock\.hide|setProgressBar\(\s*-1/);
      expect(f.text, f.path).not.toMatch(/hiddenInMissionControl|setThumbnailClip|showInactive\(/);
    }
    const index = textOf("/main/index.ts");
    expect(index).toContain("createTray(");
    // The tray is destroyed only when the app is quitting.
    const destroys = [...index.matchAll(/tray\??\.destroy\(\)/g)].length;
    const willQuit = index.slice(index.indexOf('app.on("will-quit"'));
    expect(destroys).toBe([...willQuit.matchAll(/tray\??\.destroy\(\)/g)].length);
  });

  it("the setting is enabled only by the IPC handler; startup, recovery, display and update paths only re-apply", () => {
    for (const f of files) {
      if (f.path.endsWith("/main/capture-privacy.ts")) continue;
      const code = f.text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
      if (f.path.endsWith("/main/ipc.ts")) {
        expect([...code.matchAll(/capturePrivacy\.enable\(/g)].length).toBe(1);
      } else {
        expect(code, f.path).not.toMatch(/capturePrivacy\??\.enable\(|setCapturePrivacy\(true/);
      }
    }
    const index = textOf("/main/index.ts");
    expect(index).toContain("capturePrivacy.reapply()");
    expect(index).not.toMatch(/capturePrivacy\.enable/);
  });

  it("the renderer shows an indicator on every page while it is on, and never claims invisibility", () => {
    const panel = textOf("/ui/CapturePrivacyPanel.tsx");
    const app = textOf("/ui/App.tsx");
    expect(app).toContain("<CapturePrivacyBanner />");
    expect(panel).toContain("CAPTURE_PRIVACY_INDICATOR");
    expect(panel).toMatch(/if \(!state \|\| !state\.enabled\) return null/);
    expect(panel).not.toMatch(/Dismiss|onClick=\{.*hide/i); // the indicator cannot be dismissed
    const wording = [CAPTURE_PRIVACY_INDICATOR, ...CAPTURE_PRIVACY_EXPLANATION, ...Object.values(CAPTURE_PRIVACY_MESSAGES)].join(" ");
    expect(wording).not.toMatch(/\b(invisible|undetectable|cannot be (seen|detected)|untraceable|100%|guaranteed to)\b/i);
    expect(wording).toMatch(/not a guarantee/i);
    expect(wording).toMatch(/taskbar/i);
    expect(wording).toMatch(/system tray/i);
    expect(wording).toMatch(/responsible for following the rules/i);
  });

  it("protection is not applied inside Windows remote sessions (no concealment from remote-access tools)", () => {
    expect(textOf("/main/index.ts")).toMatch(
      /appendSwitch\(\s*"disable-features",\s*"AllowWindowCaptureExclusionInRemoteSessions"\s*\)/,
    );
    expect(CAPTURE_PRIVACY_EXPLANATION.join(" ")).toMatch(/remote session/i);
  });

  it("the setting button states the explanation first and is disabled when unsupported", () => {
    const panel = textOf("/ui/CapturePrivacyPanel.tsx");
    expect(panel).toContain("CAPTURE_PRIVACY_EXPLANATION.map");
    expect(panel).toMatch(/!state\.supported/);
  });

  it("the acknowledgement wording mentions the setting", () => {
    const ack = textOf("/shared/acknowledgement.ts");
    expect(ack).toMatch(/leave this window out of screen capture/);
    expect(ack).toMatch(/will not use it to avoid monitoring/);
  });
});
