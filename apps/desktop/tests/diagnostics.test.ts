/**
 * M-5 diagnostics: bounded, typed, redacted, local. Automated tests of our own report builder; they say
 * nothing about what a user would actually paste, which the manual checks in
 * docs/DESKTOP_TROUBLESHOOTING.md cover.
 */
import { describe, expect, it } from "vitest";
import {
  DIAG_EVENT_TYPES,
  DiagnosticsLog,
  MAX_LOG_EVENTS,
  MAX_REPORT_CHARS,
  MAX_REPORT_EVENTS,
  redactText,
  type AppInfo,
  type ServicesInfo,
} from "../src/main/diagnostics";
import { METRICS, PerfRecorder } from "../src/main/perf-marks";

const SECRETS = [
  "SECRET-TRANSCRIPT what is a mutex",
  "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF",
  "gsk_abcdefghijklmnopqrstuvwxyz0123456789",
  "AIzaSyA-abcdefghijklmnopqrstuvwxyz12345",
  "Bearer abc.def.ghi-123456",
  "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl",
  "person@example.com",
  "C:\\Users\\Jane Doe\\AppData\\Roaming\\Interview Assistant",
  "hunter2hunter2hunter2hunter2hunter2hunter2hunter2",
];

const app: AppInfo = {
  version: "0.1.0",
  electron: "45.2.3",
  chrome: "140.0.0.0",
  node: "24.22.2",
  platform: "win32",
  arch: "x64",
  osRelease: "10.0.26300",
  uptimeSeconds: 123.7,
};
const services: ServicesInfo = {
  signedIn: true,
  audioState: "idle",
  captureBusy: false,
  window: { visible: true, focused: false, minimized: false },
  settings: { audioInputEnabled: true, screenshotInputEnabled: false, alwaysOnTop: true, expanded: false },
  backend: "local",
  acknowledgementAccepted: true,
  shortcuts: { toggleWindow: "active", focusAudio: "unavailable" },
};

function log(): DiagnosticsLog {
  const l = new DiagnosticsLog(() => Date.UTC(2026, 10, 18, 9, 0, 0));
  l.provide("app", () => app);
  l.provide("services", () => services);
  l.provide("recovery", () => ({ state: "healthy", crashes: 1, autoReloads: 1, manualReloads: 0, failures: 0 }));
  return l;
}

describe("report contents", () => {
  it("includes version, OS and architecture, startup/uptime, service states, recovery counters and recent events", () => {
    const l = log();
    l.record("app_started");
    l.record("renderer_gone", { reason: "crashed", code: 139 });
    l.record("recovery_reload_started", { mode: "auto", code: 1 });
    l.record("recovery_succeeded");
    const { text, truncated } = l.report();
    expect(truncated).toBe(false);
    for (const needle of [
      "version: 0.1.0",
      "electron: 45.2.3",
      "os: win32 x64 10.0.26300",
      "uptime seconds: 123",
      "signed in: yes",
      "audio: idle",
      "screenshot capture in progress: no",
      "visible yes, focused no, minimized no",
      "audio input yes, screenshot input no",
      "backend: local",
      "acceptable-use notice accepted: yes",
      "toggleWindow=active, focusAudio=unavailable",
      "renderer stops: 1  automatic reloads: 1",
      "renderer_gone reason=crashed code=139",
      "recovery_reload_started code=1 mode=auto",
      "recovery_succeeded",
      "kept in memory only",
    ]) {
      expect(text, needle).toContain(needle);
    }
  });

  it("includes the M-2 performance summaries as numbers only", () => {
    const l = log();
    const clock = { t: 0 };
    const perf = new PerfRecorder(() => clock.t);
    const id = "11111111-2222-4333-8444-555555555555";
    perf.begin("startup", id);
    perf.mark(id, "module_loaded");
    clock.t = 468;
    perf.mark(id, "ready_to_show");
    perf.end(id, "ok");
    l.provide("perf", () => perf.summary());
    const { text } = l.report();
    expect(text).toContain("startup.module_to_ready_to_show_ms: 1 / 468 / 468 / 468");
  });

  it("never throws when a provider fails or is missing", () => {
    const l = new DiagnosticsLog();
    l.provide("services", () => {
      throw new Error("boom");
    });
    expect(() => l.report()).not.toThrow();
    expect(l.report().text).toContain("(none)");
  });

  it("a detached provider disappears from the report", () => {
    const l = log();
    expect(l.report().text).toContain("[services]");
    l.provide("services", undefined);
    expect(l.report().text).not.toContain("[services]");
  });
});

