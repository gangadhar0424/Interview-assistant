/**
 * M-5 static guards: crash recovery and diagnostics can only reload the window, clean up, and report
 * typed facts. They read the source, so a later change that lets recovery start a capture, write to
 * disk, send diagnostics over the network, or put free text into a report fails here.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..", "src");
const read = (rel: string) => readFileSync(join(SRC, rel), "utf8").replaceAll("\r\n", "\n");
const stripComments = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

describe("recovery.ts cannot start anything", () => {
  const code = stripComments(read("main/recovery.ts"));

  it("imports nothing but the diagnostics log", () => {
    expect([...code.matchAll(/from "([^"]+)"/g)].map((m) => m[1])).toEqual(["./diagnostics"]);
  });

  it("names no audio, capture, session, sign-in, network or file operation", () => {
    expect(code).not.toMatch(
      /audio|capture|desktopCapturer|getUserMedia|transcribe|session|signIn|login|logout|fetch\(|XMLHttpRequest|node:fs|writeFile|setInterval|process\.exit|app\.quit/i,
    );
  });

  it("its ports are limited to reload, cleanup, state checks, the dialog, copy, timer and clock", () => {
    const portBlock = code.slice(code.indexOf("export type RecoveryPorts"), code.indexOf("export const AUTO_RELOAD_GAP_MS"));
    const names = [...portBlock.matchAll(/^\s{2}(\w+)\(/gm)].map((m) => m[1]).sort();
    expect(names).toEqual(["cleanup", "copyDiagnostics", "isQuitting", "isWindowAlive", "now", "promptFailure", "reload", "schedule"]);
  });
});

describe("diagnostics.ts stays local, bounded and content-free", () => {
  const code = stripComments(read("main/diagnostics.ts"));

  it("has no imports, no network, no disk, no console and no timers", () => {
    expect(code).not.toMatch(/\bimport\b/);
    expect(code).not.toMatch(/fetch\(|XMLHttpRequest|from "node:|require\(|writeFile|console\.|setInterval|setTimeout|crashReporter|process\.env/);
  });

  it("builds the report only from typed provider data and a closed set of events", () => {
    expect(code).toContain("DIAG_EVENT_TYPES");
    expect(code).toContain("DIAG_REASONS");
    expect(code).toContain("MAX_REPORT_CHARS");
    expect(code).toContain("MAX_LOG_EVENTS");
  });
});

describe("wiring", () => {
  it("index.ts reloads only through the recovery module and never sends diagnostics anywhere", () => {
    const index = stripComments(read("main/index.ts"));
    expect(index).not.toMatch(/crashReporter|submitURL|uploadToServer/);
    // The only places the main process reloads the page.
    expect([...index.matchAll(/\.reload\(\)/g)]).toHaveLength(1);
    expect(index).toMatch(/new RendererRecovery\(/);
    expect(index).toMatch(/recovery\.dispose\(\)/);
    expect(index).toMatch(/window\.on\("session-end"/); // logoff is shutdown, not a crash
    // A crash handler passes the details to the module; it does not decide for itself.
    expect(index).toMatch(/"render-process-gone", \(_event, details\) => recovery\.onRendererGone\(details\)/);
  });

  it("the renderer only ever asks to copy diagnostics and never receives the report text", () => {
    const preload = read("preload/index.ts");
    expect(preload).toMatch(/copyDiagnostics: \(\) =>\s+ipcRenderer\.invoke\("app:copy-diagnostics"\)/);
    const contracts = read("shared/recovery.ts");
    const resultType = stripComments(contracts).match(/CopyDiagnosticsResult = \{([^}]*)\}/)?.[1] ?? "";
    expect([...resultType.matchAll(/(\w+):/g)].map((m) => m[1])).toEqual(["characters", "truncated"]); // counts only, no text
    const panel = stripComments(read("renderer/src/ui/SupportPanel.tsx"));
    // The text may SAY "clipboard"; the code must not touch it (the main process does the copying).
    expect(panel).not.toMatch(/navigator\.|clipboard\.|writeText/);
  });

  it("the recovery notice is shown on the signed-out screen as well as in the signed-in app", () => {
    const app = stripComments(read("renderer/src/ui/App.tsx"));
    expect(app.match(/<RecoveryBanner \/>/g)).toHaveLength(2);
  });

  it("the main process still never logs (existing rule) and the renderer shows no stack traces", () => {
    for (const rel of ["main/recovery.ts", "main/diagnostics.ts"]) expect(read(rel)).not.toMatch(/console\./);
    const panel = stripComments(read("renderer/src/ui/SupportPanel.tsx"));
    expect(panel).not.toMatch(/\.stack|stack trace|error\.message/);
  });
});
