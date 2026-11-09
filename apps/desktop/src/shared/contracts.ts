export type DesktopSettings = {
  backendBaseUrl: string;
  alwaysOnTop: boolean;
  opacity: number;
  expanded: boolean;
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
    ): Promise<DesktopResult<void>>;
    cancelQuestion(requestId: string): Promise<DesktopResult<void>>;
  };
  onStreamMessage(listener: (message: StreamMessage) => void): () => void;
  onSessionExpired(listener: () => void): () => void;
};

declare global {
  interface Window {
    desktop: DesktopBridge;
  }
}
