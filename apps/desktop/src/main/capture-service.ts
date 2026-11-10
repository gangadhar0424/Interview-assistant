/**
 * Screenshot capture orchestration (Phase 11B, D-51). Pure logic: every side effect (the screen,
 * the window, the tray, the network, the clock) comes in through `CapturePorts`, so the whole flow
 * is testable without Electron. Runs only in the main process.
 *
 * Guarantees enforced here, not in the renderer:
 *  - capture happens only after an explicit `start` and a cancellable countdown;
 *  - `start` is refused unless the feature is enabled, a user is signed in, the assistant window is
 *    visible, focused and not minimised, and the chosen session exists and is ready;
 *  - the image is memory only: one held `Buffer`, never written anywhere, zero-filled and released
 *    on extraction (success or failure), cancellation, expiry, sign-out, window hide/close,
 *    reload, disabling the feature and quit;
 *  - the image is sent only after the user has seen the preview and called `extract` explicitly;
 *  - the extracted text carries a single-use token, so a question is marked `screenshot` only if it
 *    follows a completed extraction in the same session.
 *
 * Nothing sensitive is logged: this module has no logging at all.
 */
import { randomUUID } from "node:crypto";
import type {
  CaptureCancelReason,
  CaptureDisplay,
  CaptureEvent,
  ExtractionResult,
} from "../shared/contracts";

export const COUNTDOWN_SECONDS = 3;
export const MIN_CAPTURE_INTERVAL_MS = 5_000;
export const MAX_CAPTURES_PER_SESSION = 10;
export const HELD_IMAGE_TTL_MS = 60_000;
export const EXTRACTION_TTL_MS = 10 * 60_000;
export const MAX_PNG_BYTES = 5 * 1024 * 1024;

export type WindowState = { visible: boolean; focused: boolean; minimized: boolean };

export type IndicatorState = "idle" | "countdown" | "capturing" | "held";

export type ExtractedText = { text: string; truncated: boolean };

/** The Electron-bound parts of `CapturePorts` (screen, preview, tray/taskbar); injected by index.ts. */
export type CaptureHost = Pick<
  CapturePorts,
  "listDisplays" | "captureDisplay" | "makePreview" | "setIndicator"
>;

export type CapturePorts = {
  isEnabled(): boolean;
  isSignedIn(): boolean;
  windowState(): WindowState;
  listDisplays(): CaptureDisplay[];
  /** Resolves the PNG of one display (already bounded in size), or null if it could not be read. */
  captureDisplay(displayId: string): Promise<Buffer | null>;
  /** A small preview (data URL) of a PNG; the full image never goes to the renderer. */
  makePreview(png: Buffer): { dataUrl: string; width: number; height: number };
  /** True if the session exists for this user and is ready (checked again by the backend). */
  sessionReady(sessionId: string): Promise<boolean>;
  upload(png: Buffer, sessionId: string, signal: AbortSignal): Promise<ExtractedText>;
  emit(event: CaptureEvent): void;
  /** Visible, main-process-owned indication (taskbar flash, tray tooltip). */
  setIndicator(state: IndicatorState): void;
  now(): number;
};

export class CaptureRefusal extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "CaptureRefusal";
  }
}

type Held = {
  captureId: string;
  sessionId: string;
  png: Buffer;
  expiry: ReturnType<typeof setTimeout>;
  extracting: AbortController | null;
};

type Countdown = {
  displayId: string;
  displayLabel: string;
  sessionId: string;
  remaining: number;
  timer: ReturnType<typeof setTimeout> | null;
};

type Extraction = { sessionId: string; expiresAt: number };

/** Zero-fill then drop: the only copy of the pixels is overwritten before release. */
function wipe(buffer: Buffer): void {
  buffer.fill(0);
}

export class CaptureService {
  private countdown: Countdown | null = null;
  private capturing = false;
  // Bumped by every discard: a capture already in flight then drops its image when it returns.
  private generation = 0;
  private held: Held | null = null;
  private lastStartAt: number | null = null;
  private readonly perSession = new Map<string, number>();
  private readonly extractions = new Map<string, Extraction>();

  constructor(private readonly ports: CapturePorts) {}

  // ------------------------------------------------------------------ queries

  listDisplays(): CaptureDisplay[] {
    this.requireEnabledAndSignedIn();
    return this.ports.listDisplays();
  }

  /** True while a countdown, a capture or a held image exists (used only by tests/diagnostics). */
  busy(): boolean {
    return this.countdown !== null || this.capturing || this.held !== null;
  }

  hasHeldImage(): boolean {
    return this.held !== null;
  }

  // -------------------------------------------------------------------- start

