/**
 * M-5 renderer crash recovery: the RendererRecovery state machine driven with fake ports and a fake
 * clock/timer. These are mocked tests: they prove OUR logic. How a real Windows renderer dies, and
 * what the user sees, needs the manual checks in docs/DESKTOP_TROUBLESHOOTING.md.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DiagnosticsLog } from "../src/main/diagnostics";
import {
  AUTO_RELOAD_BUDGET,
  AUTO_RELOAD_GAP_MS,
  LOAD_ABORTED,
  LOAD_TIMEOUT_MS,
  RendererRecovery,
  type FailureChoice,
  type RecoveryPorts,
} from "../src/main/recovery";

function harness() {
  let clock = 1_000_000;
  const timers: Array<{ at: number; fn: () => void; cancelled: boolean }> = [];
  const state = { quitting: false, alive: true, reloadThrows: false };
  const calls: string[] = [];
  const prompts: Array<(choice: FailureChoice) => void> = [];
  const ports: RecoveryPorts = {
    reload: () => {
      calls.push("reload");
      if (state.reloadThrows) throw new Error("reload failed");
    },
    cleanup: () => void calls.push("cleanup"),
    isQuitting: () => state.quitting,
    isWindowAlive: () => state.alive,
    promptFailure: () => {
      calls.push("prompt");
      return new Promise<FailureChoice>((resolve) => prompts.push(resolve));
    },
    copyDiagnostics: () => void calls.push("copy"),
    schedule: (ms, fn) => {
      const timer = { at: clock + ms, fn, cancelled: false };
      timers.push(timer);
      return () => {
        timer.cancelled = true;
      };
    },
    now: () => clock,
  };
  const log = new DiagnosticsLog(() => clock);
  const recovery = new RendererRecovery(ports, log);
  const advance = (ms: number) => {
    clock += ms;
    for (const timer of timers) {
      if (!timer.cancelled && timer.at <= clock) {
        timer.cancelled = true;
        timer.fn();
      }
    }
  };
  /** Answer the dialog that is currently open and let the async prompt loop continue. */
  const answer = async (choice: FailureChoice) => {
    const resolve = prompts.shift();
    if (!resolve) throw new Error("no dialog is open");
    resolve(choice);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  };
  const count = (name: string) => calls.filter((c) => c === name).length;
  const events = () => log.recent(200).map((e) => `${e.type}${e.reason ? `:${e.reason}` : ""}`);
  return { recovery, ports, state, calls, prompts, advance, answer, count, events, log, now: () => clock };
}

const crash = { reason: "crashed", exitCode: 139 };
let h: ReturnType<typeof harness>;
beforeEach(() => {
  h = harness();
});

describe("crash detection and cleanup", () => {
  it("treats a dead renderer as a crash: cleans up first, then reloads once, with no dialog", () => {
    h.recovery.onRendererGone(crash);
    expect(h.calls).toEqual(["cleanup", "reload"]); // the dead window's work stops BEFORE the reload
    expect(h.recovery.snapshot()).toMatchObject({ state: "reloading", crashes: 1, autoReloads: 1 });
    expect(h.count("prompt")).toBe(0);
  });

  it("every crash reason that is not a clean exit counts", () => {
    for (const reason of ["crashed", "oom", "killed", "launch-failed", "integrity-failure", "abnormal-exit"]) {
      const x = harness();
      x.recovery.onRendererGone({ reason });
      expect(x.count("reload"), reason).toBe(1);
    }
  });

  it("records the crash with a reason and exit code, nothing else", () => {
    h.recovery.onRendererGone({ reason: "oom", exitCode: -1073741819 });
    const event = h.log.recent().find((e) => e.type === "renderer_gone");
    expect(event).toMatchObject({ reason: "oom", code: -1073741819 });
    expect(Object.keys(event ?? {}).sort()).toEqual(["at", "code", "reason", "type"]);
  });

  it("a burst of crash events while reloading is one cleanup-then-failure, never extra reloads", () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onRendererGone(crash);
    h.recovery.onRendererGone(crash);
    expect(h.count("reload")).toBe(1);
    expect(h.count("prompt")).toBe(1); // a crash during the reload is a failure, asked once
  });
});

