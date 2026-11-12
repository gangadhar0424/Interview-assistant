// Tests for the web -> desktop handoff (D-55): launch link, install fallback, error handling and
// source-level guards. Run: npm test (Node's built-in runner, no extra dependencies).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { ApiError } from "../lib/api.ts";
import {
  CODE_PATTERN,
  LAUNCH_WAIT_MS,
  buildLaunchUrl,
  describeHandoffError,
  launchDesktop,
  parseDownloadUrl,
  type LaunchDeps,
} from "../lib/handoff.ts";

const ROOT = path.resolve(import.meta.dirname, "..");
const CODE = "A".repeat(43);

function source(relative: string): string {
  const text = readFileSync(path.join(ROOT, relative), "utf-8");
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

type Harness = { deps: LaunchDeps; calls: string[]; navigated: string[] };

function harness(options: { left: boolean; issue?: () => Promise<{ code: string; expires_in: number }> }): Harness {
  const calls: string[] = [];
  const navigated: string[] = [];
  const deps: LaunchDeps = {
    requestCode: options.issue ?? (async () => ({ code: CODE, expires_in: 60 })),
    navigate: (url) => {
      calls.push("navigate");
      navigated.push(url);
    },
    watchForLeave: async () => {
      calls.push("watch");
      return options.left;
    },
  };
  return { deps, calls, navigated };
}

// ------------------------------------------------------------------------------ launch link
test("the launch link contains only the opaque code", () => {
  assert.equal(buildLaunchUrl(CODE), `interviewassistant://start?h=${CODE}`);
  const url = new URL(buildLaunchUrl(CODE) as string);
  assert.equal(url.protocol, "interviewassistant:");
  assert.deepEqual([...url.searchParams.keys()], ["h"]);
  assert.equal(url.username + url.password, "");
});

test("malformed codes never produce a launch link", () => {
  for (const bad of ["", "short", "A".repeat(42), "A".repeat(44), "A".repeat(42) + "!", "../".repeat(15), "é".repeat(43)]) {
    assert.equal(buildLaunchUrl(bad), null, bad);
  }
  assert.ok(CODE_PATTERN.test("a-_".repeat(14) + "a"));
});

// ---------------------------------------------------------------------------- download url
test("the download URL is configurable and only an https URL without credentials is used", () => {
  assert.equal(parseDownloadUrl("https://downloads.example.com/app.exe"), "https://downloads.example.com/app.exe");
  for (const bad of [undefined, "", "   ", "http://example.com/a.exe", "javascript:alert(1)", "file:///c:/a.exe", "not a url", "https://user:pw@example.com/a.exe", "//example.com/a.exe"]) {
    assert.equal(parseDownloadUrl(bad), null, String(bad));
  }
});

// -------------------------------------------------------------------------- launch + fallback
test("a launch where the page loses focus is reported as opened", async () => {
  const h = harness({ left: true });
  assert.deepEqual(await launchDesktop(h.deps), { kind: "opened" });
  assert.deepEqual(h.navigated, [`interviewassistant://start?h=${CODE}`]);
});

test("the focus watcher starts before the navigation so a fast app launch is not missed", async () => {
  const h = harness({ left: true });
  await launchDesktop(h.deps);
  assert.deepEqual(h.calls, ["watch", "navigate"]);
});

test("a launch where the page stays in front shows the install/retry fallback", async () => {
  const h = harness({ left: false });
  assert.deepEqual(await launchDesktop(h.deps), { kind: "not-opened" });
});

test("the wait before the fallback is bounded", async () => {
  let waited = -1;
  const h = harness({ left: false });
  h.deps.watchForLeave = async (ms) => {
    waited = ms;
    return false;
  };
  await launchDesktop(h.deps);
  assert.equal(waited, LAUNCH_WAIT_MS);
  assert.ok(LAUNCH_WAIT_MS >= 1000 && LAUNCH_WAIT_MS <= 5000);
});

test("each attempt requests a fresh code (retry never reuses a code)", async () => {
  let n = 0;
  const h = harness({
    left: false,
    issue: async () => ({ code: String.fromCharCode(65 + n++).repeat(43), expires_in: 60 }),
  });
  await launchDesktop(h.deps);
  await launchDesktop(h.deps);
  assert.deepEqual(h.navigated, [
    `interviewassistant://start?h=${"A".repeat(43)}`,
    `interviewassistant://start?h=${"B".repeat(43)}`,
  ]);
});

test("an API failure never launches and shows only fixed text", async () => {
  const secret = "internal detail: handoff:deadbeef at 10.0.0.5";
  const h = harness({
    left: true,
    issue: async () => {
      throw new ApiError(500, "internal_error", secret);
    },
  });
  const outcome = await launchDesktop(h.deps);
  assert.equal(outcome.kind, "error");
  assert.equal(h.navigated.length, 0);
  assert.equal(JSON.stringify(outcome).includes("deadbeef"), false);
  assert.equal(JSON.stringify(outcome).includes(secret), false);
});

test("a malformed code from the server is never launched", async () => {
  const h = harness({ left: true, issue: async () => ({ code: "not-a-code", expires_in: 60 }) });
  assert.equal((await launchDesktop(h.deps)).kind, "error");
  assert.equal(h.navigated.length, 0);
});

test("error messages are fixed, never server-provided", () => {
  assert.match(describeHandoffError(new ApiError(404, "session_not_found", "Session not found.")), /not found/);
  assert.match(describeHandoffError(new ApiError(409, "session_not_startable", "x")), /cannot be started/);
  assert.match(describeHandoffError(new ApiError(429, "rate_limited", "x", [], 12)), /12 seconds/);
  assert.match(describeHandoffError(new ApiError(429, "rate_limited", "x")), /wait a moment/);
  assert.match(describeHandoffError(new ApiError(503, "handoff_unavailable", "x")), /temporarily unavailable/);
  assert.match(describeHandoffError(new ApiError(401, "unauthorized", "x")), /sign in/i);
  assert.match(describeHandoffError(new Error("boom: handoff:abc")), /Could not start/);
  assert.equal(describeHandoffError(new ApiError(500, "e", "SECRET-DETAIL")).includes("SECRET-DETAIL"), false);
});

// ------------------------------------------------------------------------------ source guards
test("the handoff UI uses the authenticated client, never logs, and never renders the code", () => {
  const component = source("components/DesktopHandoff.tsx");
  const lib = source("lib/handoff.ts");
  assert.equal(/\bfetch\(/.test(component + lib), false, "calls fetch directly");
  assert.ok(component.includes("authedRequest"));
  assert.equal(/console\./.test(component + lib), false, "logs");
  for (const banned of ["localStorage", "sessionStorage", "dangerouslySetInnerHTML", "innerHTML"]) {
    assert.equal((component + lib).includes(banned), false, banned);
  }
  // The code is only ever passed to buildLaunchUrl/navigate; it is not part of any rendered text.
  assert.equal(/\{[^}]*\.code[^}]*\}/.test(component.split("return (")[1] ?? ""), false, "renders the code");
});

