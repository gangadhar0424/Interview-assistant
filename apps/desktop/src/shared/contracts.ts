import type { AcknowledgementState } from "./acknowledgement";
import type { AnswerSource } from "./answer-source";
import type { CapturePrivacyState } from "./capture-privacy";
import type { DisplayList } from "./placement";
import type { CopyDiagnosticsResult, RecoveryInfo } from "./recovery";
import type { ShortcutConfig, ShortcutEvent, ShortcutState } from "./shortcuts";
import type { SessionDraft, SessionOptions } from "./session-form";
import type { UpdateState } from "./updates";

export type DesktopSettings = {
  backendBaseUrl: string;
  alwaysOnTop: boolean;
  opacity: number;
  expanded: boolean;
  /** Opt-in (default false). Turning it on needs a native confirmation from the main process. */
  screenshotInputEnabled: boolean;
  /** Opt-in (default false). Turning it on needs a native confirmation from the main process. */
  audioInputEnabled: boolean;
};

export type DesktopUser = {
  id: string;
  email: string;
  created_at: string;
};

export type DesktopError = {
  kind: "network" | "http" | "invalid_input" | "storage" | "unknown";
  message: string;
  status?: number;
  code?: string;
  retryAfterSeconds?: number;
  /** Backend validation hints (field name + the backend's own fixed message); never user input. */
  fieldErrors?: Array<{ field: string; message: string }>;
};

export type DesktopResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: DesktopError };

export type BackendStatus = "checking" | "connected" | "unavailable";

export type SessionSummary = {
  id: string;
  type: string;
  title: string | null;
  company_name: string;
  status: string;
  created_at: string;
  updated_at: string;
  resume: { id: string | null; filename: string };
  document_count: number;
  /** M-9, derived by the server. Optional so an older server still works. */
  last_activity_at?: string;
  question_count?: number;
  activity_state?: "active" | "completed" | "interrupted";
};

/** User-triggered session export (M-9). The content is saved by the main process; never returned. */
export type SessionExportFormat = "markdown" | "pdf";
export type SessionExportResult = { saved: boolean; fileName: string | null };

/** A resume or document the user may select (Phase 11.5). Names and status only: never content. */
export type FileOption = {
  id: string;
  filename: string;
  size_bytes: number;
  created_at: string;
  status: "ready" | "processing" | "failed";
};

export type CreateSessionOptions = {
  resumes: FileOption[];
  documents: FileOption[];
  options: SessionOptions;
};

export type SessionList = {
  items: SessionSummary[];
  next_cursor: string | null;
};

export type AnswerItem = {
  id: string;
  question_id: string;
  status: string;
  text: string;
  provider: string | null;
  model: string | null;
  retrieval_ms: number | null;
  first_token_ms: number | null;
  total_ms: number | null;
  fallback_used: boolean;
  error_code: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  created_at: string;
  source?: AnswerSource | null;
};

export type QuestionItem = {
  id: string;
  text: string;
  source: string;
  kind: string;
  type: string | null;
  confidence: number | null;
  status: string;
  created_at: string;
  answers: AnswerItem[];
};

export type QuestionPage = { items: QuestionItem[]; next_cursor: string | null };

export type AnswerMetrics = {
  retrieval_ms: number | null;
  first_token_ms: number | null;
  total_ms: number | null;
};

export type QaEvent =
  | { type: "question.created"; questionId: string; answerId: string }
  | { type: "retrieval.done"; questionId: string; chunkCount: number; ms: number }
  | {
      type: "answer.started";
      answerId: string;
      questionId: string;
      provider: string;
      model: string;
    }
  | { type: "answer.delta"; answerId: string; index: number; text: string }
  | {
      type: "answer.done";
      answerId: string;
      questionId: string;
      provider: string;
      model: string;
      metrics: AnswerMetrics;
      fallbackUsed: boolean;
      inputTokens: number | null;
      outputTokens: number | null;
      source?: AnswerSource | null;
    }
  | {
      type: "answer.error";
      answerId: string | null;
      questionId: string | null;
      code: string;
      retryable: boolean;
    };

export type StreamMessage =
  | { requestId: string; event: QaEvent }
  | { requestId: string; error: DesktopError }
  | { requestId: string; cancelled: true };

export type AuthState = { user: DesktopUser | null };

// ---- Screenshot input (Phase 11B, D-51). Capture lives only in the main process. ----

/** A display the user may choose. Names only: no thumbnails are taken to build the picker. */
export type CaptureDisplay = {
  id: string;
  label: string;
  primary: boolean;
  width: number;
  height: number;
};

export type CaptureCancelReason =
  | "user"
  | "window"
  | "disabled"
  | "signed_out"
  | "session"
  | "expired"
  | "navigation"
  | "error";

