import {
  evaluateUpdateConfig,
  MANIFEST_SIGNATURE_FILE,
  parseAppUpdateYml,
  parseLatestYml,
  parseManifestPublicKey,
  requiredFreeBytes,
  validateManifest,
  verifyManifestSignature,
} from "../shared/update-manifest.mjs";
import {
  UPDATE_ERROR_MESSAGES,
  type UpdateDisabledReason,
  type UpdateErrorCode,
  type UpdateState,
} from "../shared/updates";

/**
 * Secure, user-controlled update manager (M-11, D-78).
 *
 * What it guarantees (each pinned by tests/updater.test.ts):
 *  - It can only run in a packaged, SIGNED build whose update feed is https and whose publisher is pinned.
 *  - A metadata check never downloads. Downloading and installing happen only on an explicit user action.
 *  - The announced manifest is validated before the engine is asked for anything, and the engine's
 *    own announcement must match it; a downgrade or the same version is never offered.
 *  - Only one operation runs at a time; repeated or out-of-order engine events are ignored.
 *  - Failures end in a fixed, content-free error state; retries are bounded and use backoff with jitter.
 *  - Nothing here starts audio or screen capture. Before installing, the host stops anything active.
 *
 * The installer's signature and checksum are verified by the engine (electron-updater) against the
 * publisher pinned in the build; this class never executes a downloaded file itself.
 */

export type EngineEvent =
  | { type: "update-available"; version: string }
  | { type: "update-not-available" }
  | { type: "download-progress"; percent: number }
  | { type: "update-downloaded"; version: string; downloadedFile?: string }
  | { type: "error"; code: UpdateErrorCode };

export interface UpdateEngine {
  /** Apply the safe configuration (no auto download, no install on quit, no downgrade, no pre-release). */
  configure(): void;
  subscribe(listener: (event: EngineEvent) => void): () => void;
  checkForUpdates(): Promise<void>;
  downloadUpdate(): Promise<void>;
  cancelDownload(): void;
  quitAndInstall(): void;
}

export class UpdateFetchError extends Error {
  constructor(readonly kind: "network" | "timeout" | "status" | "too_large" | "redirect", readonly status?: number) {
    super(`update fetch failed: ${kind}`);
    this.name = "UpdateFetchError";
  }
}

export type TimerHandle = { cancel(): void };

export type UpdateDeps = {
  engine: UpdateEngine;
  currentVersion: string;
  isPackaged: boolean;
  disabledByEnvironment: boolean;
  /** Text of `app-update.yml` shipped inside the installed app, or null when absent. */
  readUpdateConfig(): string | null;
  /** GET the manifest; must refuse redirects and cap the size. Throws UpdateFetchError. */
  fetchManifest(url: string, timeoutMs: number): Promise<string>;
  /** GET the detached manifest signature (`latest.yml.sig`); same rules as `fetchManifest`. */
  fetchSignature(url: string, timeoutMs: number): Promise<string>;
  /** The Ed25519 public key (base64 SPKI) built into this installer, or null when absent. */
  readManifestKey(): string | null;
  /** SHA-512 (base64) of a file on disk; rejects when it cannot be read. */
  hashFile(path: string): Promise<string>;
  freeDiskBytes(): Promise<number>;
  /** Stop listening, discard held screenshots and cancel running answers before the app quits. */
  prepareForInstall(): Promise<void> | void;
  now(): number;
  random(): number;
  setTimer(callback: () => void, ms: number): TimerHandle;
  emit(state: UpdateState): void;
  log(event: string, data?: Record<string, string | number | boolean>): void;
};

export const UPDATE_TIMINGS = Object.freeze({
  startupDelayMs: 30_000,
  periodicMs: 6 * 60 * 60 * 1000,
  periodicJitterMs: 10 * 60 * 1000,
  autoMinIntervalMs: 30 * 60 * 1000,
  manualMinIntervalMs: 10_000,
  manifestTimeoutMs: 15_000,
  engineCheckTimeoutMs: 30_000,
  downloadStallMs: 90_000,
  installWatchdogMs: 45_000,
  retryBaseMs: 60_000,
  retryCapMs: 15 * 60_000,
  maxAutoRetries: 3,
});