  /** Begin the visible countdown. Refuses unless every precondition holds. */
  async start(displayId: string, sessionId: string): Promise<void> {
    this.requireEnabledAndSignedIn();
    if (this.countdown || this.capturing || this.held) {
      throw new CaptureRefusal(
        "capture_in_progress",
        "Finish or cancel the current screenshot first.",
      );
    }
    this.requireWindowReady("start");
    const display = this.ports.listDisplays().find((item) => item.id === displayId);
    if (!display) throw new CaptureRefusal("display_not_found", "That display is no longer available.");
    if (!(await this.ports.sessionReady(sessionId))) {
      throw new CaptureRefusal(
        "session_not_ready",
        "Select a ready session before capturing a screenshot.",
      );
    }
    // Conditions can change while we waited for the network: check everything again.
    this.requireEnabledAndSignedIn();
    this.requireWindowReady("start");
    if (this.countdown || this.capturing || this.held) {
      throw new CaptureRefusal(
        "capture_in_progress",
        "Finish or cancel the current screenshot first.",
      );
    }
    const now = this.ports.now();
    if (this.lastStartAt !== null && now - this.lastStartAt < MIN_CAPTURE_INTERVAL_MS) {
      throw new CaptureRefusal("too_soon", "Please wait a few seconds before capturing again.");
    }
    if ((this.perSession.get(sessionId) ?? 0) >= MAX_CAPTURES_PER_SESSION) {
      throw new CaptureRefusal(
        "capture_limit",
        "The screenshot limit for this session was reached. Start a new session to continue.",
      );
    }
    this.lastStartAt = now;
    this.perSession.set(sessionId, (this.perSession.get(sessionId) ?? 0) + 1);
    this.countdown = {
      displayId,
      displayLabel: display.label,
      sessionId,
      remaining: COUNTDOWN_SECONDS,
      timer: null,
    };
    this.ports.setIndicator("countdown");
    this.tick();
  }

  private tick(): void {
    const countdown = this.countdown;
    if (!countdown) return;
    this.ports.emit({
      type: "countdown",
      seconds: countdown.remaining,
      displayLabel: countdown.displayLabel,
    });
    countdown.timer = setTimeout(() => {
      if (this.countdown !== countdown) return;
      countdown.remaining -= 1;
      if (countdown.remaining > 0) {
        this.tick();
      } else {
        countdown.timer = null;
        void this.captureNow(countdown);
      }
    }, 1000);
  }

  private async captureNow(countdown: Countdown): Promise<void> {
    if (this.countdown !== countdown) return;
    // The user may have switched to the window they want captured, so focus is not required now,
    // but the assistant must still be visible (never captured while hidden in the tray).
    const problem = this.checkAtCaptureTime();
    if (problem) {
      this.countdown = null;
      this.ports.setIndicator("idle");
      this.ports.emit({ type: "cancelled", reason: problem.reason, message: problem.message });
      return;
    }
    this.countdown = null;
    this.capturing = true;
    const generation = this.generation;
    this.ports.setIndicator("capturing");
    let png: Buffer | null = null;
    try {
      png = await this.ports.captureDisplay(countdown.displayId);
    } catch {
      png = null;
    }
    if (generation !== this.generation) {
      // Cancelled (or the session ended) while the screen was being read: drop the image silently.
      if (png) wipe(png);
      return;
    }
    this.capturing = false;
    if (!png || png.length === 0 || png.length > MAX_PNG_BYTES) {
      if (png) wipe(png);
      this.ports.setIndicator("idle");
      this.ports.emit({
        type: "cancelled",
        reason: "error",
        message: "The screen could not be captured. Please try again.",
      });
      return;
    }
    // Capture finished while the user was already signed out, disabled or hidden the window: drop it.
    const after = this.checkAtCaptureTime();
    if (after) {
      wipe(png);
      this.ports.setIndicator("idle");
      this.ports.emit({ type: "cancelled", reason: after.reason, message: after.message });
      return;
    }
    let preview: ReturnType<CapturePorts["makePreview"]>;
    try {
      preview = this.ports.makePreview(png);
    } catch {
      wipe(png);
      this.ports.setIndicator("idle");
      this.ports.emit({
        type: "cancelled",
        reason: "error",
        message: "The screenshot preview could not be created. Please try again.",
      });
      return;
    }
    const captureId = randomUUID();
    const expiry = setTimeout(() => this.discard("expired"), HELD_IMAGE_TTL_MS);
    this.held = {
      captureId,
      sessionId: countdown.sessionId,
      png,
      expiry,
      extracting: null,
    };
    this.ports.setIndicator("held");
    this.ports.emit({
      type: "captured",
      captureId,
      previewDataUrl: preview.dataUrl,
      width: preview.width,
      height: preview.height,
      bytes: png.length,
    });
  }

  private checkAtCaptureTime(): { reason: CaptureCancelReason; message: string } | null {
    if (!this.ports.isEnabled()) {
      return { reason: "disabled", message: "Screenshot input was turned off." };
    }
    if (!this.ports.isSignedIn()) {
      return { reason: "signed_out", message: "You were signed out, so the screenshot was cancelled." };
    }
    const state = this.ports.windowState();
    if (!state.visible || state.minimized) {
      return {
        reason: "window",
        message: "The assistant window was hidden, so the screenshot was cancelled.",
      };
    }
    return null;
  }