/** Pushed by the main process. The full image never leaves main; `previewDataUrl` is a small thumbnail. */
export type CaptureEvent =
  | { type: "countdown"; seconds: number; displayLabel: string }
  | {
      type: "captured";
      captureId: string;
      previewDataUrl: string;
      width: number;
      height: number;
      bytes: number;
    }
  | { type: "cancelled"; reason: CaptureCancelReason; message: string };

// ---- Live audio input (Phase 12, D-53). Capture runs in the renderer; control lives in main. ----

export type AudioSourceId = "microphone" | "system";

export type AudioStopReason =
  | "user"
  | "window"
  | "disabled"
  | "signed_out"
  | "session"
  | "navigation"
  | "timeout"
  | "limit"
  | "unavailable"
  | "voice_provider_required"
  | "error";

/** Pushed by the main process. Audio itself never travels to the renderer, only text. */
export type AudioEvent =
  | { type: "state"; state: "arming" | "listening" | "idle"; source: AudioSourceId | null }
  | {
      type: "utterance";
      id: string;
      text: string;
      truncated: boolean;
      isQuestion: boolean;
      confidence: number;
    }
  | { type: "notice"; message: string }
  | { type: "stopped"; reason: AudioStopReason; message: string };

export type ExtractionResult = {
  /** Single-use token proving the text came from a completed screenshot flow. */
  extractionId: string;
  text: string;
  truncated: boolean;
};

// ---- Web -> desktop handoff (D-55). The renderer only ever sees this state: never the code. ----

export type HandoffFailure =
  | "invalid" // malformed, unknown, expired, reused, another account's: deliberately one answer
  | "expired" // the code was not redeemed within its short lifetime on this device
  | "unavailable" // the server could not be reached or is temporarily unavailable
  | "rate_limited"
  | "declined"; // the user chose not to switch away from a running capture or account

export type HandoffSession = { id: string; title: string | null; company_name: string };

export type HandoffState = {
  /**
   * `ready` only means the prepared session is selected and the assistant is waiting. It never
   * means audio or screenshot capture has started: those stay explicit, separately-consented steps.
   */
  phase: "idle" | "redeeming" | "ready" | "failed";
  session: HandoffSession | null;
  failure: HandoffFailure | null;
};

export const IDLE_HANDOFF: HandoffState = { phase: "idle", session: null, failure: null };