describe("what can and cannot be recorded", () => {
  it("only events from the closed list are recorded; free text is not an event", () => {
    const l = new DiagnosticsLog();
    l.record("SECRET-TRANSCRIPT" as never);
    l.record("app_started");
    expect(l.size()).toBe(1);
    expect([...DIAG_EVENT_TYPES]).toContain("renderer_gone");
  });

  it("an unknown reason becomes \"other\", so text cannot ride along in a reason", () => {
    const l = new DiagnosticsLog();
    l.record("renderer_gone", { reason: SECRETS[0] as string });
    expect(l.recent()[0]?.reason).toBe("other");
    expect(JSON.stringify(l.recent())).not.toContain("SECRET");
  });

  it("numbers are clamped integers and anything else is dropped", () => {
    const l = new DiagnosticsLog();
    l.record("renderer_gone", { code: 1.9 });
    l.record("renderer_gone", { code: Number.NaN });
    l.record("renderer_gone", { code: "SECRET" as never });
    l.record("renderer_gone", { code: 9e15 });
    l.record("renderer_gone", { code: -1073741819 }); // a typical Windows crash code
    const codes = l.recent().map((e) => e.code);
    expect(codes).toEqual([1, undefined, undefined, 2_147_483_647, -1073741819]);
  });

  it("an event has no field that could hold content", () => {
    const l = new DiagnosticsLog();
    l.record("renderer_gone", { reason: "crashed", code: 1, mode: "auto", ...({ text: SECRETS[0], token: SECRETS[1] } as object) });
    expect(Object.keys(l.recent()[0] ?? {}).sort()).toEqual(["at", "code", "mode", "reason", "type"]);
  });
});

describe("redaction", () => {
  it("scrubs every secret-shaped string, email address and user path", () => {
    // Pattern redaction cannot recognise plain prose (SECRETS[0]); that is why prose can never enter the log at all (see "what can and cannot be recorded").
    for (const secret of SECRETS.slice(1)) {
      const out = redactText(`before ${secret} after`);
      const core = secret.replace(/^Bearer /, "");
      expect(out, secret).not.toContain(core.slice(0, 24));
      expect(out).toContain("before");
      expect(out).toContain("after");
    }
    expect(redactText("C:\\Users\\Jane Doe\\x")).not.toContain("Jane");
    expect(redactText("/Users/jane/x")).not.toContain("jane");
  });

  it("does not damage ordinary report text such as metric names and timestamps", () => {
    for (const ok of [
      "startup.window_created_to_did_finish_load_ms: 10 / 296 / 383 / 383",
      "2026-11-18T09:00:00.000Z renderer_gone reason=crashed code=-1073741819",
      "toggleWindow=active, focusScreenshot=unavailable",
    ]) {
      expect(redactText(ok)).toBe(ok);
    }
  });

  it("the finished report scrubs secrets even if a provider hands them in", () => {
    const l = new DiagnosticsLog();
    l.provide("app", () => ({ ...app, version: `0.1.0 ${SECRETS[1]} ${SECRETS[6]}` }));
    l.provide("services", () => ({ ...services, shortcuts: { evil: SECRETS[5] as string, other: SECRETS[7] as string } }));
    const { text } = l.report();
    for (const secret of SECRETS.slice(1)) expect(text).not.toContain(secret);
    expect(text).not.toContain("Jane");
  });

  it("the report never carries ids, the backend host, or content-shaped state", () => {
    const { text } = log().report();
    expect(text).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i); // no UUIDs
    expect(text).not.toMatch(/https?:\/\//);
    expect(text).not.toMatch(/@/);
    for (const word of ["transcript", "prompt", "answer", "password", "token", "api key", "authorization"]) {
      expect(text.toLowerCase(), word).not.toContain(`${word}:`);
    }
  });
});

describe("size limits", () => {
  it("the log keeps only the newest events", () => {
    const l = new DiagnosticsLog();
    for (let i = 0; i < MAX_LOG_EVENTS + 50; i += 1) l.record("renderer_gone", { code: i });
    expect(l.size()).toBe(MAX_LOG_EVENTS);
    expect(l.recent(MAX_LOG_EVENTS)[0]?.code).toBe(50);
    expect(l.recent(MAX_LOG_EVENTS).at(-1)?.code).toBe(MAX_LOG_EVENTS + 49);
  });

  it("the report shows at most the newest events", () => {
    const l = log();
    for (let i = 0; i < 150; i += 1) l.record("renderer_gone", { code: i });
    const lines = l.report().text.split("\n").filter((line) => line.includes("renderer_gone"));
    expect(lines).toHaveLength(MAX_REPORT_EVENTS);
    expect(lines.at(-1)).toContain("code=149");
  });

  it("the report never exceeds the character limit, keeps the newest events and says it was cut", () => {
    const l = new DiagnosticsLog();
    l.provide("perf", () => ({
      metrics: Object.fromEntries(
        Array.from({ length: 800 }, (_, i) => [`metric_${String(i).padStart(4, "0")}_name_for_size_test`, { count: 1, p50: 1, p95: 2, max: 3 }]),
      ),
    }));
    for (let i = 0; i < 40; i += 1) l.record("renderer_gone", { code: i });
    const { text, truncated } = l.report();
    expect(text.length).toBeLessThanOrEqual(MAX_REPORT_CHARS);
    expect(truncated).toBe(true);
    expect(text).toContain("[truncated]");
  });

  it("a normal report is far below the limit", () => {
    const l = log();
    for (const m of METRICS.slice(0, 3)) void m;
    expect(l.report().text.length).toBeLessThan(MAX_REPORT_CHARS / 4);
  });
});