export type CheckTrigger = "startup" | "periodic" | "manual" | "retry";

/** Map an engine/OS error to a fixed code. Raw messages and URLs are never shown or stored. */
export function classifyEngineError(error: unknown): UpdateErrorCode {
  const text = `${(error as { code?: unknown })?.code ?? ""} ${(error as { message?: unknown })?.message ?? ""}`;
  if (/ENOSPC|no space|disk full/i.test(text)) return "insufficient_disk_space";
  if (/sha512|sha-512|checksum|signature|publisher|authenticode|not signed|hash/i.test(text)) return "verification_failed";
  if (/ENOTFOUND|ECONNREFUSED|ECONNRESET|EAI_AGAIN|ENETUNREACH|ERR_INTERNET|ERR_NETWORK|net::|network/i.test(text)) return "network";
  if (/ETIMEDOUT|timed? ?out|timeout/i.test(text)) return "timeout";
  if (/\b(404|403|429|5\d\d)\b/.test(text)) return "feed_unavailable";
  return "unknown";
}

function withTimeout<T>(work: Promise<T>, ms: number, deps: Pick<UpdateDeps, "setTimer">): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const timer = deps.setTimer(() => {
      if (settled) return;
      settled = true;
      reject(Object.assign(new Error("timeout"), { code: "ETIMEDOUT" }));
    }, ms);
    work.then(
      (value) => {
        if (settled) return;
        settled = true;
        timer.cancel();
        resolve(value);
      },
      (error: unknown) => {
        if (settled) return;
        settled = true;
        timer.cancel();
        reject(error);
      },
    );
  });
}

/** Whether an install request is valid in this state (also after a failed restart). */
export function canInstallNow(state: UpdateState): boolean {
  return state.status === "downloaded" || (state.status === "error" && state.code === "install_failed");
}

const RETRYABLE: ReadonlySet<UpdateErrorCode> = new Set(["network", "feed_unavailable", "timeout", "download_failed", "unknown"]);

export class UpdateManager {
  private state: UpdateState;
  private feedUrl: string | null = null;
  private lastCheckedAt: string | null = null;
  private lastCheckStartedMs = Number.NEGATIVE_INFINITY;
  private announced: { version: string; size: number } | null = null;
  /** SEC-09: the SHA-512 of the installer as stated by the SIGNATURE-VERIFIED manifest. */
  private signedSha512: string | null = null;
  private manifestKey: string | null = null;
  private verifying: Promise<void> | null = null;
  private downloadedVersion: string | null = null;
  private inFlight: Promise<void> | null = null;
  private retryAttempt = 0;
  private timers = new Set<TimerHandle>();
  private unsubscribe: (() => void) | null = null;
  private stallTimer: TimerHandle | null = null;
  private rejectStall: ((reason: Error) => void) | null = null;
  private installTimer: TimerHandle | null = null;
  private disposed = false;

  constructor(private readonly deps: UpdateDeps) {
    this.state = this.evaluatePolicy();
  }

  getState(): UpdateState {
    return this.state;
  }

  isEnabled(): boolean {
    return this.state.status !== "disabled";
  }

  private evaluatePolicy(): UpdateState {
    const disabled = (reason: UpdateDisabledReason): UpdateState => ({ status: "disabled", reason });
    if (this.deps.disabledByEnvironment) return disabled("disabled_by_environment");
    if (!this.deps.isPackaged) return disabled("development");
    const text = this.deps.readUpdateConfig();
    if (text === null) return disabled("not_configured");
    let verdict;
    try {
      verdict = evaluateUpdateConfig(parseAppUpdateYml(text));
    } catch {
      return disabled("not_configured");
    }
    if (!verdict.enabled || verdict.feedUrl === null) return disabled(verdict.reason ?? "not_configured");
    // SEC-09: a feed without a pinned manifest-signing key could be rolled back by whoever writes to
    // it, so such a build never self-updates (fail closed).
    const key = this.deps.readManifestKey();
    if (key === null || parseManifestPublicKey(key) === null) return disabled("not_configured");
    this.manifestKey = key;
    this.feedUrl = verdict.feedUrl;
    return { status: "idle", lastCheckedAt: null };
  }

