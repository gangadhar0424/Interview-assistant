/**
 * M-6 static guards: window placement can only move and remember the window. These read the source, so
 * a later change that makes placement touch capture, audio, always-on-top, the network or a timer that
 * runs forever fails here. They also check the wiring around shortcuts (M-4) and recovery (M-5).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..", "src");
const read = (rel: string) => readFileSync(join(SRC, rel), "utf8").replaceAll("\r\n", "\n");
const stripComments = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

describe("window-placement.ts only places the window", () => {
  const code = stripComments(read("main/window-placement.ts"));

  it("imports only a type from the shared placement definitions", () => {
    expect([...code.matchAll(/from "([^"]+)"/g)].map((m) => m[1])).toEqual(["../shared/placement"]);
  });

  it("names no audio, capture, always-on-top, opacity, network, disk, logging or repeating timer", () => {
    expect(code).not.toMatch(
      /audio|capture|desktopCapturer|getUserMedia|transcribe|setAlwaysOnTop|alwaysOnTop|setOpacity|fetch\(|node:fs|writeFile|console\.|setInterval|globalShortcut|screen\./i,
    );
  });

  it("its window port can only read bounds and set them", () => {
    const block = code.slice(code.indexOf("export type PlacementWindow"), code.indexOf("export type PlacementPorts"));
    expect([...block.matchAll(/^\s{2}(\w+)\(/gm)].map((m) => m[1]).sort()).toEqual([
      "getBounds", "isDestroyed", "isMinimized", "isVisible", "setBounds",
    ]);
  });
});

describe("wiring", () => {
  const index = stripComments(read("main/index.ts"));

  it("the window is created at the remembered place and the old fixed-size reset at startup is gone", () => {
    expect(index).toMatch(/placement\.initialBounds\(/);
    expect(index).not.toMatch(/\bsetExpanded\(/); // it used to reset a saved size on every start
  });

  it("display events are listened to once the app is ready and removed at quit", () => {
    const ready = index.slice(index.indexOf("app.whenReady()"));
    expect(ready).toMatch(/screen\.on\("display-added"/);
    expect(ready).toMatch(/screen\.on\("display-removed"/);
    expect(ready).toMatch(/screen\.on\("display-metrics-changed"/);
    expect(index).toMatch(/app\.on\("will-quit", \(\) => \{[\s\S]*screen\.removeListener\("display-added"[\s\S]*placement\.dispose\(\)/);
    expect(index.indexOf("screen.on(")).toBeGreaterThan(index.indexOf("app.whenReady()"));
  });

  it("a hidden window saves its place, in case a move produced no move event", () => {
    expect(index).toMatch(/window\.on\("hide", \(\) => placement\.settle\(\)\)/);
  });

  it("the placement port is the only place the main entry sets window bounds", () => {
    expect([...index.matchAll(/\.setBounds\(/g)]).toHaveLength(1);
  });

  it("always-on-top is still set only through the existing window settings (M-6 does not touch it)", () => {
    expect([...index.matchAll(/setAlwaysOnTop/g)]).toHaveLength(0);
    expect(index).toMatch(/applyWindowSettings\(mainWindow, settings\)/);
  });

  it("recovery (M-5) and shortcuts (M-4) neither import nor call placement", () => {
    for (const rel of ["main/recovery.ts", "main/diagnostics.ts", "main/shortcuts.ts", "shared/shortcuts.ts"]) {
      expect(stripComments(read(rel)), rel).not.toMatch(/placement|setBounds|display/i);
    }
  });

  it("the screenshot display chooser is unchanged: capture code does not use placement", () => {
    for (const rel of ["main/capture-electron.ts", "main/capture-service.ts", "main/audio-service.ts"]) {
      expect(stripComments(read(rel)), rel).not.toMatch(/window-placement|WindowPlacement/);
    }
  });

  it("the renderer can only ask to move to a display by id; it never sets coordinates", () => {
    const preload = read("preload/index.ts");
    expect(preload).toMatch(/moveToDisplay: \(displayId\) =>\s+ipcRenderer\.invoke\("app:move-to-display", displayId\)/);
    const panel = stripComments(read("renderer/src/ui/DisplaySettings.tsx"));
    expect(panel).not.toMatch(/setBounds|screen\.|getBounds|desktopCapturer/);
  });
});
