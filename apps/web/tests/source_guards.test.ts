// Source-level guards for the web app's security boundaries (Phase 7D).
// Imported job-page text and every API value must be rendered as plain text, tokens must never be
// persisted in browser storage, and the wizard must never send ownership fields.

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";

const ROOT = path.resolve(import.meta.dirname, "..");

function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
    const relative = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...sourceFiles(relative));
    else if (/\.(ts|tsx)$/.test(entry.name)) found.push(relative);
  }
  return found;
}

const APP_FILES = [...sourceFiles("app"), ...sourceFiles("components"), ...sourceFiles("lib")];

// Source text without comments, so a comment that explains a rule ("never use localStorage")
// does not trip the guard that enforces it. (URLs like https://... are kept: "//" after ":".)
function read(relative: string): string {
  const text = readFileSync(path.join(ROOT, relative), "utf-8");
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

test("the scanner sees the wizard sources", () => {
  for (const expected of ["components/SessionWizard.tsx", "components/SessionsPanel.tsx", "lib/wizard.ts"]) {
    assert.ok(APP_FILES.map((f) => f.replaceAll("\\", "/")).includes(expected), expected);
  }
});

test("no code path renders untrusted text as HTML", () => {
  for (const file of APP_FILES) {
    const text = read(file);
    for (const banned of ["dangerouslySetInnerHTML", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("]) {
      assert.equal(text.includes(banned), false, `${file} uses ${banned}`);
    }
  }
});

test("credentials are never persisted in browser storage", () => {
  for (const file of APP_FILES) {
    const text = read(file);
    for (const banned of ["localStorage", "sessionStorage", "indexedDB", "document.cookie"]) {
      assert.equal(text.includes(banned), false, `${file} uses ${banned}`);
    }
  }
});

test("the wizard never handles ownership, status or storage fields", () => {
  for (const file of ["lib/wizard.ts", "components/SessionWizard.tsx"]) {
    const text = read(file);
    for (const banned of ["user_id", "storage_key", "sha256", "extracted_text"]) {
      assert.equal(text.includes(banned), false, `${file} mentions ${banned}`);
    }
  }
});

test("wizard and dashboard requests all go through the authenticated client", () => {
  for (const file of ["components/SessionWizard.tsx", "components/SessionsPanel.tsx"]) {
    const text = read(file);
    assert.equal(/\bfetch\(/.test(text), false, `${file} calls fetch directly`);
    assert.ok(text.includes("authedRequest"), file);
    assert.equal(/https?:\/\/(?!jobs|example)/.test(text.replace(/http:\/\/ or https:\/\//g, "")), false, `${file} hard-codes a host`);
  }
});

test("every wizard request targets only the documented same-origin API paths", () => {
  const wizard = read("components/SessionWizard.tsx");
  const paths = [...wizard.matchAll(/"(\/api\/v1\/[^"]*)"/g)].map((m) => m[1]);
  assert.deepEqual([...new Set(paths)].sort(), [
    "/api/v1/ai/options",
    "/api/v1/documents?limit=200",
    "/api/v1/resumes?limit=200",
    "/api/v1/sessions",
    "/api/v1/sessions/job-import",
  ]);
});

// ------------------------------------------------------------------ Phase 8C: typed Q&A UI

test("the scanner sees the Q&A sources", () => {
  const files = APP_FILES.map((f) => f.replaceAll("\\", "/"));
  for (const expected of ["components/SessionChat.tsx", "lib/qa.ts", "app/sessions/[id]/page.tsx"]) {
    assert.ok(files.includes(expected), expected);
  }
});

test("the Q&A UI talks to the API only through the authenticated helpers", () => {
  const chat = read("components/SessionChat.tsx");
  assert.equal(/\bfetch\(/.test(chat), false, "SessionChat calls fetch directly");
  assert.ok(chat.includes("authedStream") && chat.includes("authedRequest"));
  assert.equal(/\bfetch\(/.test(read("lib/qa.ts")), false, "lib/qa.ts calls fetch directly");
  assert.equal(/\bEventSource\b/.test(chat + read("lib/qa.ts")), false, "EventSource cannot send the bearer token");
  // The streaming POST is the only place the stream helper is used, and it always aborts on unmount.
  assert.match(chat, /return \(\) => \{\s*mountedRef\.current = false;\s*abortRef\.current\?\.abort\(\);/, "unmount must abort the stream");
});

test("every Q&A request targets only the documented same-origin API paths", () => {
  const chat = read("components/SessionChat.tsx");
  const literals = [...chat.matchAll(/["`](\/api\/v1\/[^"`]*)["`]/g)].map((m) => m[1]);
  assert.deepEqual([...new Set(literals)].sort(), ["/api/v1/ai/options", "/api/v1/sessions/${sessionId}"]);
  const suffixes = [...chat.matchAll(/\$\{base\}(\/[a-z/]*)/g)].map((m) => m[1]);
  assert.deepEqual([...new Set(suffixes)].sort(), ["/questions"]);
});

test("the Q&A UI never handles ownership, prompt or storage fields and never logs", () => {
  for (const file of ["components/SessionChat.tsx", "lib/qa.ts", "app/sessions/[id]/page.tsx"]) {
    const text = read(file);
    for (const banned of ["user_id", "session_id", "storage_key", "generation_config", "retrieved_chunk_ids", "prompt", "console."]) {
      assert.equal(text.includes(banned), false, `${file} mentions ${banned}`);
    }
  }
});

test("server error text from the stream is never displayed", () => {
  const qa = read("lib/qa.ts");
  assert.equal(/o\["message"\]|\.message\b.*answer\.error/.test(qa.replace(/\/\/.*$/gm, "")), false);
  const chat = read("components/SessionChat.tsx");
  // `outcome.message` is allowed: it is text chosen by describeAskError, never server text.
  assert.equal(
    /\b(err|error|failure|payload|response)\??\.message\b/.test(chat),
    false,
    "SessionChat displays a server-provided message",
  );
});

test("the session route validates its id before using it in a request path", () => {
  assert.ok(read("app/sessions/[id]/page.tsx").includes("isUuid(id)"));
});