describe("successful recovery", () => {
  it("becomes healthy when the page finishes loading and offers the notice exactly once", () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFinished();
    expect(h.recovery.snapshot().state).toBe("healthy");
    expect(h.recovery.takeNotice()).toBe(true);
    expect(h.recovery.takeNotice()).toBe(false);
    expect(h.events()).toEqual(["renderer_gone:crashed", "recovery_reload_started", "recovery_succeeded"]);
  });

  it("a normal page load with no crash does nothing", () => {
    h.recovery.onLoadFinished();
    h.recovery.onLoadFailed(-105);
    expect(h.calls).toEqual([]);
    expect(h.recovery.takeNotice()).toBe(false);
  });

  it("does nothing that could start a session, audio or a screenshot: the only effects are cleanup and reload", () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFinished();
    expect(new Set(h.calls)).toEqual(new Set(["cleanup", "reload"]));
  });
});

describe("recovery is attempted once, and loops are prevented", () => {
  it("a second crash soon after a successful reload is not reloaded again: the user is asked", () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFinished();
    h.advance(AUTO_RELOAD_GAP_MS - 1);
    h.recovery.onRendererGone(crash);
    expect(h.count("reload")).toBe(1);
    expect(h.count("prompt")).toBe(1);
    expect(h.count("cleanup")).toBe(2); // still cleaned up
    expect(h.events()).toContain("recovery_suppressed:crashed-again");
    expect(h.recovery.snapshot().state).toBe("failed");
  });

  it("a crash after the quiet period starts a new cycle with one automatic reload", () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFinished();
    h.advance(AUTO_RELOAD_GAP_MS + 1);
    h.recovery.onRendererGone(crash);
    expect(h.count("reload")).toBe(2);
    expect(h.count("prompt")).toBe(0);
  });

  it("a renderer that keeps crashing slowly is still capped (three automatic reloads in ten minutes)", () => {
    for (let i = 0; i < AUTO_RELOAD_BUDGET; i += 1) {
      h.recovery.onRendererGone(crash);
      h.recovery.onLoadFinished();
      h.advance(AUTO_RELOAD_GAP_MS + 1);
    }
    expect(h.count("reload")).toBe(AUTO_RELOAD_BUDGET);
    h.recovery.onRendererGone(crash);
    expect(h.count("reload")).toBe(AUTO_RELOAD_BUDGET); // the fourth is refused
    expect(h.events()).toContain("recovery_suppressed:too-many-reloads");
    expect(h.count("prompt")).toBe(1);
  });

  it("never reloads on its own once it has failed, however many crash events arrive", async () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onRendererGone(crash); // crashed again during reload -> failed, dialog open
    for (let i = 0; i < 5; i += 1) h.recovery.onRendererGone(crash);
    expect(h.count("reload")).toBe(1);
    expect(h.count("prompt")).toBe(1); // no stacked dialogs while one is open
  });
});

describe("failed recovery and the user's retry", () => {
  it("a load failure shows the dialog; a timeout does too; an aborted navigation is ignored", () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFailed(LOAD_ABORTED);
    expect(h.count("prompt")).toBe(0);
    h.recovery.onLoadFailed(-102);
    expect(h.count("prompt")).toBe(1);
    expect(h.events()).toContain("recovery_failed:load-failed");

    const x = harness();
    x.recovery.onRendererGone(crash);
    x.advance(LOAD_TIMEOUT_MS + 1);
    expect(x.count("prompt")).toBe(1);
    expect(x.events()).toContain("recovery_failed:load-timeout");
  });

  it("a reload that throws is a failure, not a crash of the main process", () => {
    h.state.reloadThrows = true;
    expect(() => h.recovery.onRendererGone(crash)).not.toThrow();
    expect(h.count("prompt")).toBe(1);
  });

  it("Try again reloads once per click, and success clears the failure and shows the notice", async () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFailed(-102);
    await h.answer("retry");
    expect(h.count("reload")).toBe(2);
    expect(h.recovery.snapshot()).toMatchObject({ state: "reloading", manualReloads: 1 });
    h.recovery.onLoadFinished();
    expect(h.recovery.snapshot().state).toBe("healthy");
    expect(h.recovery.takeNotice()).toBe(true);
  });

  it("a failed retry asks again; Close ends it without reloading", async () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFailed(-102);
    await h.answer("retry");
    h.recovery.onLoadFailed(-102); // the manual reload failed too
    expect(h.count("prompt")).toBe(2);
    await h.answer("dismiss");
    expect(h.count("reload")).toBe(3 - 1); // initial auto + one manual only
    expect(h.recovery.snapshot().state).toBe("failed");
  });

  it("Copy diagnostics copies and keeps the choice open", async () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFailed(-102);
    await h.answer("copy");
    expect(h.count("copy")).toBe(1);
    expect(h.count("prompt")).toBe(2); // the dialog came back
    expect(h.count("reload")).toBe(1);
    await h.answer("dismiss");
  });

  it("a crash after the user closed the dialog asks again instead of staying silent", async () => {
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFailed(-102);
    await h.answer("dismiss");
    h.recovery.onRendererGone(crash);
    expect(h.count("prompt")).toBe(2);
    expect(h.count("reload")).toBe(1); // still no automatic reload
  });

  it("the tray's Reload window is a deliberate reload, but not while one is running, and not after shutdown", () => {
    h.recovery.reloadNow();
    expect(h.count("reload")).toBe(1);
    expect(h.recovery.takeNotice()).toBe(false); // a healthy manual reload is not a "recovery"
    h.recovery.reloadNow(); // already reloading
    expect(h.count("reload")).toBe(1);
    h.recovery.onLoadFinished();
    h.recovery.dispose();
    h.recovery.reloadNow();
    expect(h.count("reload")).toBe(1);
  });

  it("a prompt that throws counts as Close", async () => {
    const x = harness();
    x.ports.promptFailure = () => Promise.reject(new Error("dialog failed"));
    x.recovery.onRendererGone(crash);
    x.recovery.onLoadFailed(-102);
    await Promise.resolve();
    await Promise.resolve();
    expect(x.recovery.snapshot().state).toBe("failed");
    expect(x.count("reload")).toBe(1);
  });
});

