/**
 * Live audio orchestration (Phase 12, D-53). Pure logic: every side effect (window, tray, network,
 * clock) comes in through `AudioPorts`, so the whole flow is testable without Electron or hardware.
 * Runs only in the main process.
 *
 * Guarantees enforced here, not in the renderer:
 *  - nothing is captured until `start`; `start` is refused unless the feature is enabled, a user is
 *    signed in, the assistant window is visible, focused and not minimised, and the session is ready;
 *  - audio exists only as in-memory buffers: the segmenter's working frames and the utterance being
 *    uploaded; everything is zero-filled after upload and on every stop path;
 *  - listening stops (and drops what it holds) when the window is hidden or minimised, on sign-out,
 *    session change, disabling the feature, reload, quit, and after a maximum duration;
 *  - the renderer cannot push audio unless listening was started here (chunks are refused in `idle`);
 *  - a transcript carries a single-use token, so a question is marked `audio` only if it follows an
 *    utterance heard in the same session.
 *
 * Nothing in this module logs: audio and transcripts must never reach a log.
 */
import { randomUUID } from "node:crypto";
import type { AudioEvent, AudioStopReason } from "../shared/contracts";
import { UtteranceSegmenter } from "./audio-vad";
import type { AudioGateState, AudioSource } from "./media-permissions";
import type { WindowState } from "./capture-service";
import { buildWav } from "./wav";

export const ARMING_TIMEOUT_MS = 10_000;
export const MAX_LISTEN_MS = 2 * 60 * 60 * 1000;
export const MAX_CHUNK_BYTES = 32 * 1024;
export const MAX_BYTES_PER_SECOND = 128 * 1024; // 4x real time: a flood is dropped, not queued
export const MAX_IN_FLIGHT = 2;
export const MAX_QUEUED = 3;
export const UTTERANCE_TTL_MS = 10 * 60_000;
export const MAX_LINES_PER_SESSION = 500;

export type TranscribeResult = {
  text: string;
  truncated: boolean;
  isQuestion: boolean;
  confidence: number;
};

export type AudioIndicator = "idle" | "arming" | "listening";

export type AudioPorts = {
  isEnabled(): boolean;
  isSignedIn(): boolean;
  windowState(): WindowState;
  sessionReady(sessionId: string): Promise<boolean>;
  transcribe(wav: Buffer, sessionId: string, signal: AbortSignal): Promise<TranscribeResult>;
  /** A safe, user-facing message for a failed transcription (never content). */
  describeError(error: unknown): string;
  /** True when the failure means audio input cannot work at all (stop listening). */
  isFatal(error: unknown): boolean;
  emit(event: AudioEvent): void;
  /** Visible, main-process-owned indication (tray tooltip, taskbar flash). */
  setIndicator(state: AudioIndicator, source: AudioSource | null): void;
  now(): number;
};

export class AudioRefusal extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "AudioRefusal";
  }
}

type Utterance = { sessionId: string; expiresAt: number };

const STOP_MESSAGES: Record<AudioStopReason, string> = {
  user: "Listening stopped.",
  window: "The assistant window was hidden, so listening stopped.",
  disabled: "Audio input was turned off.",
  signed_out: "You were signed out, so listening stopped.",
  session: "The session changed, so listening stopped.",
  navigation: "Listening stopped.",
  timeout: "No audio arrived, so listening stopped.",
  limit: "The maximum listening time was reached, so listening stopped.",
  unavailable: "Audio input is not available on this server.",
  error: "Listening stopped.",
};

function wipe(buffer: Uint8Array): void {
  buffer.fill(0);
}

export class AudioService {
  private current: "idle" | "arming" | "listening" = "idle";
  private activeSource: AudioSource | null = null;
  private sessionId: string | null = null;
  private segmenter = new UtteranceSegmenter();
  private armingTimer: ReturnType<typeof setTimeout> | null = null;
  private limitTimer: ReturnType<typeof setTimeout> | null = null;
  private inFlight = new Set<AbortController>();
  private queue: Buffer[] = [];
  private windowStart = 0;
  private windowBytes = 0;
  private floodNoticed = false;
  private lines = 0;
  private readonly utterances = new Map<string, Utterance>();

