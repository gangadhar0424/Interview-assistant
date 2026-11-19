/**
 * M-4 static guards: a keyboard shortcut can only show, hide or focus the window and ask the
 * renderer to move to a panel. These read the source, so a later change that wires a shortcut to
 * audio or screenshot capture fails here.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SHORTCUT_ACTIONS } from "../src/shared/shortcuts";

const SRC = join(__dirname, "..", "src");
const read = (rel: string) => readFileSync(join(SRC, rel), "utf8").replaceAll("\r\n", "\n");
const stripComments = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const FORBIDDEN =
  /audio-service|capture-service|capture-electron|AudioService|CaptureService|desktopCapturer|getUserMedia|getDisplayMedia|\baudio\.(start|stop|pushChunk)|\bcapture\.(start|extract|cancel)|setIndicator|transcribe|extractScreenshot/;

describe("shortcut modules cannot start capture", () => {
  it("shortcuts.ts imports and calls nothing about audio, capture, screens or microphones", () => {
    const code = stripComments(read("main/shortcuts.ts"));
    expect(code).not.toMatch(FORBIDDEN);
    const imports = [...code.matchAll(/from "([^"]+)"/g)].map((m) => m[1]);
    expect(imports.sort()).toEqual(["../shared/shortcuts"]);
  });

  it("the shared shortcut definitions name no capture action and import nothing", () => {
    const code = stripComments(read("shared/shortcuts.ts"));
    expect(code).not.toMatch(FORBIDDEN);
    expect(code).not.toMatch(/\bimport\b/);
    expect([...SHORTCUT_ACTIONS]).toEqual(["toggleWindow", "hideWindow", "focusAudio", "focusScreenshot"]);
  });

  it("the shortcut wiring in index.ts gives actions only window methods and a panel message", () => {
    const index = stripComments(read("main/index.ts"));
    const start = index.indexOf("const shortcutManager = new ShortcutManager({");
    const end = index.indexOf("function showMainWindow", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const block = index.slice(start, end);
    expect(block).not.toMatch(FORBIDDEN);
    expect(block).toMatch(/globalShortcut\.register/);
    expect(block).toMatch(/globalShortcut\.unregister/);
    // The only thing sent to the renderer is the panel to move to.
    expect([...block.matchAll(/\.send\("([^"]+)"/g)].map((m) => m[1])).toEqual(["shortcut:event"]);
  });

  it("registration happens after the app is ready and removal at quit", () => {
    const index = read("main/index.ts");
    expect(index).toMatch(/app\.whenReady\(\)\.then\(\(\) => \{[\s\S]*shortcutManager\.apply\(settingsStore\.shortcutConfig\(\)\)/);
    expect(index).toMatch(/app\.on\("will-quit", \(\) => \{[^}]*shortcutManager\.dispose\(\)/);
    expect(index.indexOf("shortcutManager.apply(")).toBeGreaterThan(index.indexOf("app.whenReady()"));
  });

  it("the renderer reacts to a shortcut by moving focus only (no start call in the handler or panels)", () => {
    const app = stripComments(read("renderer/src/ui/App.tsx"));
    const handler = app.slice(app.indexOf("onShortcutEvent("), app.indexOf("onSessionExpired("));
    expect(handler).toMatch(/requestPanelFocus/);
    expect(handler).not.toMatch(/startAudio|startCapture|openCapture|audio\.start|capture\.start/);
    for (const panel of ["AudioPanel.tsx", "ScreenshotPanel.tsx"]) {
      const code = stripComments(read(`renderer/src/ui/${panel}`));
      const focusEffect = code.slice(code.indexOf("panelFocus?.panel"), code.indexOf("panelFocus?.panel") + 260);
      expect(focusEffect).toMatch(/\.focus\(\)/);
      expect(focusEffect).not.toMatch(/start|open|click\(/);
    }
  });
});
