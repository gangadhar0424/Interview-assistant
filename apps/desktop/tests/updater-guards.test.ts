import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level guards for the secure updater (M-11, D-78). They pin the properties a refactor must
// not lose: no silent download or install, no unverified installer, no extra renderer capability.
const DESKTOP = resolve(__dirname, "..");
const SRC = join(DESKTOP, "src");

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  });
}
const sources = files(SRC)
  .filter((p) => /\.(ts|tsx|mjs)$/.test(p))
  .map((p) => ({ rel: relative(SRC, p).replaceAll("\\", "/"), text: readFileSync(p, "utf8").replaceAll("\r\n", "\n") }));
const read = (rel: string): string => sources.find((s) => s.rel === rel)?.text ?? "";
const where = (needle: RegExp) => sources.filter((s) => needle.test(s.text)).map((s) => s.rel).sort();

describe("the update engine is configured safely and is the only place that touches electron-updater", () => {
  it("only the engine adapter imports electron-updater or can quit-and-install", () => {
    expect(where(/from "electron-updater"|import\("electron-updater"\)/)).toEqual(["main/updater-engine.ts"]);
    expect(where(/quitAndInstall\(/).filter((rel) => rel !== "main/updater.ts" && rel !== "main/updater-engine.ts")).toEqual([]);
  });

  it("never downloads or installs by itself, never downgrades, never relaxes verification", () => {
    const engine = read("main/updater-engine.ts");
    expect(engine).toMatch(/autoDownload = false/);
    expect(engine).toMatch(/autoInstallOnAppQuit = false/);
    expect(engine).toMatch(/allowDowngrade = false/);
    expect(engine).toMatch(/allowPrerelease = false/);
    expect(engine).not.toMatch(/autoDownload = true|autoInstallOnAppQuit = true|allowDowngrade = true|allowPrerelease = true/);
    // The library's publisher verification must stay at its default.
    expect(engine).not.toMatch(/verifyUpdateCodeSignature|forceDevUpdateConfig|setFeedURL|requestHeaders|addAuthHeader|allowInsecure/);
  });

  it("every other module is free of update-library and browser-launch capabilities", () => {
    expect(where(/openExternal/)).toEqual([]);
    expect(where(/setFeedURL|checkForUpdatesAndNotify|autoUpdater\./).filter((rel) => rel !== "main/updater-engine.ts")).toEqual([]);
  });

  it("the update code never starts audio or screen capture", () => {
    for (const rel of ["main/updater.ts", "main/updater-io.ts", "main/updater-engine.ts", "renderer/src/ui/UpdatePanel.tsx"]) {
      expect(read(rel), rel).not.toMatch(/desktopCapturer|getUserMedia|audio\.start|capture\.start|startListening|setPermissionRequestHandler/);
    }
  });
});

describe("the update source is fetched safely", () => {
  const io = read("main/updater-io.ts");

  it("uses https only, no redirects, no cookies, a size cap and a timeout", () => {
    expect(io).toMatch(/protocol !== "https:"/);
    expect(io).toMatch(/redirect: "error"/);
    expect(io).toMatch(/credentials: "omit"/);
    expect(io).toMatch(/MAX_MANIFEST_BYTES/);
    expect(io).toMatch(/AbortController/);
  });

  it("only reads the disk: no write APIs", () => {
    expect(io).not.toMatch(/writeFile|createWriteStream|appendFile|unlink|rmSync|renameSync|execFile|spawn|child_process/);
  });
});

describe("the renderer can only reach the updater through four argument-free channels", () => {
  const ipc = read("main/ipc.ts");
  const preload = read("preload/index.ts");

  it("registers exactly the four update channels and they take no renderer input", () => {
    const handlers = [...ipc.matchAll(/handle\("(updates:[a-z-]+)",\s*(async\s*)?\(([^)]*)\)/g)];
    expect(handlers.map((m) => m[1])).toEqual(["updates:get-state", "updates:check", "updates:download", "updates:install"]);
    for (const match of handlers) expect(match[3], match[1]).toBe(""); // no parameters at all
  });

  it("installing first stops what the renderer owned, then installs", () => {
    const block = /handle\("updates:install"[\s\S]*?\n  \}\);/.exec(ipc)?.[0] ?? "";
    expect(block.indexOf("canInstallNow")).toBeGreaterThan(-1);
    expect(block.indexOf("onRendererGone()")).toBeGreaterThan(block.indexOf("canInstallNow"));
    expect(block.indexOf("installAndRestart")).toBeGreaterThan(block.indexOf("onRendererGone()"));
  });

  it("the bridge exposes four functions and one event subscription for updates", () => {
    const block = /updates:\s*\{([\s\S]*?)\n  \},\n  handoff:/.exec(preload)?.[1] ?? "";
    expect([...block.matchAll(/^\s{4}(\w+):/gm)].map((m) => m[1])).toEqual(["getState", "check", "download", "install"]);
    expect([...preload.matchAll(/ipcRenderer\.on\("(updates:[a-z-]+)"/g)].map((m) => m[1])).toEqual(["updates:event"]);
  });

  it("the main process has no way to enable updates for an unsigned build", () => {
    const manager = read("main/updater.ts");
    expect(manager).toMatch(/evaluateUpdateConfig\(parseAppUpdateYml\(text\)\)/);
    expect(manager).toMatch(/if \(!this\.isEnabled\(\)/);
  });
});

describe("user data survives an update", () => {
  // The project sets nsis.deleteAppDataOnUninstall. An update runs the old uninstaller first; the
  // installer template must pass --updated so that deletion is skipped. This pins that behaviour so a
  // library upgrade that changes it fails here instead of wiping settings and sign-in on update.
  const templates = join(DESKTOP, "node_modules", "app-builder-lib", "templates", "nsis");
  const read2 = (p: string): string => readFileSync(join(templates, p), "utf8").replaceAll("\r\n", "\n");

  it("the uninstaller does not delete app data when invoked as part of an update", () => {
    const uninstaller = read2("uninstaller.nsh");
    expect(uninstaller).toMatch(/DELETE_APP_DATA_ON_UNINSTALL[\s\S]{0,80}\$\{ifNot\} \$\{isUpdated\}[\s\S]{0,80}StrCpy \$isDeleteAppData "1"/);
  });

  it("the updating installer tells the old uninstaller it is an update", () => {
    expect(read2("include/installUtil.nsh")).toMatch(/--updated/);
  });

  it("the project really does delete app data on a manual uninstall (so the check above matters)", () => {
    const pkg = JSON.parse(readFileSync(join(DESKTOP, "package.json"), "utf8")) as { build: { nsis: { deleteAppDataOnUninstall: boolean } } };
    expect(pkg.build.nsis.deleteAppDataOnUninstall).toBe(true);
  });
});

describe("the updater adds no file-system writes (SEC-09)", () => {
  it("the manager never deletes or writes: a rejected installer is only refused", () => {
    const manager = read("main/updater.ts");
    expect(manager).not.toMatch(/rmSync|unlink|writeFile|node:fs|discardDownload/);
  });
});
