// Phase 13A: the static web security headers and their wiring in next.config.ts. Run: npm test

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { CSP_NON_SCRIPT_DIRECTIVES, SECURITY_HEADERS } from "../lib/security-headers.ts";

const byKey = new Map(SECURITY_HEADERS.map((h) => [h.key, h.value]));

test("the expected security headers are defined", () => {
  assert.equal(byKey.get("X-Content-Type-Options"), "nosniff");
  assert.equal(byKey.get("X-Frame-Options"), "DENY");
  assert.equal(byKey.get("Referrer-Policy"), "strict-origin-when-cross-origin");
  assert.match(byKey.get("Permissions-Policy") ?? "", /microphone=\(\)/);
  assert.equal(byKey.get("Content-Security-Policy"), CSP_NON_SCRIPT_DIRECTIVES);
});

test("the CSP forbids framing, plugins and foreign form targets", () => {
  for (const directive of [
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
  ]) {
    assert.ok(CSP_NON_SCRIPT_DIRECTIVES.includes(directive), directive);
  }
});

test("the CSP does not restrict script or style loading (that would break Next.js without nonces)", () => {
  assert.doesNotMatch(CSP_NON_SCRIPT_DIRECTIVES, /script-src|style-src|default-src|connect-src/);
});

test("header names are unique and values contain no line breaks", () => {
  assert.equal(byKey.size, SECURITY_HEADERS.length);
  for (const { value } of SECURITY_HEADERS) assert.doesNotMatch(value, /[\r\n]/);
});

test("next.config.ts applies the headers to every path and hides the framework banner", () => {
  const config = readFileSync(path.resolve(import.meta.dirname, "../next.config.ts"), "utf8");
  assert.match(config, /SECURITY_HEADERS/);
  assert.match(config, /source:\s*"\/:path\*"/);
  assert.match(config, /poweredByHeader:\s*false/);
});