test("the handoff request targets only the documented same-origin path", () => {
  const component = source("components/DesktopHandoff.tsx");
  const paths = [...component.matchAll(/["`](\/api\/v1\/[^"`]*)["`]/g)].map((m) => m[1]);
  assert.deepEqual([...new Set(paths)], ["/api/v1/sessions/${sessionId}/handoff"]);
});

test("the installer URL is configuration, never hard-coded", () => {
  const component = source("components/DesktopHandoff.tsx");
  assert.ok(component.includes("process.env.NEXT_PUBLIC_DESKTOP_DOWNLOAD_URL"));
  assert.equal(/https?:\/\/(?!$)/.test(component), false, "hard-coded host");
  assert.ok(readFileSync(path.resolve(ROOT, "../../.env.example"), "utf-8").includes("NEXT_PUBLIC_DESKTOP_DOWNLOAD_URL"));
});

test("the session page validates its id before showing the handoff button", () => {
  const page = source("app/sessions/[id]/page.tsx");
  assert.ok(page.indexOf("isUuid(id)") < page.indexOf("<DesktopHandoff"));
});

// ----------------------------------------------------------- local installer + dashboard actions
test("a loopback http installer URL is accepted for local development, other http hosts are not", () => {
  assert.equal(parseDownloadUrl("http://localhost:3000/downloads/app.exe"), "http://localhost:3000/downloads/app.exe");
  assert.equal(parseDownloadUrl("http://127.0.0.1:3000/downloads/app.exe"), "http://127.0.0.1:3000/downloads/app.exe");
  for (const bad of ["http://example.com/a.exe", "http://192.168.1.5/a.exe", "http://localhost.evil.com/a.exe", "http://user:pw@localhost/a.exe", "ftp://localhost/a.exe"]) {
    assert.equal(parseDownloadUrl(bad), null, bad);
  }
});

test("each ready session on the dashboard has Start Interview and Open session actions", () => {
  const panel = source("components/SessionsPanel.tsx");
  assert.ok(panel.includes("<DesktopHandoff sessionId={item.id} compact />"));
  assert.match(panel, /item\.status === "ready" && <DesktopHandoff/);
  assert.ok(panel.includes("Open session"));
  assert.equal(/\bfetch\(/.test(panel), false);
});

test("creating a session goes straight to it, after validating the id", () => {
  const wizard = source("components/SessionWizard.tsx");
  assert.ok(wizard.includes("isUuid(created.id)"));
  assert.ok(wizard.includes("`/sessions/${created.id}`"));
  assert.ok(wizard.includes('"/home"')); // safe fallback
});
