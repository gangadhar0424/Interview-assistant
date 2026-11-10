import { create } from "zustand";
import type {
  AnswerMetrics,
  BackendStatus,
  CaptureDisplay,
  CaptureEvent,
  DesktopError,
  DesktopResult,
  DesktopSettings,
  DesktopUser,
  QaEvent,
  QuestionItem,
  SessionSummary,
  StreamMessage,
} from "../../shared/contracts";

type LiveAnswer = {
  requestId: string;
  question: string;
  phase: "sending" | "retrieving" | "generating" | "streaming" | "done" | "failed" | "cancelled";
  text: string;
  questionId: string | null;
  answerId: string | null;
  provider: string | null;
  model: string | null;
  metrics: AnswerMetrics | null;
  deltaCount: number;
  errorMessage: string | null;
};

/** Screenshot flow state (Phase 11B). The full image never reaches the renderer; only a thumbnail. */
export type CaptureUi =
  | { phase: "idle" }
  | { phase: "choosing"; displays: CaptureDisplay[] }
  | { phase: "countdown"; seconds: number; displayLabel: string }
  | {
      phase: "held";
      captureId: string;
      previewDataUrl: string;
      width: number;
      height: number;
      bytes: number;
    }
  | { phase: "extracting" }
  | { phase: "review"; extractionId: string; text: string; truncated: boolean };

type AppState = {
  initialized: boolean;
  user: DesktopUser | null;
  settings: DesktopSettings | null;
  connection: BackendStatus;
  sessions: SessionSummary[];
  selectedSessionId: string | null;
  history: QuestionItem[];
  liveAnswer: LiveAnswer | null;
  capture: CaptureUi;
  captureNotice: string | null;
  busy: boolean;
  error: string | null;
  initialize(): Promise<void>;
  login(email: string, password: string): Promise<void>;
  signup(email: string, password: string): Promise<void>;
  logout(): Promise<void>;
  checkBackend(): Promise<void>;
  refreshSessions(): Promise<void>;
  selectSession(sessionId: string): Promise<void>;
  loadHistory(): Promise<void>;
  ask(question: string, extractionId?: string): Promise<void>;
  openCapture(): Promise<void>;
  startCapture(displayId: string): Promise<void>;
  cancelCapture(): Promise<void>;
  sendCapture(): Promise<void>;
  setReviewText(text: string): void;
  askFromScreenshot(): Promise<void>;
  handleCaptureEvent(event: CaptureEvent): void;
  cancel(): Promise<void>;
  handleStreamMessage(message: StreamMessage): void;
  expireSession(): void;
  updateSettings(patch: Partial<DesktopSettings>): Promise<boolean>;
  reportError(message: string): void;
  clearError(): void;
};

function unwrap<T>(result: DesktopResult<T>): T {
  if (!result.ok) throw new Error(result.error.message);
  return result.data;
}

function answerError(code: string): string {
  const messages: Record<string, string> = {
    llm_unavailable: "The answer service is temporarily unavailable.",
    llm_rate_limited: "The answer service is busy. Please try again shortly.",
    llm_timeout: "The answer took too long. Please try again.",
    llm_content_filtered: "The answer could not be generated for this question.",
    invalid_provider_selection: "This session's answer provider is no longer available.",
    retrieval_failed: "The selected session materials could not be searched.",
    empty_answer: "The answer service returned no content.",
    internal_error: "The answer could not be completed.",
  };
  return messages[code] ?? "The answer could not be completed.";
}

function updateLive(answer: LiveAnswer, event: QaEvent): LiveAnswer {
  if (answer.phase === "done" || answer.phase === "failed" || answer.phase === "cancelled") {
    return answer;
  }
  switch (event.type) {
    case "question.created":
      return {
        ...answer,
        phase: "retrieving",
        questionId: event.questionId,
        answerId: event.answerId,
      };
    case "retrieval.done":
      return { ...answer, phase: "generating" };
    case "answer.started":
      return {
        ...answer,
        phase: "generating",
        answerId: event.answerId,
        questionId: event.questionId,
        provider: event.provider,
        model: event.model,
      };
    case "answer.delta":
      if (event.answerId !== answer.answerId || event.index < answer.deltaCount) return answer;
      if (event.index !== answer.deltaCount) {
        return {
          ...answer,
          phase: "failed",
          errorMessage: "The answer stream arrived out of order. Please try again.",
        };
      }
      return {
        ...answer,
        phase: "streaming",
        text: answer.text + event.text,
        deltaCount: answer.deltaCount + 1,
      };
    case "answer.done":
      if (answer.answerId && event.answerId !== answer.answerId) return answer;
      return {
        ...answer,
        phase: "done",
        answerId: event.answerId,
        questionId: event.questionId,
        provider: event.provider,
        model: event.model,
        metrics: event.metrics,
      };
    case "answer.error":
      return {
        ...answer,
        phase: "failed",
        answerId: event.answerId ?? answer.answerId,
        questionId: event.questionId ?? answer.questionId,
        errorMessage: answerError(event.code),
      };
  }
}

