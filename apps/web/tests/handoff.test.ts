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
import {
  CHALLENGE_URL,
  PRESENCE_PORT,
  PRESENCE_TIMEOUT_MS,
  PRESENCE_URL,
  parseChallengeResponse,
} from "../lib/desktop-presence.ts";

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

// ------------------------------------------------------------------- presence check (D-60)
test("when the desktop app is not detected nothing is requested or launched", async () => {
  const h = harness({ left: true });
  let requested = false;
  h.deps.requestCode = async () => {
    requested = true;
    return { code: CODE, expires_in: 60 };
  };
  h.deps.detect = async () => false;
  assert.deepEqual(await launchDesktop(h.deps), { kind: "not-detected" });
  assert.equal(requested, false);
  assert.deepEqual(h.calls, []);
  assert.deepEqual(h.navigated, []);
});

test("when the desktop app is detected the normal launch runs", async () => {
  const h = harness({ left: true });
  h.deps.detect = async () => true;
  assert.deepEqual(await launchDesktop(h.deps), { kind: "opened" });
  assert.deepEqual(h.navigated, [`interviewassistant://start?h=${CODE}`]);
});

test("the presence beacon is a loopback URL on the documented port", () => {
  assert.equal(PRESENCE_URL, `http://127.0.0.1:${PRESENCE_PORT}/ia-presence`);
  assert.ok(PRESENCE_TIMEOUT_MS >= 200 && PRESENCE_TIMEOUT_MS <= 3000);
});

test("the only unauthenticated requests are the two fixed loopback calls: presence and challenge, no credentials", () => {
  const presence = source("lib/desktop-presence.ts");
  // Exactly two fetches (the presence beacon and, since SEC-08, the challenge), each to a fixed constant.
  assert.equal((presence.match(/fetch\(/g) ?? []).length, 2);
  assert.ok(presence.includes("fetch(PRESENCE_URL") && presence.includes("fetch(CHALLENGE_URL"));
  assert.equal((presence.match(/credentials: "omit"/g) ?? []).length, 2);
  assert.ok(presence.includes('mode: "no-cors"') && presence.includes('mode: "cors"'));
  assert.equal(PRESENCE_URL, `http://127.0.0.1:${PRESENCE_PORT}/ia-presence`);
  assert.equal(CHALLENGE_URL, `http://127.0.0.1:${PRESENCE_PORT}/ia-presence/challenge`);
  assert.equal(/console\.|localStorage|sessionStorage|document\.cookie/.test(presence), false);
});

// ------------------------------------------------------------ SEC-08: binding to the desktop's challenge
const CHALLENGE = "Zy9_-".repeat(8) + "abc";

test("a bound launch link adds the challenge and still carries no secret or session data", () => {
  const link = buildLaunchUrl(CODE, CHALLENGE) as string;
  assert.equal(link, `interviewassistant://start?h=${CODE}&c=${CHALLENGE}`);
  const url = new URL(link);
  assert.deepEqual([...url.searchParams.keys()], ["h", "c"]);
  assert.equal(url.username + url.password + url.hash, "");
  // The unbound link is unchanged.
  assert.equal(buildLaunchUrl(CODE, null), `interviewassistant://start?h=${CODE}`);
  assert.equal(buildLaunchUrl(CODE), `interviewassistant://start?h=${CODE}`);
});

test("a malformed challenge never produces a launch link", () => {
  for (const bad of ["", "short", "A".repeat(42), "A".repeat(44), "A".repeat(42) + "!", "&x=1" + "A".repeat(39)]) {
    assert.equal(buildLaunchUrl(CODE, bad), null, bad);
  }
});

test("the flow is detect -> fetch the challenge -> request a BOUND code -> open the link", async () => {
  const order: string[] = [];
  const navigated: string[] = [];
  let boundTo: string | null | undefined;
  const outcome = await launchDesktop({
    detect: async () => (order.push("detect"), true),
    fetchChallenge: async () => (order.push("challenge"), CHALLENGE),
    requestCode: async (challenge) => {
      order.push("code");
      boundTo = challenge;
      return { code: CODE, expires_in: 60 };
    },
    navigate: (url) => (order.push("navigate"), navigated.push(url)),
    watchForLeave: async () => (order.push("watch"), true),
  });
  assert.deepEqual(outcome, { kind: "opened" });
  assert.deepEqual(order, ["detect", "challenge", "code", "watch", "navigate"]);
  assert.equal(boundTo, CHALLENGE);
  assert.deepEqual(navigated, [`interviewassistant://start?h=${CODE}&c=${CHALLENGE}`]);
});

test("without a challenge (older desktop, refused or slow answer, or a throw) the unbound flow is used", async () => {
  for (const fetchChallenge of [
    async () => null,
    async (): Promise<string | null> => {
      throw new Error("refused");
    },
    async () => "not-a-challenge",
    undefined,
  ]) {
    const navigated: string[] = [];
    let boundTo: string | null | undefined;
    const outcome = await launchDesktop({
      ...(fetchChallenge ? { fetchChallenge } : {}),
      requestCode: async (challenge) => {
        boundTo = challenge;
        return { code: CODE, expires_in: 60 };
      },
      navigate: (url) => navigated.push(url),
      watchForLeave: async () => true,
    });
    assert.deepEqual(outcome, { kind: "opened" });
    assert.equal(boundTo, null);
    assert.deepEqual(navigated, [`interviewassistant://start?h=${CODE}`]);
  }
});

test("no challenge is requested when the desktop app is not running", async () => {
  let asked = false;
  const outcome = await launchDesktop({
    detect: async () => false,
    fetchChallenge: async () => ((asked = true), CHALLENGE),
    requestCode: async () => ({ code: CODE, expires_in: 60 }),
    navigate: () => assert.fail("must not navigate"),
    watchForLeave: async () => true,
  });
  assert.deepEqual(outcome, { kind: "not-detected" });
  assert.equal(asked, false);
});

test("a server that requires binding gives a fixed 'update the desktop app' message, never server text", () => {
  const message = describeHandoffError(new ApiError(422, "challenge_required", "SECRET server text"));
  assert.equal(message, "Please update the desktop app, then try again.");
  assert.equal(message.includes("SECRET"), false);
});

test("the beacon's answer is accepted only when it is exactly a well-formed challenge", () => {
  assert.equal(parseChallengeResponse({ challenge: CHALLENGE, expires_in: 120 }), CHALLENGE);
  for (const bad of [null, undefined, 42, "x", [], {}, { challenge: 1 }, { challenge: "short" }, { challenge: CHALLENGE + "A" }, { challenge: "<script>" }]) {
    assert.equal(parseChallengeResponse(bad), null);
  }
});

test("the page never handles a verifier, and never stores or logs the challenge or the code", () => {
  for (const file of ["lib/handoff.ts", "lib/desktop-presence.ts", "components/DesktopHandoff.tsx"]) {
    const text = source(file);
    assert.equal(/verifier/i.test(text), false, `${file} must not mention a verifier`);
    assert.equal(/console\.|localStorage|sessionStorage|indexedDB|document\.cookie/.test(text), false, file);
  }
  // The challenge goes to the API in the request BODY (authedRequest `body`), not in a URL.
  const component = source("components/DesktopHandoff.tsx");
  assert.ok(component.includes("body: { code_challenge: challenge }"));
  assert.equal(/handoff\?[^`'"]*challenge/.test(component), false);
});
