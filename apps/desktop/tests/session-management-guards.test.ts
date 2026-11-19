/** M-9 source guards: the new file-writing and session-deleting code stays narrow. */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..", "src");
function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : /\.(ts|tsx)$/.test(name) ? [full] : [];
  });
}
const sources = walk(SRC).map((file) => ({
  rel: relative(SRC, file).replace(/\\/g, "/"),
  text: readFileSync(file, "utf8"),
}));
const text = (rel: string) => sources.find((s) => s.rel === rel)?.text ?? "";

describe("session export writes only where the user chose", () => {
  it("only the file-writing modules import the file system, and session-files.ts is one of them", () => {
    const importers = sources
      .filter((s) => /from "node:fs(\/promises)?"/.test(s.text) && /writeFile|createWriteStream|appendFile/.test(s.text))
      .map((s) => s.rel)
      .filter((rel) => rel.startsWith("main/"));
    expect(importers).toContain("main/session-files.ts");
    expect(importers).not.toContain("main/ipc.ts"); // ipc.ts only calls the injected writer
    expect(importers.filter((rel) => /capture|audio/.test(rel))).toEqual([]);
  });

  it("session-files.ts takes the path only from a native save dialog and never touches capture or audio", () => {
    const body = text("main/session-files.ts");
    expect(body).toContain("showSaveDialog");
    expect(body).toContain("showMessageBox");
    expect(body).not.toMatch(/from "\.\/(capture|audio)|desktopCapturer|clipboard|console\./);
    // The write takes the path it was given by the dialog result, never a renderer string.
    expect(text("main/ipc.ts")).toMatch(/chooseExportFile\(\s*`session-/);
  });

  it("the export channel validates the id and format and writes only after a path is chosen", () => {
    const ipc = text("main/ipc.ts");
    const block = ipc.slice(ipc.indexOf('handle("sessions:export"'), ipc.indexOf("sessions:export", ipc.indexOf('handle("sessions:export"')) + 1400);
    expect(block).toMatch(/isUuid\(sessionId\)/);
    expect(block.indexOf("chooseExportFile")).toBeLessThan(block.indexOf("exportSession"));
    expect(block.indexOf("exportSession")).toBeLessThan(block.indexOf("writeFile"));
  });

  it("the renderer never receives export content or a file path", () => {
    const contracts = text("shared/contracts.ts");
    expect(contracts).toContain("export type SessionExportResult = { saved: boolean; fileName: string | null };");
    for (const s of sources.filter((x) => x.rel.startsWith("renderer/"))) {
      expect(s.text, s.rel).not.toMatch(/node:fs|showSaveDialog|writeFile/);
    }
  });
});

describe("delete and end never leave something running", () => {
  it("delete is confirmed by the main process before the request", () => {
    const ipc = text("main/ipc.ts");
    const start = ipc.indexOf('handle("sessions:delete"');
    const block = ipc.slice(start, start + 700);
    expect(block.indexOf("confirmDelete")).toBeGreaterThan(-1);
    expect(block.indexOf("confirmDelete")).toBeLessThan(block.indexOf("deleteSession"));
  });

  it("completing or deleting a session stops audio and any screenshot first", () => {
    const ipc = text("main/ipc.ts");
    for (const channel of ["sessions:end", "sessions:delete"]) {
      const start = ipc.indexOf(`handle("${channel}"`);
      const block = ipc.slice(start, start + 800);
      expect(block, channel).toContain('audio.stop("session")');
      expect(block, channel).toContain('capture.discard("session")');
    }
  });

  it("reopening a session starts no audio and no capture", () => {
    const ipc = text("main/ipc.ts");
    const start = ipc.indexOf('handle("sessions:reopen"');
    const block = ipc.slice(start, start + 300);
    expect(block).not.toMatch(/audio\.start|capture\.start/);
  });

  it("no session-management code in the store starts audio or a capture", () => {
    const store = text("renderer/src/store.ts");
    const start = store.indexOf("renameSession: async");
    const end = store.indexOf("loadHistory: async");
    const block = store.slice(start, end);
    expect(block).not.toMatch(/audio\.start|startAudio\(|startCapture\(|capture\.start|askFrom/);
  });
});
