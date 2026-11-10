/**
 * Phase 11B source guards: structural promises that no runtime test can prove by itself.
 * They scan the real source tree, so a future change that adds hidden capture, a second capture
 * entry point, disk writes or a wider renderer bridge fails here.
 */
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";
import { DEFAULT_SETTINGS, SettingsStore } from "../src/main/settings-store";

const SRC = join(__dirname, "..", "src");

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  });
}

const sources = files(SRC)
  .filter((path) => /\.(ts|tsx|html)$/.test(path))
  .map((path) => ({
    rel: relative(SRC, path).replaceAll("\\", "/"),
    text: readFileSync(path, "utf8").replaceAll("\r\n", "\n"),
  }));

const where = (needle: RegExp) =>
  sources.filter((s) => needle.test(s.text)).map((s) => s.rel).sort();

describe("capture can only start from the one explicit, gated path", () => {
  it("desktopCapturer is used only by the capture adapter", () => {
    expect(where(/desktopCapturer/)).toEqual(["main/capture-electron.ts"]);
  });

  it("the capture adapter is imported only by the main entry point", () => {
    expect(where(/from "\.\/capture-electron"/)).toEqual(["main/index.ts"]);
  });

  it("captureDisplay is invoked only from CaptureService, after the countdown", () => {
    const callers = sources.filter((s) => /\.captureDisplay\(/.test(s.text)).map((s) => s.rel);
    expect(callers).toEqual(["main/capture-service.ts"]);
    const service = sources.find((s) => s.rel === "main/capture-service.ts")?.text ?? "";
    expect(service.match(/\.captureDisplay\(/g)).toHaveLength(1);
    // the single call sits behind the visible countdown and the capture-time checks
    expect(service.indexOf("checkAtCaptureTime()")).toBeLessThan(service.indexOf(".captureDisplay("));
  });

  it("there is no hotkey, periodic timer, display-media API or background capture anywhere", () => {
    expect(where(/globalShortcut/)).toEqual([]);
    expect(where(/getDisplayMedia|mediaDevices|MediaRecorder/)).toEqual([]);
    expect(where(/setInterval\(/).filter((rel) => rel.startsWith("main/"))).toEqual([]);
    expect(where(/powerMonitor|desktopCapturer\.getSources/).filter((rel) => rel !== "main/capture-electron.ts")).toEqual([]);
  });

  it("the capture modules never write images or text to disk, the clipboard or the log", () => {
    for (const rel of ["main/capture-service.ts", "main/capture-electron.ts", "main/capture-image.ts"]) {
      const text = sources.find((s) => s.rel === rel)?.text ?? "";
      expect(text, rel).not.toMatch(/node:fs|writeFile|createWriteStream|saveAs|savePage|capturePage|clipboard|console\.|localStorage|sessionStorage|indexedDB/);
    }
  });

  it("nothing in the app writes a screenshot to disk (fs use is limited to settings and the token store)", () => {
    const usesFs = where(/node:fs/).filter((rel) => rel.startsWith("main/"));
    expect(usesFs).toEqual(["main/secure-refresh-store.ts", "main/settings-store.ts"]);
  });

  it("the renderer never imports Electron or touches the screen", () => {
    for (const s of sources.filter((x) => x.rel.startsWith("renderer/"))) {
      expect(s.text, s.rel).not.toMatch(/from "electron"|require\(|desktopCapturer|nativeImage/);
    }
  });
});

describe("the preload bridge stays narrow", () => {
  const preload = sources.find((s) => s.rel === "preload/index.ts")?.text ?? "";
  const channels = [...new Set([...preload.matchAll(/invoke\(\s*"([^"]+)"/g)].map((m) => m[1]))].sort();

  it("exposes exactly the approved channels and no generic invoke", () => {
    expect(channels).toEqual(
      [
        "app:check-backend",
        "app:close-to-tray",
        "app:copy-text",
        "app:get-settings",
        "app:minimize",
        "app:set-expanded",
        "app:update-settings",
        "auth:get-state",
        "auth:login",
        "auth:logout",
        "auth:signup",
        "capture:cancel",
        "capture:extract",
        "capture:list-displays",
        "capture:start",
        "sessions:list",
        "sessions:questions",
        "sessions:stream-cancel",
        "sessions:stream-start",
      ].sort(),
    );
    expect(preload).not.toMatch(/invoke:\s*ipcRenderer|ipcRenderer:\s*ipcRenderer|exposeInMainWorld\("(?!desktop")/);
    expect(preload).not.toMatch(/desktopCapturer|nativeImage|require\(|from "node:/);
  });

  it("the capture bridge has exactly four functions plus one event subscription", () => {
    const block = /capture:\s*\{([\s\S]*?)\n  \},\n  onCaptureEvent/.exec(preload)?.[1] ?? "";
    const names = [...block.matchAll(/^\s{4}(\w+):/gm)].map((m) => m[1]);
    expect(names).toEqual(["listDisplays", "start", "cancel", "extract"]);
  });
});

describe("Electron hardening is unchanged", () => {
  const html = sources.find((s) => s.rel === "renderer/index.html")?.text ?? "";
  const main = sources.find((s) => s.rel === "main/index.ts")?.text ?? "";

  it("keeps the strict CSP (no inline styles, no network from the renderer)", () => {
    expect(html).toContain("style-src 'self';");
    expect(html).not.toContain("unsafe-inline");
    expect(html).toContain("connect-src 'none'");
    expect(html).toContain("img-src 'self' data:");
    expect(html).toContain("form-action 'none'");
  });

  it("keeps context isolation, the sandbox and denied permissions", () => {
    expect(main).toContain("contextIsolation: true");
    expect(main).toContain("nodeIntegration: false");
    expect(main).toContain("sandbox: true");
    expect(main).toContain("webviewTag: false");
    expect(main).toContain("callback(false)");
    expect(main).not.toMatch(/setDisplayMediaRequestHandler|setContentProtection/);
  });
});

describe("settings", () => {
  it("screenshot input is off by default", () => {
    expect(DEFAULT_SETTINGS.screenshotInputEnabled).toBe(false);
  });

  it("a settings file written before Phase 11B loads with screenshot input off", () => {
    const dir = mkdtempSync(join(tmpdir(), "settings-legacy-"));
    try {
      const path = join(dir, "settings.json");
      writeFileSync(
        path,
        JSON.stringify({ backendBaseUrl: "http://127.0.0.1:8000", alwaysOnTop: true, opacity: 0.9, expanded: false }),
      );
      const store = new SettingsStore(path);
      expect(store.load().screenshotInputEnabled).toBe(false);
      expect(store.update({ screenshotInputEnabled: true }).screenshotInputEnabled).toBe(true);
      expect(new SettingsStore(path).load().screenshotInputEnabled).toBe(true);
      const saved = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
      expect(Object.keys(saved).sort()).toEqual(
        ["alwaysOnTop", "backendBaseUrl", "expanded", "opacity", "screenshotInputEnabled"],
      ); // only settings: no screenshot, text or session data is ever written
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("rejects a non-boolean screenshot setting in a settings file", () => {
    const dir = mkdtempSync(join(tmpdir(), "settings-bad-"));
    try {
      const path = join(dir, "settings.json");
      writeFileSync(
        path,
        JSON.stringify({ backendBaseUrl: "http://127.0.0.1:8000", alwaysOnTop: true, opacity: 0.9, expanded: false, screenshotInputEnabled: "yes" }),
      );
      expect(() => new SettingsStore(path).load()).toThrow();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