  // ------------------------------------------------------------------ extract

  /**
   * Send the held image for text extraction. Only valid after the `captured` event (the user saw
   * the preview). The image is wiped and released whatever the outcome.
   */
  async extract(captureId: string): Promise<ExtractionResult> {
    const held = this.held;
    if (!held || held.captureId !== captureId || held.extracting) {
      throw new CaptureRefusal("capture_not_found", "That screenshot is no longer available.");
    }
    this.requireEnabledAndSignedIn();
    const controller = new AbortController();
    held.extracting = controller;
    clearTimeout(held.expiry);
    try {
      const result = await this.ports.upload(held.png, held.sessionId, controller.signal);
      if (controller.signal.aborted) {
        throw new CaptureRefusal("cancelled", "The screenshot was cancelled.");
      }
      const extractionId = randomUUID();
      this.extractions.set(extractionId, {
        sessionId: held.sessionId,
        expiresAt: this.ports.now() + EXTRACTION_TTL_MS,
      });
      return { extractionId, text: result.text, truncated: result.truncated };
    } finally {
      if (this.held === held) this.release(held);
      else wipe(held.png);
      this.ports.setIndicator("idle");
    }
  }

  // ------------------------------------------------------- cancel and cleanup

  /** User cancel: stops a countdown or discards a held image (also aborts an upload in flight). */
  cancel(): void {
    this.discard("user");
  }

  /** Cancel/discard everything for `reason`. Safe to call at any time, any number of times. */
  discard(reason: CaptureCancelReason): void {
    const message = CANCEL_MESSAGES[reason];
    const had = this.countdown !== null || this.held !== null || this.capturing;
    this.generation += 1;
    this.capturing = false;
    if (this.countdown) {
      if (this.countdown.timer) clearTimeout(this.countdown.timer);
      this.countdown = null;
    }
    const held = this.held;
    if (held) {
      held.extracting?.abort();
      this.release(held);
    }
    if (had) {
      this.ports.setIndicator("idle");
      this.ports.emit({ type: "cancelled", reason, message });
    }
    if (reason === "signed_out" || reason === "session") this.extractions.clear();
  }

  /** Everything is dropped without a renderer notification (the renderer may be gone). */
  dispose(): void {
    this.generation += 1;
    this.capturing = false;
    if (this.countdown?.timer) clearTimeout(this.countdown.timer);
    this.countdown = null;
    if (this.held) {
      this.held.extracting?.abort();
      this.release(this.held);
    }
    this.extractions.clear();
    this.ports.setIndicator("idle");
  }

  private release(held: Held): void {
    clearTimeout(held.expiry);
    wipe(held.png);
    if (this.held === held) this.held = null;
  }

  // -------------------------------------------------------- extraction tokens

  /** Single-use: valid only for the session whose screenshot was extracted. */
  consumeExtraction(extractionId: string, sessionId: string): boolean {
    const entry = this.extractions.get(extractionId);
    if (!entry || entry.sessionId !== sessionId || entry.expiresAt < this.ports.now()) {
      this.extractions.delete(extractionId);
      return false;
    }
    this.extractions.delete(extractionId);
    return true;
  }

  /** Re-arm a token whose question never started (for example a refused or failed request). */
  restoreExtraction(extractionId: string, sessionId: string): void {
    this.extractions.set(extractionId, {
      sessionId,
      expiresAt: this.ports.now() + EXTRACTION_TTL_MS,
    });
  }

  // ----------------------------------------------------------------- guards

  private requireEnabledAndSignedIn(): void {
    if (!this.ports.isEnabled()) {
      throw new CaptureRefusal(
        "screenshot_disabled",
        "Screenshot input is turned off. Enable it in Settings first.",
      );
    }
    if (!this.ports.isSignedIn()) {
      throw new CaptureRefusal("not_signed_in", "Sign in to use screenshot input.");
    }
  }

  private requireWindowReady(_stage: "start"): void {
    const state = this.ports.windowState();
    if (!state.visible || state.minimized || !state.focused) {
      throw new CaptureRefusal(
        "window_not_ready",
        "The assistant window must be visible and focused to capture a screenshot.",
      );
    }
  }
}

const CANCEL_MESSAGES: Record<CaptureCancelReason, string> = {
  user: "Screenshot cancelled.",
  window: "The assistant window was hidden, so the screenshot was cancelled.",
  disabled: "Screenshot input was turned off.",
  signed_out: "You were signed out, so the screenshot was cancelled.",
  session: "The session changed, so the screenshot was discarded.",
  expired: "The screenshot was discarded because it was not sent in time.",
  navigation: "The screenshot was discarded.",
  error: "The screenshot was discarded.",
};
