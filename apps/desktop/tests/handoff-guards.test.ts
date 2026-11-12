/**
 * Web -> desktop handoff (D-55) source guards: structural promises no runtime test proves alone.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");
const SRC = join(ROOT, "src");

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  });
}

const sources = files(SRC)
  .filter((path) => /\.(ts|tsx)$/.test(path))
  .map((path) => ({
    rel: relative(SRC, path).replaceAll("\\", "/"),
    text: readFileSync(path, "utf8").replaceAll("\r\n", "\n"),
  }));

const text = (rel: string) => sources.find((s) => s.rel === rel)?.text ?? "";
// Comments explain rules ("never log the code"); strip them so they do not trip the guards.
const code = (rel: string) =>
  text(rel).replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");

describe("the protocol is registered by the packaged app", () => {
  const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8")) as {
    build: { protocols?: Array<{ name: string; schemes: string[] }> };
  };

  it("the installer registers exactly the interviewassistant scheme", () => {
    expect(pkg.build.protocols).toEqual([{ name: "Interview Assistant", schemes: ["interviewassistant"] }]);
  });

  it("main registers the scheme, takes the single-instance lock and handles a second instance", () => {
    const main = code("main/index.ts");
    expect(main).toContain("setAsDefaultProtocolClient(PROTOCOL_SCHEME");
    expect(main).toContain("requestSingleInstanceLock()");
    expect(main).toMatch(/app\.on\("second-instance"/);
    expect(main).toMatch(/if \(!gotLock\) \{\s*app\.quit\(\)/);
    expect(main).toContain("extractLinkFromArgv(process.argv)"); // cold start
    expect(main).toContain("extractLinkFromArgv(argv)"); // second instance
  });

  it("the development-only registration path is the documented electron-vite one", () => {
    expect(code("main/index.ts")).toContain("process.defaultApp");
  });
});

describe("the code stays in the main process and out of logs", () => {
  const handoffSources = ["main/deep-link.ts", "main/handoff.ts"];

  it("handoff code never logs", () => {
    for (const rel of [...handoffSources, "main/index.ts"]) {
      expect(code(rel), rel).not.toMatch(/console\./);
    }
  });

  it("the renderer and preload never see a code, a deep link or the protocol scheme", () => {
    for (const s of sources.filter((x) => x.rel.startsWith("renderer/") || x.rel === "preload/index.ts")) {
      expect(s.text, s.rel).not.toMatch(/interviewassistant:\/\/|parseHandoffLink|redeemHandoff|deep-link/);
    }
  });

  it("the contract state has no field that could carry the code", () => {
    const contracts = text("shared/contracts.ts");
    const block = /export type HandoffState = \{([\s\S]*?)\n\};/.exec(contracts)?.[1] ?? "";
    expect(block).not.toMatch(/code|token|url|link/i);
  });

  it("the code is sent only in the redeem request body, never in a URL or query", () => {
    const client = code("main/api-client.ts");
    const redeem = /async redeemHandoff[\s\S]*?\n  \}\n/.exec(client)?.[0] ?? "";
    expect(redeem).toContain('"/api/v1/sessions/handoff/redeem"');
    expect(redeem).toMatch(/requestJson<unknown>\(\s*"\/api\/v1\/sessions\/handoff\/redeem",\s*\{ code \}/);
    expect(redeem).not.toMatch(/\?h=|searchParams|console\./);
  });

  it("the minted login is adopted only through commit() and is held by main alone (D-56)", () => {
    const controller = code("main/handoff.ts");
    // Adoption happens in exactly one place, after the account and capture confirmations.
    expect([...controller.matchAll(/\.commit\(\)/g)]).toHaveLength(1);
    expect(controller.indexOf("confirmAccountSwitch()")).toBeLessThan(controller.indexOf(".commit()"));
    const contracts = text("shared/contracts.ts");
    const state = /export type HandoffState = \{([\s\S]*?)\n\};/.exec(contracts)?.[1] ?? "";
    expect(state).not.toMatch(/token|cookie|refresh|userId|email/i);
  });

  it("redemption sends no Authorization header (the code is the credential)", () => {
    const client = code("main/api-client.ts");
    const redeem = /async redeemHandoff[\s\S]*?\n  \}\n/.exec(client)?.[0] ?? "";
    expect(redeem).not.toMatch(/Authorization|Bearer/);
  });

  it("nothing persists the code (no fs, settings or storage use in handoff code)", () => {
    for (const rel of handoffSources) {
      expect(code(rel), rel).not.toMatch(/node:fs|writeFile|localStorage|sessionStorage|safeStorage|settings/);
    }
  });
});

describe("READY can never start capture", () => {
  it("the handoff controller has no way to start audio or screenshots", () => {
    const controller = code("main/handoff.ts");
    expect(controller).not.toMatch(/audio\.start|capture\.start|startAudio|startCapture|getUserMedia|desktopCapturer/);
    // The only capture-related effects are the confirmation and the existing stop on switching.
    expect(controller).toContain("confirmSwitch");
    expect(controller).toContain("stopLive");
  });

  it("the renderer's handoff handling never calls the audio or capture start paths", () => {
    const store = code("renderer/src/store.ts");
    const handler = /async function selectHandedOffSession[\s\S]*?\n\}\n/.exec(store)?.[0] ?? "";
    expect(handler).toContain("selectSession");
    expect(handler).not.toMatch(/startAudio|startCapture|audio\.start|capture\.start|openCapture/);
    const event = /handleHandoffEvent: async[\s\S]*?\n  \},/.exec(store)?.[0] ?? "";
    expect(event).not.toMatch(/startAudio|startCapture|audio\.start|capture\.start|openCapture/);
  });

  it("the handoff IPC adds exactly two named channels and the bridge stays narrow", () => {
    const preload = text("preload/index.ts");
    const block = /handoff:\s*\{([\s\S]*?)\n  \},\n  capture:/.exec(preload)?.[1] ?? "";
    expect([...block.matchAll(/^\s{4}(\w+):/gm)].map((m) => m[1])).toEqual(["getState", "dismiss"]);
    expect([...preload.matchAll(/invoke\(\s*"(handoff:[^"]+)"/g)].map((m) => m[1]).sort()).toEqual([
      "handoff:dismiss",
      "handoff:get-state",
    ]);
  });

  it("main never marks a session started: no session status or start request from the desktop", () => {
    expect(code("main/api-client.ts")).not.toMatch(/\/start|status:\s*["']active/);
  });
});

describe("the cold-start window never looks like a login prompt (D-56)", () => {
  it("the login screen shows the 'Opening your prepared session' notice while a handoff redeems", () => {
    const app = code("renderer/src/ui/App.tsx");
    // The login screen's banner is hidden for every phase EXCEPT failed and redeeming.
    expect(app).toContain('if (onLogin && handoff.phase !== "failed" && handoff.phase !== "redeeming") return null;');
    expect(app).toContain("Opening your prepared session");
  });
});