export type DesktopBridge = {
  app: {
    getSettings(): Promise<DesktopResult<DesktopSettings>>;
    updateSettings(
      patch: Partial<DesktopSettings>,
    ): Promise<DesktopResult<DesktopSettings>>;
    checkBackend(): Promise<DesktopResult<{ status: "connected" }>>;
    setExpanded(expanded: boolean): Promise<DesktopResult<DesktopSettings>>;
    minimize(): Promise<DesktopResult<void>>;
    closeToTray(): Promise<DesktopResult<void>>;
    copyText(text: string): Promise<DesktopResult<void>>;
    /** Whether the signed-in user accepted the current acceptable-use notice (M-1, D-68). */
    getAcknowledgement(): Promise<DesktopResult<AcknowledgementState>>;
    /**
     * Asks the main process to record acceptance of `version`. Main shows its own native
     * confirmation first; the renderer cannot record acceptance by any other route.
     */
    acceptAcknowledgement(version: number): Promise<DesktopResult<AcknowledgementState>>;
    /** Saved keyboard-shortcut preferences and whether each one is registered (M-4, D-69). */
    getShortcuts(): Promise<DesktopResult<ShortcutState>>;
    /** Validates in main, saves, re-registers, and returns the new state. */
    setShortcuts(config: ShortcutConfig): Promise<DesktopResult<ShortcutState>>;
    /** Whether the window was just reloaded after its renderer stopped (read once; M-5, D-70). */
    getRecovery(): Promise<DesktopResult<RecoveryInfo>>;
    /**
     * Copies a bounded, redacted diagnostics report to the clipboard (written by the main process;
     * the text never reaches the renderer). Returns only how many characters were copied.
     */
    copyDiagnostics(): Promise<DesktopResult<CopyDiagnosticsResult>>;
    /** Screen-capture privacy state (M-15). Off by default; see shared/capture-privacy.ts. */
    getCapturePrivacy(): Promise<DesktopResult<CapturePrivacyState>>;
    /**
     * Ask for it on or off. Turning on is refused until the acceptable-use notice is accepted, and
     * needs a native confirmation in the main process. Returns the resulting state.
     */
    setCapturePrivacy(enabled: boolean): Promise<DesktopResult<CapturePrivacyState>>;
    /** The connected displays and which one the assistant is on (M-6, D-71). */
    getDisplays(): Promise<DesktopResult<DisplayList>>;
    /** Moves the assistant to the middle of that display and remembers it. Validated in main. */
    moveToDisplay(displayId: number): Promise<DesktopResult<DisplayList>>;
  };
  auth: {
    getState(): Promise<DesktopResult<AuthState>>;
    logout(): Promise<DesktopResult<void>>;
  };
  sessions: {
    list(): Promise<DesktopResult<SessionSummary[]>>;
    questions(sessionId: string): Promise<DesktopResult<QuestionItem[]>>;
    startQuestion(
      requestId: string,
      sessionId: string,
      text: string,
      extractionId?: string,
      /** D-66: "summary" asks for a short spoken answer; omitted means full detail. */
      detail?: "summary",
    ): Promise<DesktopResult<void>>;
    cancelQuestion(requestId: string): Promise<DesktopResult<void>>;
    /** Ready/not-ready resumes and documents plus the answer-style options (Phase 11.5). */
    createOptions(): Promise<DesktopResult<CreateSessionOptions>>;
    /** Validates in main, then calls the existing `POST /sessions`. */
    create(draft: SessionDraft): Promise<DesktopResult<SessionSummary>>;
    /** Saves the voice-listening provider the user picked for a session that had none (D-57). */
    setVoice(sessionId: string, provider: string): Promise<DesktopResult<void>>;
    /** Saves a different answer (reasoning) provider for a session whose provider failed (D-63). */
    setReasoning(sessionId: string, provider: string): Promise<DesktopResult<void>>;
    /** M-9: renames a session (title only). Returns the updated summary. */
    rename(sessionId: string, title: string): Promise<DesktopResult<SessionSummary>>;
    /** M-9: completes a session. Stops no capture by itself; the app stops audio first. */
    end(sessionId: string): Promise<DesktopResult<SessionSummary>>;
    /** M-9: reopens a completed session. Starts no audio or capture. */
    reopen(sessionId: string): Promise<DesktopResult<SessionSummary>>;
    /** M-9: deletes a session after a native confirmation shown by the main process. */
    remove(sessionId: string): Promise<DesktopResult<{ deleted: boolean }>>;
    /**
     * M-9: exports one session to a file the user chooses. Main shows what is exported and where
     * it is written, asks for a path, and writes the file; the content never reaches the renderer.
     */
    exportToFile(
      sessionId: string,
      format: SessionExportFormat,
    ): Promise<DesktopResult<SessionExportResult>>;
  };
  updates: {
    /** The current update state (always "disabled" with a reason in an unsigned or development build). */
    getState(): Promise<DesktopResult<UpdateState>>;
    /** A manual metadata check. Downloads nothing. */
    check(): Promise<DesktopResult<UpdateState>>;
    /** Downloads the announced update. Only valid from "available". */
    download(): Promise<DesktopResult<UpdateState>>;
    /** Stops capture, quits and installs. Only valid from "downloaded". Never starts audio or capture. */
    install(): Promise<DesktopResult<UpdateState>>;
  };
  handoff: {
    /** The current handoff state (the link can arrive before the renderer is listening). */
    getState(): Promise<DesktopResult<HandoffState>>;
    /** Clears a ready/failed handoff notice. */
    dismiss(): Promise<DesktopResult<void>>;
  };
  capture: {
    listDisplays(): Promise<DesktopResult<CaptureDisplay[]>>;
    /** Starts the cancellable countdown; the image is captured only when it completes. */
    start(displayId: string, sessionId: string): Promise<DesktopResult<void>>;
    /** Cancels a countdown or discards a held capture. */
    cancel(): Promise<DesktopResult<void>>;
    /** Sends the held image (after the user saw the preview) and returns editable text. */
    extract(captureId: string): Promise<DesktopResult<ExtractionResult>>;
  };
  audio: {
    /** Arms capture (permission is granted only now); the renderer then starts the stream. */
    start(source: AudioSourceId, sessionId: string): Promise<DesktopResult<void>>;
    stop(): Promise<DesktopResult<void>>;
    /** One-way PCM16 16 kHz mono frame; ignored by main unless listening was started. */
    sendChunk(chunk: ArrayBuffer): void;
  };
  onAudioEvent(listener: (event: AudioEvent) => void): () => void;
  /** A shortcut asked to move to a panel. It never starts audio or capture. */
  onShortcutEvent(listener: (event: ShortcutEvent) => void): () => void;
  onCaptureEvent(listener: (event: CaptureEvent) => void): () => void;
  onStreamMessage(listener: (message: StreamMessage) => void): () => void;
  onSessionExpired(listener: () => void): () => void;
  onHandoffEvent(listener: (state: HandoffState) => void): () => void;
  onUpdateEvent(listener: (state: UpdateState) => void): () => void;
};

declare global {
  interface Window {
    desktop: DesktopBridge;
  }
}