describe("shutdown is never a crash", () => {
  it("ignores the renderer dying while the app is quitting", () => {
    h.state.quitting = true;
    h.recovery.onRendererGone({ reason: "killed" });
    expect(h.calls).toEqual([]);
    expect(h.events()).toEqual(["renderer_gone_ignored:quitting"]);
    expect(h.recovery.snapshot().crashes).toBe(0);
  });

  it("ignores a clean exit and a window that is already gone", () => {
    h.recovery.onRendererGone({ reason: "clean-exit", exitCode: 0 });
    h.state.alive = false;
    h.recovery.onRendererGone(crash);
    expect(h.calls).toEqual([]);
    expect(h.events()).toEqual(["renderer_gone_ignored:clean-exit", "renderer_gone_ignored:window-gone"]);
  });

  it("dispose cancels the load timer and silences everything afterwards, including a dialog answer", async () => {
    h.recovery.onRendererGone(crash);
    h.recovery.dispose();
    h.advance(LOAD_TIMEOUT_MS * 2);
    expect(h.count("prompt")).toBe(0); // the timer never fired
    h.recovery.onRendererGone(crash);
    h.recovery.onLoadFailed(-102);
    expect(h.count("reload")).toBe(1);

    const x = harness();
    x.recovery.onRendererGone(crash);
    x.recovery.onLoadFailed(-102); // dialog open
    x.recovery.dispose(); // the app quits while it is open
    await x.answer("retry");
    expect(x.count("reload")).toBe(1); // the late answer reloads nothing
  });

  it("quitting while a reload is in flight raises no failure when the timer would have fired", () => {
    h.recovery.onRendererGone(crash);
    h.state.quitting = true;
    h.recovery.dispose();
    h.advance(LOAD_TIMEOUT_MS + 1);
    expect(h.count("prompt")).toBe(0);
  });
});

describe("robustness", () => {
  it("never throws into Electron when a port throws", () => {
    const x = harness();
    x.ports.cleanup = () => {
      throw new Error("cleanup failed");
    };
    expect(() => x.recovery.onRendererGone(crash)).not.toThrow();
    expect(x.count("reload")).toBe(1); // a failing cleanup does not block the recovery
    const y = harness();
    y.ports.copyDiagnostics = () => {
      throw new Error("clipboard failed");
    };
    y.recovery.onRendererGone(crash);
    y.recovery.onLoadFailed(-102);
    expect(async () => y.answer("copy")).not.toThrow();
  });

  it("tolerates malformed crash details", () => {
    const x = harness();
    expect(() => x.recovery.onRendererGone(undefined as never)).not.toThrow();
    expect(() => x.recovery.onRendererGone({ reason: 5 as never })).not.toThrow();
    vi.useRealTimers();
  });
});