  /** Configure the engine and schedule the quiet first check and the periodic ones. */
  start(): void {
    if (!this.isEnabled() || this.unsubscribe) return;
    this.deps.engine.configure();
    this.unsubscribe = this.deps.engine.subscribe((event) => this.onEngineEvent(event));
    this.schedule(UPDATE_TIMINGS.startupDelayMs, "startup");
    this.deps.log("update_manager_started");
  }

  dispose(): void {
    this.disposed = true;
    for (const timer of this.timers) timer.cancel();
    this.timers.clear();
    this.stallTimer?.cancel();
    this.installTimer?.cancel();
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  // ---- scheduling ----------------------------------------------------------------------------

  private schedule(ms: number, trigger: CheckTrigger): void {
    if (this.disposed) return;
    const timer = this.deps.setTimer(() => {
      this.timers.delete(timer);
      void this.check(trigger).finally(() => {
        if (trigger === "startup" || trigger === "periodic") {
          this.schedule(UPDATE_TIMINGS.periodicMs + Math.floor(this.deps.random() * UPDATE_TIMINGS.periodicJitterMs), "periodic");
        }
      });
    }, ms);
    this.timers.add(timer);
  }

  private scheduleRetry(): void {
    if (this.retryAttempt >= UPDATE_TIMINGS.maxAutoRetries) return;
    const base = Math.min(UPDATE_TIMINGS.retryBaseMs * 2 ** this.retryAttempt, UPDATE_TIMINGS.retryCapMs);
    this.retryAttempt += 1;
    const delay = base + Math.floor(this.deps.random() * base * 0.1);
    const timer = this.deps.setTimer(() => {
      this.timers.delete(timer);
      void this.check("retry");
    }, delay);
    this.timers.add(timer);
  }

  // ---- state ---------------------------------------------------------------------------------

  private set(next: UpdateState): void {
    if (JSON.stringify(next) === JSON.stringify(this.state)) return;
    this.state = next;
    this.deps.emit(next);
  }

  private fail(code: UpdateErrorCode): void {
    this.stallTimer?.cancel();
    this.stallTimer = null;
    this.set({ status: "error", code, message: UPDATE_ERROR_MESSAGES[code], retryable: RETRYABLE.has(code) });
    this.deps.log("update_error", { code });
  }

  private busy(): boolean {
    return this.state.status === "checking" || this.state.status === "downloading" || this.state.status === "installing";
  }

  // ---- check ---------------------------------------------------------------------------------

  async check(trigger: CheckTrigger = "manual"): Promise<UpdateState> {
    if (!this.isEnabled() || this.disposed) return this.state;
    if (this.inFlight || this.busy()) return this.state; // repeated or concurrent requests collapse into the running one
    // A downloaded update is never replaced by a new check; an announced one only by an explicit manual check.
    if (this.state.status === "downloaded") return this.state;
    if (trigger !== "manual" && this.state.status === "available") return this.state;
    const now = this.deps.now();
    const minGap = trigger === "manual" ? UPDATE_TIMINGS.manualMinIntervalMs : UPDATE_TIMINGS.autoMinIntervalMs;
    if (trigger !== "retry" && now - this.lastCheckStartedMs < minGap) return this.state;
    this.lastCheckStartedMs = now;
    this.set({ status: "checking" });
    this.inFlight = this.runCheck(trigger).finally(() => {
      this.inFlight = null;
    });
    await this.inFlight;
    return this.state;
  }

  private async runCheck(trigger: CheckTrigger): Promise<void> {
    if (this.feedUrl === null) return this.fail("unknown");
    let manifestText: string;
    try {
      // The fetcher has its own timeout; this outer bound means a misbehaving fetcher can never wedge a check.
      manifestText = await withTimeout(
        this.deps.fetchManifest(`${this.feedUrl}latest.yml`, UPDATE_TIMINGS.manifestTimeoutMs),
        UPDATE_TIMINGS.manifestTimeoutMs + 5_000,
        this.deps,
      );
    } catch (error) {
      const code = this.fetchErrorCode(error);
      this.fail(code);
      if (trigger !== "manual" && RETRYABLE.has(code)) this.scheduleRetry();
      return;
    }
    // SEC-09: authenticate the manifest BEFORE trusting a single field of it.
    let signatureText: string;
    try {
      signatureText = await withTimeout(
        this.deps.fetchSignature(`${this.feedUrl}${MANIFEST_SIGNATURE_FILE}`, UPDATE_TIMINGS.manifestTimeoutMs),
        UPDATE_TIMINGS.manifestTimeoutMs + 5_000,
        this.deps,
      );
    } catch (error) {
      const code = this.fetchErrorCode(error);
      this.fail(code);
      if (trigger !== "manual" && RETRYABLE.has(code)) this.scheduleRetry();
      return;
    }
    if (!verifyManifestSignature(manifestText, signatureText, this.manifestKey)) {
      return this.fail("verification_failed");
    }
    let verdict;
    try {
      verdict = validateManifest(parseLatestYml(manifestText), { currentVersion: this.deps.currentVersion });
    } catch {
      return this.fail("manifest_invalid");
    }
    if (verdict.errors.length > 0 || verdict.version === null) return this.fail("manifest_invalid");
    this.lastCheckedAt = new Date(this.deps.now()).toISOString();
    this.retryAttempt = 0;
    if (!verdict.newer) {
      this.set({ status: "none", checkedAt: this.lastCheckedAt });
      return;
    }
    const entry = parseLatestYml(manifestText).files[0];
    this.announced = { version: verdict.version, size: entry?.size ?? 0 };
    this.signedSha512 = entry?.sha512 ?? null;
    try {
      await withTimeout(this.deps.engine.checkForUpdates(), UPDATE_TIMINGS.engineCheckTimeoutMs, this.deps);
    } catch (error) {
      this.announced = null;
      const code = classifyEngineError(error);
      this.fail(code === "unknown" ? "feed_unavailable" : code);
      return;
    }
    // The engine announces through events, which have already arrived by now.
    if (this.state.status === "checking") {
      this.announced = null;
      this.fail("version_mismatch");
    }
  }

  private fetchErrorCode(error: unknown): UpdateErrorCode {
    if (error instanceof UpdateFetchError) {
      if (error.kind === "timeout") return "timeout";
      if (error.kind === "network") return "network";
      if (error.kind === "status") return "feed_unavailable";
      return "manifest_invalid"; // too large or redirected: refused
    }
    return classifyEngineError(error);
  }

  // ---- engine events -------------------------------------------------------------------------

  private onEngineEvent(event: EngineEvent): void {
    if (this.disposed) return;
    switch (event.type) {
      case "update-available": {
        if (this.state.status !== "checking" || this.announced === null) return; // unsolicited or duplicate
        if (event.version !== this.announced.version) {
          this.announced = null;
          this.fail("version_mismatch");
          return;
        }
        this.set({ status: "available", version: this.announced.version, sizeBytes: this.announced.size });
        return;
      }
      case "update-not-available": {
        if (this.state.status !== "checking") return;
        if (this.announced !== null) {
          this.announced = null;
          this.fail("version_mismatch"); // the manifest claimed a newer version the engine does not see
        }
        return;
      }
      case "download-progress": {
        if (this.state.status !== "downloading") return;
        const percent = Math.max(0, Math.min(99, Math.floor(event.percent)));
        this.armStallTimer();
        if (percent > this.state.percent) this.set({ status: "downloading", version: this.state.version, percent });
        return;
      }
      case "update-downloaded": {
        if (this.state.status !== "downloading") return; // duplicate or unsolicited
        if (event.version !== this.state.version) {
          this.fail("version_mismatch");
          return;
        }
        this.stallTimer?.cancel();
        this.stallTimer = null;
        // SEC-09: the engine verified the file against ITS copy of the manifest and the publisher
        // signature. Bind it to the SIGNED manifest too: nothing becomes "downloaded" until the
        // file's own hash equals the hash the signed manifest names.
        if (this.verifying === null) {
          this.verifying = this.verifyDownloaded(event.version, event.downloadedFile).finally(() => {
            this.verifying = null;
          });
        }
        return;
      }
      case "error": {
        if (this.state.status === "checking" || this.state.status === "downloading") this.fail(event.code);
        return;
      }
    }
  }

  private async verifyDownloaded(version: string, file: string | undefined): Promise<void> {
    const expected = this.signedSha512;
    let ok = false;
    try {
      ok = expected !== null && file !== undefined && file !== "" && (await this.deps.hashFile(file)) === expected;
    } catch {
      ok = false;
    }
    if (this.disposed || this.state.status !== "downloading") return;
    if (!ok) {
      // The file is NOT deleted here: this app's file-system use is limited to a short allow-list
      // (capture-guards.test.ts). A rejected installer can never be installed: only a verified
      // "downloaded" state allows it, and the next verified download replaces it.
      this.downloadedVersion = null;
      this.fail("verification_failed");
      return;
    }
    this.downloadedVersion = version;
    this.set({ status: "downloaded", version });
  }

  // ---- download ------------------------------------------------------------------------------

  async download(): Promise<UpdateState> {
    const current = this.state;
    if (current.status !== "available" || this.inFlight) return this.state; // only from "available"
    const { version, sizeBytes } = current;
    this.inFlight = this.runDownload(version, sizeBytes).finally(() => {
      this.inFlight = null;
    });
    await this.inFlight;
    return this.state;
  }

  private async runDownload(version: string, sizeBytes: number): Promise<void> {
    let free: number;
    try {
      free = await this.deps.freeDiskBytes();
    } catch {
      free = Number.POSITIVE_INFINITY; // cannot tell: let the engine fail on a real shortage
    }
    if (free < requiredFreeBytes(sizeBytes)) return this.fail("insufficient_disk_space");
    this.set({ status: "downloading", version, percent: 0 });
    // If the connection stalls the watchdog cancels the engine AND rejects this race, so an engine
    // that ignores cancellation can never leave the manager waiting forever.
    const stalled = new Promise<never>((_resolve, reject) => {
      this.rejectStall = reject;
    });
    stalled.catch(() => undefined);
    this.armStallTimer();
    try {
      await Promise.race([this.deps.engine.downloadUpdate(), stalled]);
    } catch (error) {
      if (this.state.status === "downloading") {
        const code = classifyEngineError(error);
        this.fail(code === "unknown" ? "download_failed" : code);
      }
      return;
    }
    // The downloaded event starts an asynchronous verification; wait for it to finish.
    if (this.verifying) await this.verifying;
    // A completed call must have produced the downloaded event; anything else is not trusted.
    if (this.state.status === "downloading") this.fail("download_failed");
  }

  private armStallTimer(): void {
    this.stallTimer?.cancel();
    this.stallTimer = this.deps.setTimer(() => {
      if (this.state.status !== "downloading") return;
      this.deps.engine.cancelDownload();
      this.fail("timeout");
      this.rejectStall?.(new Error("download stalled"));
    }, UPDATE_TIMINGS.downloadStallMs);
  }

  // ---- install -------------------------------------------------------------------------------

  /** Quit and install the downloaded update. Only from "downloaded" (or after a failed restart). */
  async installAndRestart(): Promise<UpdateState> {
    const state = this.state;
    const retry = state.status === "error" && state.code === "install_failed" && this.downloadedVersion !== null;
    if (state.status !== "downloaded" && !retry) return this.state;
    const version = state.status === "downloaded" ? state.version : (this.downloadedVersion as string);
    if (this.inFlight) return this.state;
    try {
      await this.deps.prepareForInstall(); // stops audio, held screenshots and answers; never starts any
    } catch {
      this.fail("install_failed");
      return this.state;
    }
    this.set({ status: "installing", version });
    this.installTimer?.cancel();
    this.installTimer = this.deps.setTimer(() => {
      // quitAndInstall returned but the app is still running: the restart failed.
      if (this.state.status === "installing") this.fail("install_failed");
    }, UPDATE_TIMINGS.installWatchdogMs);
    try {
      this.deps.engine.quitAndInstall();
    } catch {
      this.installTimer.cancel();
      this.fail("install_failed");
    }
    return this.state;
  }
}
