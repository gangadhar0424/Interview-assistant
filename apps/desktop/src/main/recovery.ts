/**
 * Renderer crash recovery (M-5, D-70). Pure logic: Electron, the clock, timers and the failure dialog
 * come in through ports, so every path is unit-tested without Electron. Mocked tests cannot establish
 * how a real Windows renderer dies; that needs the manual checks in docs/DESKTOP_TROUBLESHOOTING.md.
 *
 * What it does when the renderer process dies (and the app is not quitting):
 *  1. stops everything that belonged to the dead window (`cleanup`: listening, held screenshots,
 *     running answers) before anything else;
 *  2. reloads the window ONCE, automatically, for that crash cycle;
 *  3. if the reload fails, the page crashes again, or crashes keep coming, it stops reloading and
 *     shows a native dialog the user can answer: Try again, Copy diagnostics, or Close.
 *
 * What it never does: start audio, take a screenshot, open or create a session, sign anyone in or
 * out. After a reload the renderer starts from its normal first screen; the account stays signed in
 * because the main process holds it. There is no port here that could start any of those things (a
 * guard test checks the source).
 *
 * Loop protection: at most one automatic reload within 60 s of the previous one, at most three in
 * ten minutes; a crash during a reload is a failure, not another reload; a burst of crash events
 * produces one cleanup and one reload. A user's "Try again" is a deliberate click and is the only
 * thing that can reload after the automatic attempt has been used up.
 *
 * Shutdown is never a crash: nothing happens when the app is quitting, when the window is already
 * gone, or for Electron's `clean-exit`.
 */
import { diagnostics as defaultDiagnostics, type DiagnosticsLog } from "./diagnostics";

export type CrashDetails = { reason?: string; exitCode?: number };
export type FailureChoice = "retry" | "copy" | "dismiss";

export type RecoveryPorts = {
  /** Reload the window's page (a fresh renderer process). May throw. */
  reload(): void;
  /** Stop and release everything the dead window owned. Idempotent. */
  cleanup(): void;
  /** True once the app is quitting. */
  isQuitting(): boolean;
  /** False once the window object is destroyed. */
  isWindowAlive(): boolean;
  /** The native failure dialog (never shows technical details). */
  promptFailure(): Promise<FailureChoice>;
  /** Put the diagnostics report on the clipboard. */
  copyDiagnostics(): void;
  /** Run `fn` after `ms`; returns a function that cancels it. */
  schedule(ms: number, fn: () => void): () => void;
  now(): number;
};

export const AUTO_RELOAD_GAP_MS = 60_000;
export const AUTO_RELOAD_BUDGET = 3;
export const AUTO_RELOAD_BUDGET_WINDOW_MS = 10 * 60_000;
export const LOAD_TIMEOUT_MS = 15_000;
/** Chromium's "load aborted" code (a navigation interrupted by another one is not a failure). */
export const LOAD_ABORTED = -3;

export type RecoveryState = "healthy" | "reloading" | "failed" | "stopped";

export class RendererRecovery {
  private state: RecoveryState = "healthy";
  private autoTimes: number[] = [];
  private cancelTimer: (() => void) | null = null;
  private prompting = false;
  private noticePending = false;
  private readonly counters = { crashes: 0, autoReloads: 0, manualReloads: 0, failures: 0 };

  constructor(
    private readonly ports: RecoveryPorts,
    private readonly log: DiagnosticsLog = defaultDiagnostics,
  ) {}

  snapshot(): { state: string; crashes: number; autoReloads: number; manualReloads: number; failures: number } {
    return { state: this.state, ...this.counters };
  }

  /** The "window was reloaded" notice, once. Reading it clears it. */
  takeNotice(): boolean {
    const pending = this.noticePending;
    this.noticePending = false;
    return pending;
  }

  /** The renderer process went away (`render-process-gone`). */
  onRendererGone(details: CrashDetails): void {
    if (this.state === "stopped") return;
    const reason = typeof details?.reason === "string" ? details.reason : "other";
    const exitCode = typeof details?.exitCode === "number" ? details.exitCode : undefined;
    if (reason === "clean-exit") {
      this.log.record("renderer_gone_ignored", { reason });
      return;
    }
    if (this.ports.isQuitting()) {
      this.log.record("renderer_gone_ignored", { reason: "quitting" });
      return;
    }
    if (!this.ports.isWindowAlive()) {
      this.log.record("renderer_gone_ignored", { reason: "window-gone" });
      return;
    }
    this.counters.crashes += 1;
    this.log.record("renderer_gone", { reason, ...(exitCode !== undefined ? { code: exitCode } : {}) });
    // Always first, before any reload decision: nothing the dead window started may keep running.
    this.safely(() => this.ports.cleanup());

    if (this.state === "reloading") {
      this.fail("crashed-again"); // it died again while we were reloading it
      return;
    }
    if (this.state === "failed") {
      // Already failed. If the dialog is open, never stack another; if the user dismissed it and the
      // renderer died again, ask again. Either way this never reloads by itself.
      if (!this.prompting) this.fail("crashed-again");
      return;
    }
    const reasonToHold = this.autoReloadBlocked();
    if (reasonToHold) {
      this.log.record("recovery_suppressed", { reason: reasonToHold });
      this.fail("crashed-again");
      return;
    }
    this.startReload("auto", true);
  }

