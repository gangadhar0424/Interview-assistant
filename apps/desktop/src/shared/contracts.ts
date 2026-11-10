import type { SessionDraft, SessionOptions } from "./session-form";

export type DesktopSettings = {
  backendBaseUrl: string;
  alwaysOnTop: boolean;
  opacity: number;
  expanded: boolean;
  /** Opt-in (default false). Turning it on needs a native confirmation from the main process. */
  screenshotInputEnabled: boolean;
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
};

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

export type ExtractionResult = {
  /** Single-use token proving the text came from a completed screenshot flow. */
  extractionId: string;
  text: string;
  truncated: boolean;
};

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
  };
  auth: {
    getState(): Promise<DesktopResult<AuthState>>;
    login(email: string, password: string): Promise<DesktopResult<DesktopUser>>;
    signup(email: string, password: string): Promise<DesktopResult<DesktopUser>>;
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
    ): Promise<DesktopResult<void>>;
    cancelQuestion(requestId: string): Promise<DesktopResult<void>>;
    /** Ready/not-ready resumes and documents plus the answer-style options (Phase 11.5). */
    createOptions(): Promise<DesktopResult<CreateSessionOptions>>;
    /** Validates in main, then calls the existing `POST /sessions`. */
    create(draft: SessionDraft): Promise<DesktopResult<SessionSummary>>;
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
  onCaptureEvent(listener: (event: CaptureEvent) => void): () => void;
  onStreamMessage(listener: (message: StreamMessage) => void): () => void;
  onSessionExpired(listener: () => void): () => void;
};

declare global {
  interface Window {
    desktop: DesktopBridge;
  }
}
