import {
  CAPTURE_PRIVACY_ACK_REQUIRED_MESSAGE,
  CAPTURE_PRIVACY_MESSAGES,
  CAPTURE_PRIVACY_CONSENT_VERSION,
  isCapturePrivacySupported,
  type CapturePrivacyConsent,
  type CapturePrivacyState,
  type CapturePrivacyStatus,
} from "../shared/capture-privacy";

/**
 * Window capture privacy (M-15, merges M-14). The only module in the app that may call
 * `setContentProtection` (a guard test enforces that). Design rules, all tested:
 *
 * - Off by default. It becomes on ONLY through `enable()`, which needs the signed-in user to have
 *   accepted the acceptable-use notice AND a native confirmation the renderer cannot fake. Nothing
 *   else turns it on: not startup, window recreation, crash recovery, a display change or an update.
 * - A saved "on" is the user's own earlier choice. It is re-applied to the window (never turned on
 *   by itself) so the setting holds across restart, window recreation and renderer recovery.
 * - It touches only this app's own top-level window. It never touches the taskbar entry, the tray,
 *   the process or any other window, and it does not detect or evade anything.
 * - It reports only what is known. Electron's call returns nothing, so the best honest status is
 *   "requested" (the call completed) or "failed" (it threw). Never "protected".
 * - Any failure leaves the window shown normally and the app fully working.
 */

export type ProtectableWindow = {
  isDestroyed(): boolean;
  setContentProtection(enabled: boolean): void;
  /**
   * Electron's own record of the flag (`BrowserWindow.isContentProtected`). It reads back what was
   * set; it is not proof that a given capture tool honours it. Optional so a port can omit it.
   */
  isContentProtected?(): boolean;
};

export type CapturePrivacyStore = {
  capturePrivacyEnabled(): boolean;
  setCapturePrivacy(enabled: boolean, consent?: CapturePrivacyConsent): void;
  capturePrivacyConsent(): CapturePrivacyConsent | null;
};

export type CapturePrivacyDeps = {
  /** The current main window, or null before one exists. Called each time so recreation is handled. */
  getWindow: () => ProtectableWindow | null;
  store: CapturePrivacyStore;
  platform: string;
  /** `os.release()`. */
  osRelease: string;
  /** Native confirmation dialog. */
  confirm: () => Promise<boolean>;
  /** Clock for the consent timestamp (ISO string); defaults to the system clock. */
  now?: () => string;
  /** Content-free diagnostics event (code only). */
  record?: (event: string) => void;
};

/** Set the flag, then read it back when the window can report it; a mismatch counts as a failure. */
function applyVerified(window: ProtectableWindow, enabled: boolean): void {
  window.setContentProtection(enabled);
  if (window.isContentProtected && window.isContentProtected() !== enabled) {
    throw new Error("The window did not report the requested capture-protection state.");
  }
}

export class CapturePrivacyManager {
  private status: CapturePrivacyStatus = "off";
  private messageOverride: string | null = null;
  private confirming = false;
  private readonly supported: boolean;

  constructor(private readonly deps: CapturePrivacyDeps) {
    this.supported = isCapturePrivacySupported(deps.platform, deps.osRelease);
    if (!this.supported && this.deps.store.capturePrivacyEnabled()) this.status = "unsupported";
  }

  state(): CapturePrivacyState {
    const enabled = this.deps.store.capturePrivacyEnabled();
    const status: CapturePrivacyStatus = !this.supported && this.status === "off" ? "unsupported" : this.status;
    return {
      enabled,
      status,
      message: this.messageOverride ?? CAPTURE_PRIVACY_MESSAGES[status],
      supported: this.supported,
      consent: enabled ? this.deps.store.capturePrivacyConsent() : null,
    };
  }

  /**
   * Turn it on after an explicit user action. `acknowledged` is whether the signed-in user accepted
   * the current acceptable-use notice (the caller reads it from the settings store). Returns the resulting state (unsupported, declined
   * and failed leave it off). Throws a user-safe Error when the notice has not been accepted or the
   * setting cannot be saved.
   */
  async enable(acknowledged: boolean): Promise<CapturePrivacyState> {
    if (this.deps.store.capturePrivacyEnabled() && this.status === "requested") return this.state();
    if (!this.supported) {
      this.setStatus("unsupported");
      return this.state();
    }
    if (!acknowledged) throw new Error(CAPTURE_PRIVACY_ACK_REQUIRED_MESSAGE);
    if (this.confirming) throw new Error("The confirmation is already open.");
    this.confirming = true;
    try {
      if (!(await this.deps.confirm())) return this.state(); // declined: nothing changed
    } finally {
      this.confirming = false;
    }
    const window = this.liveWindow();
    if (!window) {
      this.setStatus("failed");
      return this.state();
    }
    try {
      applyVerified(window, true);
    } catch {
      this.deps.record?.("capture_privacy_failed");
      this.safeRemove(window);
      this.setStatus("failed");
      return this.state();
    }
    try {
      this.deps.store.setCapturePrivacy(true, {
        version: CAPTURE_PRIVACY_CONSENT_VERSION,
        acceptedAt: (this.deps.now ?? (() => new Date().toISOString()))(),
      });
    } catch (error) {
      this.safeRemove(window); // never leave it on without a saved choice
      this.setStatus("off");
      throw error;
    }
    this.setStatus("requested");
    this.deps.record?.("capture_privacy_on");
    return this.state();
  }

  /** Turn it off. Always saves "off" first, then asks Windows to stop; a failure there is reported. */
  disable(): CapturePrivacyState {
    this.deps.store.setCapturePrivacy(false);
    const window = this.liveWindow();
    if (!window) {
      this.setStatus("off");
      return this.state();
    }
    try {
      applyVerified(window, false);
      this.setStatus("off");
    } catch {
      this.deps.record?.("capture_privacy_remove_failed");
      this.status = "failed";
      this.messageOverride =
        "Windows did not confirm that the window can be captured again. Restart the app to be sure. The setting is saved as off.";
    }
    this.deps.record?.("capture_privacy_off");
    return this.state();
  }

  /**
   * Re-apply the user's SAVED choice to the current window. Called after window creation, load,
   * show, layout and display changes, and renderer recovery. It can only repeat an earlier explicit
   * "on"; with the saved setting off it does nothing (it never turns the feature on).
   */
  reapply(): CapturePrivacyState {
    if (!this.deps.store.capturePrivacyEnabled()) return this.state();
    if (!this.supported) {
      this.setStatus("unsupported");
      return this.state();
    }
    const window = this.liveWindow();
    if (!window) return this.state();
    try {
      applyVerified(window, true);
      this.setStatus("requested");
    } catch {
      this.deps.record?.("capture_privacy_failed");
      this.setStatus("failed");
    }
    return this.state();
  }

  private liveWindow(): ProtectableWindow | null {
    const window = this.deps.getWindow();
    return window && !window.isDestroyed() ? window : null;
  }

  private safeRemove(window: ProtectableWindow): void {
    try {
      window.setContentProtection(false);
    } catch {
      // Best effort: nothing more can be done, and the status already tells the user.
    }
  }

  private setStatus(status: CapturePrivacyStatus): void {
    this.status = status;
    this.messageOverride = null;
  }
}