export const useDesktopStore = create<AppState>((set, get) => ({
  initialized: false,
  user: null,
  settings: null,
  connection: "checking",
  sessions: [],
  selectedSessionId: null,
  history: [],
  liveAnswer: null,
  capture: { phase: "idle" },
  captureNotice: null,
  busy: false,
  error: null,
  initialize: async () => {
    set({ busy: true, error: null });
    try {
      const settings = unwrap<DesktopSettings>(await window.desktop.app.getSettings());
      set({ settings });
      await get().checkBackend();
      const auth = unwrap<{ user: DesktopUser | null }>(await window.desktop.auth.getState());
      set({ user: auth.user });
      if (auth.user) await get().refreshSessions();
      set({ initialized: true });
    } catch (error) {
      set({ error: safeMessage(error), initialized: true });
    } finally {
      set({ busy: false });
    }
  },
  login: async (email, password) => {
    set({ busy: true, error: null });
    try {
      const user = unwrap<DesktopUser>(await window.desktop.auth.login(email, password));
      set({ user });
      await get().refreshSessions();
    } catch (error) {
      set({ error: safeMessage(error) });
      throw error;
    } finally {
      set({ busy: false });
    }
  },
  signup: async (email, password) => {
    set({ busy: true, error: null });
    try {
      const user = unwrap<DesktopUser>(await window.desktop.auth.signup(email, password));
      set({ user });
      await get().refreshSessions();
    } catch (error) {
      set({ error: safeMessage(error) });
      throw error;
    } finally {
      set({ busy: false });
    }
  },
  logout: async () => {
    set({ busy: true, error: null });
    try {
      unwrap<void>(await window.desktop.auth.logout());
    } catch (error) {
      set({ error: safeMessage(error) });
    } finally {
      void window.desktop.capture.cancel().catch(() => undefined);
      set({
        busy: false,
        user: null,
        sessions: [],
        selectedSessionId: null,
        history: [],
        liveAnswer: null,
        capture: { phase: "idle" },
        captureNotice: null,
      });
    }
  },
  checkBackend: async () => {
    set({ connection: "checking" });
    try {
      const result = await window.desktop.app.checkBackend();
      set({ connection: result.ok ? "connected" : "unavailable" });
    } catch (error) {
      set({ connection: "unavailable", error: safeMessage(error) });
    }
  },
  refreshSessions: async () => {
    try {
      const sessions = unwrap<SessionSummary[]>(await window.desktop.sessions.list());
      const selected = get().selectedSessionId;
      const selectedSessionId =
        selected && sessions.some((session) => session.id === selected)
          ? selected
          : (sessions[0]?.id ?? null);
      set({ sessions, selectedSessionId });
      if (selectedSessionId) await get().loadHistory();
    } catch (error) {
      set({ error: safeMessage(error) });
    }
  },
  selectSession: async (sessionId) => {
    // A screenshot belongs to the session it was captured for: changing session discards it.
    if (get().capture.phase !== "idle") await get().cancelCapture();
    set({ selectedSessionId: sessionId, history: [], liveAnswer: null });
    await get().loadHistory();
  },
  loadHistory: async () => {
    const sessionId = get().selectedSessionId;
    if (!sessionId) {
      set({ history: [] });
      return;
    }
    try {
      const history = unwrap<QuestionItem[]>(await window.desktop.sessions.questions(sessionId));
      if (get().selectedSessionId === sessionId) set({ history });
    } catch (error) {
      set({ error: safeMessage(error) });
    }
  },
  ask: async (question, extractionId) => {
    const sessionId = get().selectedSessionId;
    if (!sessionId) {
      set({ error: "Select a session before asking a question." });
      return;
    }
    const requestId = crypto.randomUUID();
    set({
      error: null,
      liveAnswer: {
        requestId,
        question,
        phase: "sending",
        text: "",
        questionId: null,
        answerId: null,
        provider: null,
        model: null,
        metrics: null,
        deltaCount: 0,
        errorMessage: null,
      },
    });
    try {
      const result =
        extractionId === undefined
          ? await window.desktop.sessions.startQuestion(requestId, sessionId, question)
          : await window.desktop.sessions.startQuestion(
              requestId,
              sessionId,
              question,
              extractionId,
            );
      if (!result.ok) {
        set((state) => ({
          liveAnswer: state.liveAnswer
            ? {
                ...state.liveAnswer,
                phase: "failed",
                errorMessage: result.error.message,
              }
            : null,
        }));
      }
    } catch (error) {
      set((state) => ({
        liveAnswer: state.liveAnswer
          ? { ...state.liveAnswer, phase: "failed", errorMessage: safeMessage(error) }
          : null,
      }));
    }
  },
  cancel: async () => {
    const answer = get().liveAnswer;
    if (!answer || ["done", "failed", "cancelled"].includes(answer.phase)) return;
    try {
      const result = await window.desktop.sessions.cancelQuestion(answer.requestId);
      if (!result.ok) {
        set({ error: result.error.message });
        return;
      }
      set((state) => ({
        liveAnswer: state.liveAnswer
          ? { ...state.liveAnswer, phase: "cancelled" }
          : null,
      }));
      void get().loadHistory();
    } catch (error) {
      set({ error: safeMessage(error) });
    }
  },
  handleStreamMessage: (message) => {
    const current = get().liveAnswer;
    if (!current || current.requestId !== message.requestId) return;
    if ("event" in message) {
      set({ liveAnswer: updateLive(current, message.event) });
      if (message.event.type === "answer.done" || message.event.type === "answer.error") {
        void get().loadHistory();
      }
      return;
    }
    if ("cancelled" in message) {
      set({ liveAnswer: { ...current, phase: "cancelled" } });
      void get().loadHistory();
      return;
    }
    set({
      liveAnswer: {
        ...current,
        phase: "failed",
        errorMessage: message.error.message,
      },
    });
    if (message.error.status === 401) get().expireSession();
    void get().loadHistory();
  },
  expireSession: () => {
    set({
      user: null,
      sessions: [],
      selectedSessionId: null,
      history: [],
      liveAnswer: null,
      capture: { phase: "idle" },
      captureNotice: null,
      error: "Your session expired. Sign in again to continue.",
    });
  },
  updateSettings: async (patch) => {
    try {
      const settings = unwrap<DesktopSettings>(await window.desktop.app.updateSettings(patch));
      set({ settings });
      return true;
    } catch (error) {
      set({ error: safeMessage(error) });
      return false;
    }
  },
  openCapture: async () => {
    set({ captureNotice: null });
    if (!get().selectedSessionId) {
      set({ captureNotice: "Select a session before capturing a screenshot." });
      return;
    }
    try {
      const displays = unwrap<CaptureDisplay[]>(await window.desktop.capture.listDisplays());
      if (displays.length === 0) {
        set({ captureNotice: "No display is available to capture." });
        return;
      }
      set({ capture: { phase: "choosing", displays } });
    } catch (error) {
      set({ captureNotice: safeMessage(error) });
    }
  },
  startCapture: async (displayId) => {
    const sessionId = get().selectedSessionId;
    if (!sessionId) {
      set({ captureNotice: "Select a session before capturing a screenshot." });
      return;
    }
    set({ captureNotice: null });
    try {
      unwrap<void>(await window.desktop.capture.start(displayId, sessionId));
    } catch (error) {
      set({ capture: { phase: "idle" }, captureNotice: safeMessage(error) });
    }
  },
  cancelCapture: async () => {
    set({ capture: { phase: "idle" }, captureNotice: null });
    try {
      await window.desktop.capture.cancel();
    } catch {
      // Nothing is held in the renderer; the main process discards on its own triggers too.
    }
  },
  sendCapture: async () => {
    const current = get().capture;
    if (current.phase !== "held") return;
    const captureId = current.captureId;
    // The thumbnail is dropped immediately: from here on the image is only in the main process.
    set({ capture: { phase: "extracting" }, captureNotice: null });
    try {
      const result = unwrap<{ extractionId: string; text: string; truncated: boolean }>(
        await window.desktop.capture.extract(captureId),
      );
      if (get().capture.phase !== "extracting") return; // cancelled meanwhile
      set({
        capture: {
          phase: "review",
          extractionId: result.extractionId,
          text: result.text,
          truncated: result.truncated,
        },
      });
    } catch (error) {
      if (get().capture.phase === "extracting") {
        set({ capture: { phase: "idle" }, captureNotice: safeMessage(error) });
      }
    }
  },
  setReviewText: (text) => {
    const current = get().capture;
    if (current.phase === "review") set({ capture: { ...current, text } });
  },
  askFromScreenshot: async () => {
    const current = get().capture;
    if (current.phase !== "review") return;
    const text = current.text.trim();
    if (!text) {
      set({ captureNotice: "There is no text to ask. Edit it or discard the screenshot." });
      return;
    }
    // Only now, on an explicit press of Ask, does the existing question flow begin.
    set({ capture: { phase: "idle" }, captureNotice: null });
    await get().ask(text, current.extractionId);
  },
  handleCaptureEvent: (event) => {
    switch (event.type) {
      case "countdown":
        set({
          capture: { phase: "countdown", seconds: event.seconds, displayLabel: event.displayLabel },
          captureNotice: null,
        });
        return;
      case "captured":
        if (get().capture.phase !== "countdown") return;
        set({
          capture: {
            phase: "held",
            captureId: event.captureId,
            previewDataUrl: event.previewDataUrl,
            width: event.width,
            height: event.height,
            bytes: event.bytes,
          },
        });
        return;
      case "cancelled":
        set({
          capture: { phase: "idle" },
          captureNotice: event.reason === "user" ? null : event.message,
        });
        return;
    }
  },
  reportError: (message) => set({ error: message }),
  clearError: () => set({ error: null }),
}));

function safeMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}