  constructor(private readonly ports: AudioPorts) {}

  // ------------------------------------------------------------------ queries

  get state(): "idle" | "arming" | "listening" {
    return this.current;
  }

  get source(): AudioSource | null {
    return this.activeSource;
  }

  /** What the media-permission handlers read (default is idle: everything denied). */
  gate(): AudioGateState {
    return { state: this.current, source: this.activeSource };
  }

  // -------------------------------------------------------------------- start

  async start(source: AudioSource, sessionId: string): Promise<void> {
    this.requireEnabledAndSignedIn();
    if (this.current !== "idle") {
      throw new AudioRefusal("already_listening", "Stop listening before starting again.");
    }
    this.requireWindowReady();
    if (!(await this.ports.sessionReady(sessionId))) {
      throw new AudioRefusal("session_not_ready", "Select a ready session before listening.");
    }
    // Conditions can change while we waited for the network: check everything again.
    this.requireEnabledAndSignedIn();
    this.requireWindowReady();
    if (this.current !== "idle") {
      throw new AudioRefusal("already_listening", "Stop listening before starting again.");
    }
    this.current = "arming";
    this.activeSource = source;
    this.sessionId = sessionId;
    this.lines = 0;
    this.windowStart = this.ports.now();
    this.windowBytes = 0;
    this.floodNoticed = false;
    // These two timers can only END a capture; nothing here ever starts one.
    this.armingTimer = setTimeout(() => this.stop("timeout"), ARMING_TIMEOUT_MS);
    this.limitTimer = setTimeout(() => this.stop("limit"), MAX_LISTEN_MS);
    this.ports.setIndicator("arming", source);
    this.ports.emit({ type: "state", state: "arming", source });
  }

  // ------------------------------------------------------------------- chunks

  /** Raw PCM16 little-endian audio from the renderer. Refused unless a capture was started. */
  pushChunk(data: unknown): void {
    if (this.current === "idle") return;
    if (!(data instanceof Uint8Array) || data.byteLength === 0 || data.byteLength % 2 !== 0) return;
    if (data.byteLength > MAX_CHUNK_BYTES) return;
    const now = this.ports.now();
    if (now - this.windowStart >= 1000) {
      this.windowStart = now;
      this.windowBytes = 0;
    }
    this.windowBytes += data.byteLength;
    if (this.windowBytes > MAX_BYTES_PER_SECOND) {
      if (!this.floodNoticed) {
        this.floodNoticed = true;
        this.ports.emit({ type: "notice", message: "Audio is arriving too fast; some was dropped." });
      }
      return;
    }
    if (this.current === "arming") {
      this.current = "listening";
      if (this.armingTimer) clearTimeout(this.armingTimer);
      this.armingTimer = null;
      this.ports.setIndicator("listening", this.activeSource);
      this.ports.emit({ type: "state", state: "listening", source: this.activeSource });
    }
    const samples = new Int16Array(data.byteLength / 2);
    for (let i = 0; i < samples.length; i += 1) {
      samples[i] = (data[i * 2] as number) | (((data[i * 2 + 1] as number) << 24) >> 16);
    }
    for (const pcm of this.segmenter.push(samples)) this.enqueue(pcm);
    samples.fill(0);
  }

  // -------------------------------------------------------------- transcription

  private enqueue(pcm: Buffer): void {
    if (this.inFlight.size >= MAX_IN_FLIGHT) {
      if (this.queue.length >= MAX_QUEUED) {
        wipe(pcm);
        this.ports.emit({ type: "notice", message: "Transcription is falling behind; a phrase was skipped." });
        return;
      }
      this.queue.push(pcm);
      return;
    }
    void this.transcribeSegment(pcm);
  }

