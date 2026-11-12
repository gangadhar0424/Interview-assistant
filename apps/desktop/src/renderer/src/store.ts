import { create } from "zustand";
import {
  defaultDraft,
  hasErrors,
  validateDraft,
  type DraftErrors,
  type SessionDraft,
} from "../../shared/session-form";
import { startAudioCapture, type ActiveCapture } from "./audio/capture";
import type {
  AnswerMetrics,
  AudioEvent,
  AudioSourceId,
  BackendStatus,
  CreateSessionOptions,
  CaptureDisplay,
  CaptureEvent,
  DesktopError,
  DesktopResult,
  DesktopSettings,
  DesktopUser,
  HandoffState,
  QaEvent,
  QuestionItem,
  SessionSummary,
  StreamMessage,
} from "../../shared/contracts";
import { IDLE_HANDOFF } from "../../shared/contracts";

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
export type AudioLine = {
  id: string;
  text: string;
  truncated: boolean;
  isQuestion: boolean;
  confidence: number;
  asked: boolean;
};

/** Live audio UI state. Transcript lines exist in memory only and vanish on stop/sign-out. */
export type AudioUi = {
  state: "idle" | "arming" | "listening";
  source: AudioSourceId | null;
  lines: AudioLine[];
  notice: string | null;
};

const MAX_UI_LINES = 200;
const IDLE_AUDIO: AudioUi = { state: "idle", source: null, lines: [], notice: null };
let activeCapture: ActiveCapture | null = null;

function endLocalCapture(): void {
  activeCapture?.stop();
  activeCapture = null;
}

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
  errorKind: ErrorKind | null;
  createOptions: CreateSessionOptions | null;
  createDraft: SessionDraft;
  createStatus: "idle" | "loading" | "ready" | "submitting";
  createError: string | null;
  createFieldErrors: DraftErrors;
  capture: CaptureUi;
  captureNotice: string | null;
  audio: AudioUi;
  /** Web -> desktop handoff (D-55): state pushed by main; it never contains the code. */
  handoff: HandoffState;
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
  startAudio(source: AudioSourceId): Promise<void>;
  stopAudio(): Promise<void>;
  askFromAudio(lineId: string): Promise<void>;
  dismissAudioLine(lineId: string): void;
  clearAudioLines(): void;
  handleAudioEvent(event: AudioEvent): void;
  cancel(): Promise<void>;
  handleStreamMessage(message: StreamMessage): void;
  expireSession(): void;
  handleHandoffEvent(state: HandoffState): Promise<void>;
  dismissHandoff(): Promise<void>;
  loadCreateOptions(): Promise<void>;
  updateDraft(patch: Partial<SessionDraft>): void;
  resetCreate(): void;
  submitCreate(): Promise<string | null>;
  updateSettings(patch: Partial<DesktopSettings>): Promise<boolean>;
  reportError(message: string): void;
  clearError(): void;
};

/** An error from the main process that keeps its kind and code (so "network" is not lost). */
export class StoreError extends Error {
  constructor(
    message: string,
    readonly kind?: DesktopError["kind"],
    readonly code?: string,
    readonly fieldErrors?: DesktopError["fieldErrors"],
  ) {
    super(message);
    this.name = "StoreError";
  }
}

function unwrap<T>(result: DesktopResult<T>): T {
  if (!result.ok) {
    throw new StoreError(result.error.message, result.error.kind, result.error.code, result.error.fieldErrors);
  }
  return result.data;
}

type ErrorKind = "network" | "other";

/** The banner text plus where it came from, so a later success can clear only a network error. */
function errorState(error: unknown): { error: string; errorKind: ErrorKind } {
  return {
    error: safeMessage(error),
    errorKind: error instanceof StoreError && error.kind === "network" ? "network" : "other",
  };
}

const isNetworkError = (error: unknown): boolean =>
  error instanceof StoreError && error.kind === "network";