  /** The main frame finished loading (`did-finish-load`). */
  onLoadFinished(): void {
    if (this.state !== "reloading") return;
    this.clearTimer();
    this.state = "healthy";
    this.log.record("recovery_succeeded");
  }

  /** The main frame failed to load (`did-fail-load`). */
  onLoadFailed(errorCode: number): void {
    if (this.state !== "reloading" || errorCode === LOAD_ABORTED) return;
    this.fail("load-failed", errorCode);
  }

  /** A deliberate user retry (the dialog's "Try again"). Only valid after a failure. */
  retry(): void {
    if (this.state !== "failed" || this.prompting) return;
    this.startReload("manual", true);
  }

  /**
   * The tray's "Reload window": a deliberate reload at any time except while one is already running
   * or after shutdown. Only a reload that follows a failure shows the "was reloaded" notice.
   */
  reloadNow(): void {
    if (this.state === "reloading" || this.state === "stopped" || this.prompting) return;
    this.startReload("manual", this.state === "failed");
  }

  /** Shutdown: cancel timers; every later event is ignored. */
  dispose(): void {
    this.state = "stopped";
    this.clearTimer();
  }

  // ------------------------------------------------------------------ internals

  private autoReloadBlocked(): "crashed-again" | "too-many-reloads" | null {
    const now = this.ports.now();
    this.autoTimes = this.autoTimes.filter((at) => now - at < AUTO_RELOAD_BUDGET_WINDOW_MS);
    const last = this.autoTimes[this.autoTimes.length - 1];
    if (last !== undefined && now - last < AUTO_RELOAD_GAP_MS) return "crashed-again";
    if (this.autoTimes.length >= AUTO_RELOAD_BUDGET) return "too-many-reloads";
    return null;
  }

  private startReload(mode: "auto" | "manual", notice = true): void {
    this.state = "reloading";
    if (notice) this.noticePending = true;
    if (mode === "auto") {
      this.counters.autoReloads += 1;
      this.autoTimes.push(this.ports.now());
    } else {
      this.counters.manualReloads += 1;
    }
    this.log.record("recovery_reload_started", {
      mode,
      code: mode === "auto" ? this.counters.autoReloads : this.counters.manualReloads,
    });
    this.clearTimer();
    this.cancelTimer = this.ports.schedule(LOAD_TIMEOUT_MS, () => {
      if (this.state === "reloading") this.fail("load-timeout");
    });
    try {
      this.ports.reload();
    } catch {
      this.fail("load-failed");
    }
  }

  private fail(cause: "crashed-again" | "load-failed" | "load-timeout", code?: number): void {
    this.clearTimer();
    if (this.state === "stopped") return;
    this.state = "failed";
    this.counters.failures += 1;
    this.log.record("recovery_failed", { reason: cause, ...(code !== undefined ? { code } : {}) });
    void this.prompt();
  }

  private async prompt(): Promise<void> {
    if (this.prompting) return;
    this.prompting = true;
    try {
      for (;;) {
        let choice: FailureChoice = "dismiss";
        try {
          choice = await this.ports.promptFailure();
        } catch {
          choice = "dismiss";
        }
        if (this.state === "stopped") return; // the app quit while the dialog was open
        this.log.record("recovery_prompt", { reason: choice });
        if (choice === "copy") {
          this.safely(() => this.ports.copyDiagnostics());
          continue; // the user may still retry or close
        }
        if (choice === "retry") {
          this.prompting = false;
          this.startReload("manual", true);
        }
        return;
      }
    } finally {
      this.prompting = false;
    }
  }

  private clearTimer(): void {
    this.cancelTimer?.();
    this.cancelTimer = null;
  }

  private safely(fn: () => void): void {
    try {
      fn();
    } catch {
      // Recovery must never throw into Electron's event dispatch.
    }
  }
}