  private async transcribeSegment(pcm: Buffer): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) {
      wipe(pcm);
      return;
    }
    const controller = new AbortController();
    this.inFlight.add(controller);
    const wav = buildWav(pcm);
    wipe(pcm);
    try {
      const result = await this.ports.transcribe(wav, sessionId, controller.signal);
      if (controller.signal.aborted || !result.text) return;
      if (this.lines >= MAX_LINES_PER_SESSION) return;
      this.lines += 1;
      const id = randomUUID();
      this.utterances.set(id, { sessionId, expiresAt: this.ports.now() + UTTERANCE_TTL_MS });
      this.ports.emit({
        type: "utterance",
        id,
        text: result.text,
        truncated: result.truncated,
        isQuestion: result.isQuestion,
        confidence: result.confidence,
      });
    } catch (error) {
      if (controller.signal.aborted) return;
      if (this.ports.isFatal(error)) {
        this.stop("unavailable");
        return;
      }
      this.ports.emit({ type: "notice", message: this.ports.describeError(error) });
    } finally {
      wipe(wav);
      this.inFlight.delete(controller);
      const next = this.queue.shift();
      if (next) void this.transcribeSegment(next);
    }
  }

  // ------------------------------------------------------------ stop and cleanup

  /**
   * Stop listening. An explicit user Stop still transcribes the phrase in progress; every other
   * reason drops what is held and abandons uploads in flight.
   */
  stop(reason: AudioStopReason): void {
    if (this.current === "idle") return;
    if (this.armingTimer) clearTimeout(this.armingTimer);
    if (this.limitTimer) clearTimeout(this.limitTimer);
    this.armingTimer = null;
    this.limitTimer = null;
    const finalUtterance = reason === "user" ? this.segmenter.flush() : null;
    if (reason !== "user") {
      this.segmenter.reset();
      for (const controller of this.inFlight) controller.abort();
      for (const pending of this.queue) wipe(pending);
      this.queue = [];
    }
    const wasListening = this.current === "listening";
    this.current = "idle";
    const source = this.activeSource;
    this.activeSource = null;
    if (finalUtterance && wasListening) this.enqueue(finalUtterance);
    else if (finalUtterance) wipe(finalUtterance);
    if (reason === "signed_out" || reason === "session") this.utterances.clear();
    this.ports.setIndicator("idle", null);
    this.ports.emit({ type: "state", state: "idle", source });
    this.ports.emit({ type: "stopped", reason, message: STOP_MESSAGES[reason] });
  }

  /** Everything is dropped without notifying the renderer (it may be gone). */
  dispose(): void {
    if (this.armingTimer) clearTimeout(this.armingTimer);
    if (this.limitTimer) clearTimeout(this.limitTimer);
    this.armingTimer = null;
    this.limitTimer = null;
    this.segmenter.reset();
    for (const controller of this.inFlight) controller.abort();
    for (const pending of this.queue) wipe(pending);
    this.queue = [];
    this.current = "idle";
    this.activeSource = null;
    this.sessionId = null;
    this.utterances.clear();
    this.ports.setIndicator("idle", null);
  }

  // -------------------------------------------------------- utterance tokens

  /** Single-use: valid only for the session in which the utterance was heard. */
  consumeUtterance(id: string, sessionId: string): boolean {
    const entry = this.utterances.get(id);
    if (!entry || entry.sessionId !== sessionId || entry.expiresAt < this.ports.now()) {
      this.utterances.delete(id);
      return false;
    }
    this.utterances.delete(id);
    return true;
  }

  /** Re-arm a token whose question never started (a refused or failed request). */
  restoreUtterance(id: string, sessionId: string): void {
    this.utterances.set(id, { sessionId, expiresAt: this.ports.now() + UTTERANCE_TTL_MS });
  }

  // ----------------------------------------------------------------- guards

  private requireEnabledAndSignedIn(): void {
    if (!this.ports.isEnabled()) {
      throw new AudioRefusal("audio_disabled", "Audio input is turned off. Enable it in Settings first.");
    }
    if (!this.ports.isSignedIn()) throw new AudioRefusal("not_signed_in", "Sign in to use audio input.");
  }

  private requireWindowReady(): void {
    const state = this.ports.windowState();
    if (!state.visible || state.minimized || !state.focused) {
      throw new AudioRefusal(
        "window_not_ready",
        "The assistant window must be visible and focused to start listening.",
      );
    }
  }
}