/** Backend field names (as in the validation hints) to form fields. */
const BACKEND_FIELDS: Record<string, keyof SessionDraft> = {
  company_name: "company",
  title: "title",
  job_description: "description",
  resume_id: "resumeId",
  document_ids: "documentIds",
  "preferences.language": "language",
  "preferences.question_type": "questionType",
  "preferences.answer_format": "format",
  "preferences.answer_length": "length",
  "preferences.answer_tone": "tone",
  "preferences.extra_instructions": "extra",
};

function createErrorMessage(error: unknown): string {
  if (!(error instanceof StoreError)) return safeMessage(error);
  const messages: Record<string, string> = {
    material_not_ready: "A selected resume or document is still being processed. Wait until it is ready, then try again.",
    material_not_found: "A selected resume or document is no longer available. Refresh the list and choose again.",
    duplicate_material: "Each document can only be chosen once.",
    too_many_documents: "You chose more documents than are allowed.",
    validation_error: "Some details were not accepted. Check the highlighted fields.",
    invalid_provider_selection: "The answer provider is not available. Try again.",
    rate_limited: "Too many requests. Please wait a moment and try again.",
  };
  return (error.code && messages[error.code]) || error.message;
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

/**
 * Makes the handed-off session the selected one once main reports it READY. Selecting a session
 * already stops any capture (existing behaviour); nothing here ever starts audio or screenshots.
 */
async function selectHandedOffSession(get: () => AppState): Promise<void> {
  const { handoff, user } = get();
  if (handoff.phase !== "ready" || !handoff.session || !user) return;
  const target = handoff.session.id;
  await get().refreshSessions();
  const state = get();
  if (state.selectedSessionId !== target && state.sessions.some((s) => s.id === target)) {
    await state.selectSession(target);
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
  errorKind: null,
  createOptions: null,
  createDraft: defaultDraft(null),
  createStatus: "idle",
  createError: null,
  createFieldErrors: {},
  capture: { phase: "idle" },
  captureNotice: null,
  audio: IDLE_AUDIO,
  handoff: IDLE_HANDOFF,
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
      // A handoff link can arrive (cold start) before this window was listening.
      const handoff = await window.desktop.handoff.getState();
      if (handoff.ok) await get().handleHandoffEvent(handoff.data);
      set({ initialized: true });
    } catch (error) {
      set({ ...errorState(error), initialized: true });
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
      await selectHandedOffSession(get);
    } catch (error) {
      set(errorState(error));
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
      await selectHandedOffSession(get);
    } catch (error) {
      set(errorState(error));
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
      set(errorState(error));
    } finally {
      void window.desktop.capture.cancel().catch(() => undefined);
      endLocalCapture();
      void window.desktop.audio.stop().catch(() => undefined);
      set({
        audio: IDLE_AUDIO,
        busy: false,
        user: null,
        sessions: [],
        selectedSessionId: null,
        history: [],
        liveAnswer: null,
        capture: { phase: "idle" },
        captureNotice: null,
        ...clearedCreate(),
      });
    }
  },
  checkBackend: async () => {
    set({ connection: "checking" });
    try {
      const result = await window.desktop.app.checkBackend();
      if (result.ok) markReachable(set);
      else set({ connection: "unavailable" });
    } catch (error) {
      set({ connection: "unavailable", ...errorState(error) });
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
      // The backend answered: show it as connected now and drop a stale network error banner,
      // without waiting for the periodic health check.
      markReachable(set);
      if (selectedSessionId) await get().loadHistory();
    } catch (error) {
      set(errorState(error));
      if (isNetworkError(error)) set({ connection: "unavailable" });
    }
  },
  selectSession: async (sessionId) => {
    // A screenshot belongs to the session it was captured for: changing session discards it.
    if (get().capture.phase !== "idle") await get().cancelCapture();
    if (get().audio.state !== "idle") await get().stopAudio();
    set({ audio: IDLE_AUDIO, selectedSessionId: sessionId, history: [], liveAnswer: null });
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
      markReachable(set);
    } catch (error) {
      set(errorState(error));
      if (isNetworkError(error)) set({ connection: "unavailable" });
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
      set(errorState(error));
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
      ...clearedCreate(),
      error: "Your session expired. Sign in again to continue.",
      errorKind: "other",
    });
  },
  handleHandoffEvent: async (handoff) => {
    set({ handoff });
    // Selecting the prepared session is all READY does. It starts no audio and no capture.
    await selectHandedOffSession(get);
  },
  dismissHandoff: async () => {
    set({ handoff: IDLE_HANDOFF });
    try {
      await window.desktop.handoff.dismiss();
    } catch {
      /* the local state is already cleared */
    }
  },
  updateSettings: async (patch) => {
    try {
      const settings = unwrap<DesktopSettings>(await window.desktop.app.updateSettings(patch));
      set({ settings });
      return true;
    } catch (error) {
      set(errorState(error));
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
  startAudio: async (source) => {
    const sessionId = get().selectedSessionId;
    if (!sessionId) {
      set((s) => ({ audio: { ...s.audio, notice: "Select a session before listening." } }));
      return;
    }
    set((s) => ({ audio: { ...s.audio, notice: null } }));
    try {
      // Main validates, shows the indicator and only then grants the media permission.
      unwrap<void>(await window.desktop.audio.start(source, sessionId));
      activeCapture = await startAudioCapture(source, (chunk) => window.desktop.audio.sendChunk(chunk));
    } catch (error) {
      endLocalCapture();
      void window.desktop.audio.stop().catch(() => undefined);
      const denied = error instanceof DOMException;
      set((s) => ({
        audio: {
          ...s.audio,
          state: "idle",
          source: null,
          notice: denied ? "Audio capture was not allowed or no device was found." : safeMessage(error),
        },
      }));
    }
  },
  stopAudio: async () => {
    endLocalCapture();
    try {
      await window.desktop.audio.stop();
    } catch {
      /* main also stops on window/sign-out; the state event is authoritative */
    }
  },
  askFromAudio: async (lineId) => {
    const line = get().audio.lines.find((l) => l.id === lineId);
    if (!line || line.asked) return;
    set((s) => ({
      audio: { ...s.audio, lines: s.audio.lines.map((l) => (l.id === lineId ? { ...l, asked: true } : l)) },
    }));
    // Only on this explicit press does the existing question flow begin (input_source = audio).
    await get().ask(line.text, line.id);
  },
  dismissAudioLine: (lineId) =>
    set((s) => ({ audio: { ...s.audio, lines: s.audio.lines.filter((l) => l.id !== lineId) } })),
  clearAudioLines: () => set((s) => ({ audio: { ...s.audio, lines: [] } })),
  handleAudioEvent: (event) => {
    switch (event.type) {
      case "state":
        if (event.state === "idle") endLocalCapture();
        set((s) => ({ audio: { ...s.audio, state: event.state, source: event.source } }));
        return;
      case "utterance":
        set((s) => ({
          audio: {
            ...s.audio,
            lines: [
              ...s.audio.lines,
              {
                id: event.id,
                text: event.text,
                truncated: event.truncated,
                isQuestion: event.isQuestion,
                confidence: event.confidence,
                asked: false,
              },
            ].slice(-MAX_UI_LINES),
          },
        }));
        return;
      case "notice":
        set((s) => ({ audio: { ...s.audio, notice: event.message } }));
        return;
      case "stopped":
        endLocalCapture();
        set((s) => ({ audio: { ...s.audio, state: "idle", source: null, notice: event.message } }));
        return;
    }
  },
  loadCreateOptions: async () => {
    set({ createStatus: "loading", createError: null });
    try {
      const loaded = unwrap<CreateSessionOptions>(await window.desktop.sessions.createOptions());
      const ready = new Set(loaded.resumes.filter((r) => r.status === "ready").map((r) => r.id));
      const readyDocs = new Set(loaded.documents.filter((d) => d.status === "ready").map((d) => d.id));
      const current = get().createDraft;
      const defaults = defaultDraft(loaded.options);
      const offered = (value: string, list: string[]) => (list.includes(value) ? value : undefined);
      set({
        createOptions: loaded,
        createStatus: "ready",
        createDraft: {
          ...current,
          resumeId: current.resumeId && ready.has(current.resumeId) ? current.resumeId : null,
          documentIds: current.documentIds.filter((id) => readyDocs.has(id)),
          language: offered(current.language, loaded.options.languages) ?? defaults.language,
          questionType: offered(current.questionType, loaded.options.question_types) ?? defaults.questionType,
          format: offered(current.format, loaded.options.formats) ?? defaults.format,
          length: offered(current.length, loaded.options.lengths) ?? defaults.length,
          tone: offered(current.tone, loaded.options.tones) ?? defaults.tone,
        },
      });
      markReachable(set);
    } catch (error) {
      set({ createStatus: "idle", createError: createErrorMessage(error) });
      if (isNetworkError(error)) set({ connection: "unavailable" });
    }
  },
  updateDraft: (patch) =>
    set((state) => {
      const next = { ...state.createDraft, ...patch };
      // Editing a field clears that field's message (and the general one).
      const cleared = { ...state.createFieldErrors };
      for (const key of Object.keys(patch)) delete cleared[key as keyof SessionDraft];
      return { createDraft: next, createFieldErrors: cleared, createError: null };
    }),
  resetCreate: () => set(clearedCreate(get().createOptions)),
  submitCreate: async () => {
    const { createDraft: draft, createOptions: loaded } = get();
    if (!loaded) return null;
    const ready = new Set(loaded.resumes.filter((r) => r.status === "ready").map((r) => r.id));
    const readyDocs = new Set(loaded.documents.filter((d) => d.status === "ready").map((d) => d.id));
    const issues = validateDraft(draft, loaded.options, ready, readyDocs);
    if (hasErrors(issues)) {
      set({ createFieldErrors: issues, createError: "Fix the highlighted fields and try again." });
      return null;
    }
    set({ createStatus: "submitting", createError: null, createFieldErrors: {} });
    try {
      const created = unwrap<SessionSummary>(await window.desktop.sessions.create(draft));
      markReachable(set);
      await get().refreshSessions();
      await get().selectSession(created.id);
      set(clearedCreate(get().createOptions));
      return created.id;
    } catch (error) {
      const fieldErrors: DraftErrors = {};
      if (error instanceof StoreError) {
        for (const hint of error.fieldErrors ?? []) {
          const key = BACKEND_FIELDS[hint.field];
          if (key) fieldErrors[key] = hint.message;
        }
      }
      set({ createStatus: "ready", createError: createErrorMessage(error), createFieldErrors: fieldErrors });
      if (isNetworkError(error)) set({ connection: "unavailable" });
      return null;
    }
  },
  reportError: (message) => set({ error: message, errorKind: "other" }),
  clearError: () => set({ error: null, errorKind: null }),
}));

function safeMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}

type StoreSet = (partial: Partial<AppState> | ((state: AppState) => Partial<AppState>)) => void;

/** The backend answered: mark it connected now and drop a stale network-error banner. */
function markReachable(set: StoreSet): void {
  set((state) => ({
    connection: "connected",
    ...(state.errorKind === "network" ? { error: null, errorKind: null } : {}),
  }));
}

/** A fresh, empty create-session state (the draft lives in memory only and is never persisted). */
function clearedCreate(options: CreateSessionOptions | null = null) {
  return {
    createDraft: defaultDraft(options?.options ?? null),
    createStatus: options ? ("ready" as const) : ("idle" as const),
    createError: null,
    createFieldErrors: {} as DraftErrors,
    ...(options ? {} : { createOptions: null }),
  };
}
