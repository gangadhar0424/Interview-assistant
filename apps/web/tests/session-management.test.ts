// Unit tests for the pure session-management logic (M-9). Run: npm test
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import {
  CreateAttempt,
  DELETE_WARNING,
  EXPORT_WARNING,
  activityLabel,
  activityState,
  canComplete,
  canReopen,
  exportFileName,
  formatActivity,
  newIdempotencyKey,
  sessionTitle,
  type ManagedSession,
} from "../lib/session-management.ts";

const base: ManagedSession = {
  id: "11111111-2222-4333-8444-555555555555",
  title: null,
  company_name: "Acme",
  status: "ready",
  updated_at: "2026-01-01T00:00:00Z",
};
const make = (over: Partial<ManagedSession>): ManagedSession => ({ ...base, ...over });

test("state comes from the server, with a status fallback for an older server", () => {
  assert.equal(activityState(make({ activity_state: "interrupted" })), "interrupted");
  assert.equal(activityState(make({ status: "ready" })), "active");
  assert.equal(activityState(make({ status: "ended" })), "completed");
  assert.equal(activityState(make({ status: "archived" })), "completed");
  assert.equal(activityLabel("active"), "Active");
  assert.equal(activityLabel("completed"), "Completed");
  assert.equal(activityLabel("interrupted"), "Interrupted");
});

test("complete and reopen are offered only where the server allows them", () => {
  assert.equal(canComplete(make({ status: "ready" })), true);
  assert.equal(canComplete(make({ status: "ended" })), false);
  assert.equal(canComplete(make({ status: "archived" })), false);
  assert.equal(canReopen(make({ status: "ended" })), true);
  assert.equal(canReopen(make({ status: "ready" })), false);
  assert.equal(canReopen(make({ status: "archived" })), false);
});

test("a session is named by its title, else its company", () => {
  assert.equal(sessionTitle(make({ title: " Round 2 " })), "Round 2");
  assert.equal(sessionTitle(make({ title: "  " })), "Acme");
  assert.equal(sessionTitle(make({ title: null })), "Acme");
});

test("activity text is relative and never throws", () => {
  const now = Date.parse("2026-06-01T12:00:00Z");
  const ago = (ms: number) => new Date(now - ms).toISOString();
  assert.equal(formatActivity(ago(5_000), now), "just now");
  assert.equal(formatActivity(ago(5 * 60_000), now), "5 min ago");
  assert.equal(formatActivity(ago(3 * 3_600_000), now), "3 h ago");
  assert.equal(formatActivity(ago(4 * 86_400_000), now), "4 d ago");
  assert.equal(formatActivity("nonsense", now), "");
});

test("the export file name carries no company, title or user data", () => {
  const sid = "abcdef12-2222-4333-8444-555555555555";
  assert.equal(exportFileName(sid, "markdown"), "session-abcdef12.md");
  assert.equal(exportFileName(sid, "json"), "session-abcdef12.json");
});

test("the export and delete warnings say what happens", () => {
  assert.match(EXPORT_WARNING, /every question and answer/);
  assert.match(EXPORT_WARNING, /does not\s+contain your resume or document text/);
  assert.match(EXPORT_WARNING, /Nothing is sent anywhere/);
  assert.match(DELETE_WARNING, /cannot be\s+recovered/);
  assert.match(DELETE_WARNING, /resume and documents are not deleted/);
});

test("idempotency keys match the server's accepted shape and differ each time", () => {
  const shape = /^[A-Za-z0-9_.:-]{8,128}$/;
  const keys = new Set(Array.from({ length: 20 }, () => newIdempotencyKey()));
  assert.equal(keys.size, 20);
  for (const key of keys) assert.match(key, shape);
});

test("a retry of the same create reuses its key; a changed or finished one does not", () => {
  const attempt = new CreateAttempt();
  const first = attempt.keyFor({ company_name: "Acme" });
  assert.equal(attempt.keyFor({ company_name: "Acme" }), first);
  const changed = attempt.keyFor({ company_name: "Other" });
  assert.notEqual(changed, first);
  attempt.succeeded();
  assert.notEqual(attempt.keyFor({ company_name: "Other" }), changed);
});

// ---- source guards: the web UI asks first and sends nothing it should not -------------------

const read = (rel: string) => readFileSync(path.join(import.meta.dirname, "..", rel), "utf8");

test("delete and export ask for confirmation before any request", () => {
  const panel = read("components/SessionsPanel.tsx");
  const remove = panel.slice(panel.indexOf("const remove ="), panel.indexOf("const exportSession ="));
  assert.ok(remove.indexOf("window.confirm(DELETE_WARNING)") < remove.indexOf("authedRequest"));
  const exp = panel.slice(panel.indexOf("const exportSession ="), panel.indexOf("return (\n    <section"));
  assert.ok(exp.indexOf("window.confirm(EXPORT_WARNING)") < exp.indexOf("authedRequest"));
});

test("the panel never writes session content to storage, logs or the URL", () => {
  for (const rel of ["components/SessionsPanel.tsx", "lib/session-management.ts"]) {
    assert.doesNotMatch(read(rel), /localStorage|sessionStorage|indexedDB|console\./, rel);
  }
});

test("creating a session sends an Idempotency-Key and the panel starts no capture", () => {
  assert.match(read("components/SessionWizard.tsx"), /"Idempotency-Key": attempt\.current\.keyFor\(payload\)/);
  assert.doesNotMatch(read("components/SessionsPanel.tsx"), /getUserMedia|getDisplayMedia|MediaRecorder/);
});
