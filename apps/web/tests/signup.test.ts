// Phase 13A Batch 2: the signup request body and the optional invitation-code field. Run: npm test

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { buildSignupBody } from "../lib/signup.ts";

test("no invitation code is sent when the field is empty or blank", () => {
  assert.deepEqual(buildSignupBody("a@example.com", "pw"), { email: "a@example.com", password: "pw" });
  assert.deepEqual(buildSignupBody("a@example.com", "pw", ""), {
    email: "a@example.com",
    password: "pw",
  });
  assert.deepEqual(buildSignupBody("a@example.com", "pw", "   "), {
    email: "a@example.com",
    password: "pw",
  });
});

test("a typed invitation code is trimmed and sent", () => {
  assert.deepEqual(buildSignupBody("a@example.com", "pw", "  my-code  "), {
    email: "a@example.com",
    password: "pw",
    invite_code: "my-code",
  });
});

test("the signup form shows the field only for signup and hides the typed code", () => {
  const form = readFileSync(path.resolve(import.meta.dirname, "../components/AuthForm.tsx"), "utf8");
  assert.match(form, /name="inviteCode"/);
  assert.match(form, /isSignup && \(/);
  assert.match(form, /name="inviteCode"[\s\S]{0,200}type="password"|type="password"[\s\S]{0,200}name="inviteCode"/);
  const auth = readFileSync(path.resolve(import.meta.dirname, "../lib/auth.tsx"), "utf8");
  assert.match(auth, /buildSignupBody\(/);
});
