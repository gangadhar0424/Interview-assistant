import { create } from "zustand";
import {
  defaultDraft,
  hasErrors,
  validateDraft,
  type DraftErrors,
  type ProviderChoice,
  type SessionDraft,
} from "../../shared/session-form";
import { pickResumeSession } from "../../shared/session-view";
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
  SessionExportFormat,
  SessionExportResult,
  SessionSummary,
  StreamMessage,
} from "../../shared/contracts";
import type { AnswerSource } from "../../shared/answer-source";
import { IDLE_HANDOFF } from "../../shared/contracts";
import type { UpdateState } from "../../shared/updates";
import { ACCEPTABLE_USE_VERSION, type AcknowledgementState } from "../../shared/acknowledgement";
import type { CapturePrivacyState } from "../../shared/capture-privacy";
import type { DisplayChoice, DisplayList } from "../../shared/placement";
import type { CopyDiagnosticsResult, RecoveryInfo } from "../../shared/recovery";
import type { ShortcutConfig, ShortcutPanel, ShortcutState } from "../../shared/shortcuts";

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
  source?: AnswerSource | null;
  deltaCount: number;
  errorMessage: string | null;
  /** The server's error code for a failed answer (drives the "choose another provider" offer). */
  errorCode: string | null;
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
  /**
   * Set when the selected session has no voice-listening provider (created before D-57): the
   * providers the server offers, shown as a picker. Null otherwise.
   */
  voiceChoices: ProviderChoice[] | null;
  voiceSaving: boolean;
};

const MAX_UI_LINES = 200;
const IDLE_AUDIO: AudioUi = {
  state: "idle",
  source: null,
  lines: [],
  notice: null,
  voiceChoices: null,
  voiceSaving: false,
};
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
  /** Acceptable-use acknowledgement (M-1). `null` until main has answered; main enforces it. */
  acknowledgement: AcknowledgementState | null;
  acknowledgementBusy: boolean;
  acknowledgementNotice: string | null;
  /** Keyboard shortcuts (M-4): saved preferences and whether each is registered. */
  shortcuts: ShortcutState | null;
  shortcutsBusy: boolean;
  shortcutsNotice: string | null;
  /** A shortcut asked to move to a panel. Moving there never starts anything. */
  panelFocus: { panel: ShortcutPanel; nonce: number } | null;
  /** The window was reloaded after its renderer stopped (M-5). Shown once, then dismissed. */
  recoveryNotice: boolean;
  /** Connected displays and which one the assistant is on (M-6). */
  displays: DisplayChoice[] | null;
  displaysBusy: boolean;
  displaysNotice: string | null;
  /** Screen-capture privacy (M-15). Null until loaded. */
  capturePrivacy: CapturePrivacyState | null;
  capturePrivacyBusy: boolean;
  capturePrivacyNotice: string | null;
  diagnosticsBusy: boolean;
  diagnosticsMessage: string | null;
  connection: BackendStatus;
  sessions: SessionSummary[];
  selectedSessionId: string | null;
  /** The session a management action (rename, complete, delete, export) is running for. */
  sessionBusyId: string | null;
  /** Outcome of the last management action, shown on the Sessions page. */
  sessionNotice: { text: string; tone: "ok" | "error" } | null;
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
  /** Offer to switch a session's answer provider after a provider failure (D-63). */
  answerProvider: { choices: ProviderChoice[] | null; saving: boolean; notice: string | null };
  /** Web -> desktop handoff (D-55): state pushed by main; it never contains the code. */
  handoff: HandoffState;
  /** Secure updates (M-11): state pushed by main. Downloading and installing are always user actions. */
  update: UpdateState;
  busy: boolean;
  error: string | null;
  initialize(): Promise<void>;
  loadAcknowledgement(): Promise<void>;
  loadDisplays(): Promise<void>;
  loadCapturePrivacy(): Promise<void>;
  setCapturePrivacy(enabled: boolean): Promise<void>;
  moveToDisplay(displayId: number): Promise<void>;
  loadRecovery(): Promise<void>;
  dismissRecoveryNotice(): void;
  /** Asks the main process to copy a redacted diagnostics report; the text never comes here. */
  copyDiagnostics(): Promise<void>;
  loadShortcuts(): Promise<void>;
  saveShortcuts(config: ShortcutConfig): Promise<boolean>;
  requestPanelFocus(panel: ShortcutPanel): void;
  /** Opens the main process's native confirmation; acceptance is recorded only there. */
  acceptAcknowledgement(): Promise<void>;
  logout(): Promise<void>;
  checkBackend(): Promise<void>;
  refreshSessions(): Promise<void>;
  selectSession(sessionId: string): Promise<void>;
  /** M-9 session management. Each returns after the server answered; none starts any capture. */
  renameSession(sessionId: string, title: string): Promise<boolean>;
  endSession(sessionId: string): Promise<void>;
  reopenSession(sessionId: string): Promise<void>;
  deleteSession(sessionId: string): Promise<void>;
  exportSession(sessionId: string, format: SessionExportFormat): Promise<void>;
  clearSessionNotice(): void;
  loadHistory(): Promise<void>;
  ask(question: string, extractionId?: string): Promise<void>;
  /** Answer detail for the next questions (D-66): the full answer or a short spoken summary. */
  answerDetail: "full" | "summary";
  setAnswerDetail(detail: "full" | "summary"): void;
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
  saveVoiceProvider(providerId: string): Promise<void>;
  saveAnswerProvider(providerId: string): Promise<void>;
  handleAudioEvent(event: AudioEvent): void;
  cancel(): Promise<void>;
  handleStreamMessage(message: StreamMessage): void;
  expireSession(): void;
  handleHandoffEvent(state: HandoffState): Promise<void>;
  dismissHandoff(): Promise<void>;
  handleUpdateEvent(state: UpdateState): void;
  loadUpdateState(): Promise<void>;
  checkForUpdate(): Promise<void>;
  downloadUpdate(): Promise<void>;
  installUpdate(): Promise<void>;
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
  "preferences.provider": "provider",
  "preferences.stt_provider": "sttProvider",
};

function createErrorMessage(error: unknown): string {
  if (!(error instanceof StoreError)) return safeMessage(error);
  const messages: Record<string, string> = {
    material_not_ready: "A selected resume or document is still being processed. Wait until it is ready, then try again.",
    material_not_found: "A selected resume or document is no longer available. Refresh the list and choose again.",
    duplicate_material: "Each document can only be chosen once.",
    too_many_documents: "You chose more documents than are allowed.",
    validation_error: "Some details were not accepted. Check the highlighted fields.",
    invalid_provider_selection: "That reasoning or voice model is not available. Choose another one.",
    rate_limited: "Too many requests. Please wait a moment and try again.",
  };
  return (error.code && messages[error.code]) || error.message;
}

function answerError(code: string): string {
  const messages: Record<string, string> = {
    llm_unavailable: "The answer service is temporarily unavailable.",
    llm_rate_limited: "The answer service is busy. Please try again shortly.",
    llm_quota_exhausted:
      "This answer provider has no credit or quota left. Choose a different provider for this session.",
    llm_timeout: "The answer took too long. Please try again.",
    llm_content_filtered: "The answer could not be generated for this question.",
    invalid_provider_selection: "This session's answer provider is no longer available.",
    retrieval_failed: "The selected session materials could not be searched.",
    empty_answer: "The answer service returned no content.",
    internal_error: "The answer could not be completed.",
    answer_not_validated: "A reliable answer could not be produced. Please rephrase and try again.",
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
        source: event.source ?? null,
      };
    case "answer.error":
      return {
        ...answer,
        phase: "failed",
        answerId: event.answerId ?? answer.answerId,
        questionId: event.questionId ?? answer.questionId,
        errorMessage: answerError(event.code),
        errorCode: event.code,
      };
  }
}

/**
 * Makes the handed-off session the selected one once main reports it READY. Selecting a session
 * already stops any capture (existing behaviour); nothing here ever starts audio or screenshots.
 */
async function selectHandedOffSession(
  get: () => AppState,
  set: (partial: Partial<AppState>) => void,
): Promise<void> {
  const { handoff } = get();
  if (handoff.phase !== "ready" || !handoff.session) return;
  // The handoff signs this app in (or switches the account) in main: learn who is signed in now.
  const auth = await window.desktop.auth.getState();
  if (!auth.ok || !auth.data.user) return;
  if (auth.data.user.id !== get().user?.id) {
    set({
      user: auth.data.user,
      sessions: [],
      selectedSessionId: null,
      history: [],
      liveAnswer: null,
      capture: { phase: "idle" },
      captureNotice: null,
      error: null,
      errorKind: null,
      ...clearedCreate(),
    });
  }
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
  sessionBusyId: null,
  sessionNotice: null,
  history: [],
  answerDetail: "full",
  setAnswerDetail: (detail) => set({ answerDetail: detail }),
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
  update: { status: "disabled", reason: "development" },
  answerProvider: { choices: null, saving: false, notice: null },
  acknowledgement: null,
  acknowledgementBusy: false,
  acknowledgementNotice: null,
  shortcuts: null,
  shortcutsBusy: false,
  shortcutsNotice: null,
  panelFocus: null,
  recoveryNotice: false,
  displays: null,
  displaysBusy: false,
  displaysNotice: null,
  capturePrivacy: null,
  capturePrivacyBusy: false,
  capturePrivacyNotice: null,
  diagnosticsBusy: false,
  diagnosticsMessage: null,
  busy: false,
  error: null,
  loadCapturePrivacy: async () => {
    try {
      set({ capturePrivacy: unwrap<CapturePrivacyState>(await window.desktop.app.getCapturePrivacy()) });
    } catch (error) {
      set({ capturePrivacyNotice: safeMessage(error) });
    }
  },
  setCapturePrivacy: async (enabled) => {
    if (get().capturePrivacyBusy) return;
    set({ capturePrivacyBusy: true, capturePrivacyNotice: null });
    try {
      const next = unwrap<CapturePrivacyState>(await window.desktop.app.setCapturePrivacy(enabled));
      // A declined confirmation or an unsupported device returns the unchanged state: say so.
      const notice = enabled && !next.enabled ? next.message : null;
      set({ capturePrivacy: next, capturePrivacyBusy: false, capturePrivacyNotice: notice });
    } catch (error) {
      set({ capturePrivacyBusy: false, capturePrivacyNotice: safeMessage(error) });
    }
  },
  loadDisplays: async () => {
    try {
      set({ displays: unwrap<DisplayList>(await window.desktop.app.getDisplays()).displays });
    } catch (error) {
      set({ displaysNotice: safeMessage(error) });
    }
  },
  moveToDisplay: async (displayId) => {
    if (get().displaysBusy) return;
    set({ displaysBusy: true, displaysNotice: null });
    try {
      const list = unwrap<DisplayList>(await window.desktop.app.moveToDisplay(displayId));
      set({ displays: list.displays, displaysBusy: false });
    } catch (error) {
      set({ displaysBusy: false, displaysNotice: safeMessage(error) });
    }
  },
  loadRecovery: async () => {
    try {
      const info = unwrap<RecoveryInfo>(await window.desktop.app.getRecovery());
      if (info.notice === "recovered") set({ recoveryNotice: true });
    } catch {
      // No notice is better than a broken start.
    }
  },
  dismissRecoveryNotice: () => set({ recoveryNotice: false }),
  copyDiagnostics: async () => {
    if (get().diagnosticsBusy) return;
    set({ diagnosticsBusy: true, diagnosticsMessage: null });
    try {
      const result = unwrap<CopyDiagnosticsResult>(await window.desktop.app.copyDiagnostics());
      set({
        diagnosticsBusy: false,
        diagnosticsMessage: `Copied ${result.characters.toLocaleString("en-US")} characters${result.truncated ? " (shortened)" : ""}. Paste them into your message to support.`,
      });
    } catch (error) {
      set({ diagnosticsBusy: false, diagnosticsMessage: safeMessage(error) });
    }
  },
  loadShortcuts: async () => {
    try {
      set({ shortcuts: unwrap<ShortcutState>(await window.desktop.app.getShortcuts()) });
    } catch (error) {
      set({ shortcutsNotice: safeMessage(error) });
    }
  },
  saveShortcuts: async (config) => {
    if (get().shortcutsBusy) return false;
    set({ shortcutsBusy: true, shortcutsNotice: null });
    try {
      const shortcuts = unwrap<ShortcutState>(await window.desktop.app.setShortcuts(config));
      set({ shortcuts, shortcutsBusy: false });
      return true;
    } catch (error) {
      set({ shortcutsBusy: false, shortcutsNotice: safeMessage(error) });
      return false;
    }
  },
  requestPanelFocus: (panel) =>
    set((s) => ({ panelFocus: { panel, nonce: (s.panelFocus?.nonce ?? 0) + 1 } })),
  loadAcknowledgement: async () => {
    try {
      const acknowledgement = unwrap<AcknowledgementState>(await window.desktop.app.getAcknowledgement());
      set({ acknowledgement });
    } catch {
      // Unknown means "not accepted" in the UI; main enforces the real answer either way.
      set({ acknowledgement: null });
    }
  },
  acceptAcknowledgement: async () => {
    if (get().acknowledgementBusy) return;
    set({ acknowledgementBusy: true, acknowledgementNotice: null });
    try {
      const acknowledgement = unwrap<AcknowledgementState>(
        await window.desktop.app.acceptAcknowledgement(ACCEPTABLE_USE_VERSION),
      );
      set({ acknowledgement, acknowledgementBusy: false });
    } catch (error) {
      set({ acknowledgementBusy: false, acknowledgementNotice: safeMessage(error) });
    }
  },
  initialize: async () => {
    set({ busy: true, error: null });
    try {
      const settings = unwrap<DesktopSettings>(await window.desktop.app.getSettings());
      set({ settings });
      await get().loadRecovery();
      await get().checkBackend();
      const auth = unwrap<{ user: DesktopUser | null }>(await window.desktop.auth.getState());
      set({ user: auth.user });
      if (auth.user) {
        await get().refreshSessions();
        await get().loadAcknowledgement();
      }
      // A handoff link can arrive (cold start) before this window was listening.
      const handoff = await window.desktop.handoff.getState();
      if (handoff.ok) await get().handleHandoffEvent(handoff.data);
      await get().loadUpdateState();
      set({ initialized: true });
    } catch (error) {
      set({ ...errorState(error), initialized: true });
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
      // Keep the open session; otherwise resume the most recently used one that can take questions
      // (selecting only: nothing starts listening, capturing or answering).
      const selectedSessionId =
        selected && sessions.some((session) => session.id === selected)
          ? selected
          : pickResumeSession(sessions);
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
    if (get().selectedSessionId === sessionId) {
      // Already open: keep a running answer, but reload so an interrupted one is reconciled.
      await get().loadHistory();
      return;
    }
    // A screenshot belongs to the session it was captured for, audio listens for one session and a
    // running answer belongs to one: changing session stops all three (M-9).
    await leaveSession(get);
    // The previous session's questions and answer are cleared before anything else loads, so they
    // can never show under the new one, even if loading its history fails.
    set({
      audio: IDLE_AUDIO,
      selectedSessionId: sessionId,
      history: [],
      liveAnswer: null,
      answerProvider: { choices: null, saving: false, notice: null },
    });
    await get().loadHistory();
  },
  clearSessionNotice: () => set({ sessionNotice: null }),
  renameSession: async (sessionId, title) => {
    if (get().sessionBusyId) return false;
    set({ sessionBusyId: sessionId, sessionNotice: null });
    try {
      replaceSession(set, unwrap<SessionSummary>(await window.desktop.sessions.rename(sessionId, title)));
      set({ sessionNotice: { text: "Session renamed.", tone: "ok" } });
      markReachable(set);
      return true;
    } catch (error) {
      await sessionActionFailed(get, set, error, "rename");
      return false;
    } finally {
      set({ sessionBusyId: null });
    }
  },
  endSession: async (sessionId) => {
    if (get().sessionBusyId) return;
    set({ sessionBusyId: sessionId, sessionNotice: null });
    try {
      // Completing the open session ends its listening, capture and running answer first.
      if (get().selectedSessionId === sessionId) await leaveSession(get);
      replaceSession(set, unwrap<SessionSummary>(await window.desktop.sessions.end(sessionId)));
      set({
        sessionNotice: {
          text: "Session completed. You can reopen it later; your history is kept.",
          tone: "ok",
        },
      });
      markReachable(set);
    } catch (error) {
      await sessionActionFailed(get, set, error, "complete");
    } finally {
      set({ sessionBusyId: null });
    }
  },
  reopenSession: async (sessionId) => {
    if (get().sessionBusyId) return;
    set({ sessionBusyId: sessionId, sessionNotice: null });
    try {
      replaceSession(set, unwrap<SessionSummary>(await window.desktop.sessions.reopen(sessionId)));
      set({
        sessionNotice: {
          text: "Session reopened. Nothing is recording: turn audio or screenshots on yourself.",
          tone: "ok",
        },
      });
      markReachable(set);
    } catch (error) {
      await sessionActionFailed(get, set, error, "reopen");
    } finally {
      set({ sessionBusyId: null });
    }
  },
  deleteSession: async (sessionId) => {
    if (get().sessionBusyId) return;
    set({ sessionBusyId: sessionId, sessionNotice: null });
    try {
      // The native confirmation is shown by the main process; declining changes nothing.
      const result = unwrap<{ deleted: boolean }>(await window.desktop.sessions.remove(sessionId));
      if (!result.deleted) return;
      const wasOpen = get().selectedSessionId === sessionId;
      if (wasOpen) await leaveSession(get);
      const remaining = get().sessions.filter((session) => session.id !== sessionId);
      set({
        sessions: remaining,
        sessionNotice: { text: "Session deleted.", tone: "ok" },
        ...(wasOpen
          ? {
              selectedSessionId: pickResumeSession(remaining),
              history: [],
              liveAnswer: null,
              audio: IDLE_AUDIO,
            }
          : {}),
      });
      markReachable(set);
      if (wasOpen) await get().loadHistory();
    } catch (error) {
      await sessionActionFailed(get, set, error, "delete");
    } finally {
      set({ sessionBusyId: null });
    }
  },
  exportSession: async (sessionId, format) => {
    if (get().sessionBusyId) return;
    set({ sessionBusyId: sessionId, sessionNotice: null });
    try {
      // The file is chosen, fetched and written by the main process; no content comes here.
      const result = unwrap<SessionExportResult>(
        await window.desktop.sessions.exportToFile(sessionId, format),
      );
      if (result.saved) {
        set({
          sessionNotice: {
            text: result.fileName ? `Saved ${result.fileName}.` : "Export saved.",
            tone: "ok",
          },
        });
      }
      markReachable(set);
    } catch (error) {
      await sessionActionFailed(get, set, error, "export");
    } finally {
      set({ sessionBusyId: null });
    }
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
      answerProvider: { choices: null, saving: false, notice: null },
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
        errorCode: null,
      },
    });
    try {
      const summary = get().answerDetail === "summary";
      const result = summary
        ? await window.desktop.sessions.startQuestion(
            requestId,
            sessionId,
            question,
            extractionId,
            "summary",
          )
        : extractionId === undefined
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
      if (message.event.type === "answer.error" && PROVIDER_FIX_CODES.has(message.event.code)) {
        void offerAnswerProviders();
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
    await selectHandedOffSession(get, set);
  },
  handleUpdateEvent: (state) => set({ update: state }),
  loadUpdateState: async () => {
    try {
      const result = await window.desktop.updates.getState();
      if (result.ok) set({ update: result.data });
    } catch {
      /* an unavailable updater simply stays disabled */
    }
  },
  // Each action returns the state main decided on; the renderer never decides a transition itself.
  checkForUpdate: async () => {
    try {
      const result = await window.desktop.updates.check();
      if (result.ok) set({ update: result.data });
    } catch {
      /* the next pushed event carries the outcome */
    }
  },
  downloadUpdate: async () => {
    try {
      const result = await window.desktop.updates.download();
      if (result.ok) set({ update: result.data });
    } catch {
      /* the next pushed event carries the outcome */
    }
  },
  installUpdate: async () => {
    try {
      const result = await window.desktop.updates.install();
      if (result.ok) set({ update: result.data });
    } catch {
      /* the app is quitting to install, or the next pushed event carries the outcome */
    }
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
    if (get().settings?.audioInputEnabled !== true) {
      // First Listen press: the native, main-process consent dialog appears once and the choice is
      // remembered (Settings can turn it off again). A compromised renderer cannot fake it.
      const enabled = await get().updateSettings({ audioInputEnabled: true });
      if (!enabled) {
        set((s) => ({
          error: null,
          audio: { ...s.audio, notice: "Listening was not turned on. Press Listen to try again." },
        }));
        return;
      }
    }
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
      if (error instanceof StoreError && error.code === "stt_provider_required") {
        await offerVoiceChoices();
      }
    }
  },
  saveAnswerProvider: async (providerId) => {
    const sessionId = get().selectedSessionId;
    if (!sessionId || get().answerProvider.saving) return;
    set((s) => ({ answerProvider: { ...s.answerProvider, saving: true, notice: null } }));
    try {
      unwrap<void>(await window.desktop.sessions.setReasoning(sessionId, providerId));
      set({
        answerProvider: {
          choices: null,
          saving: false,
          notice: "Answer provider saved for this session. Ask your question again.",
        },
      });
    } catch (error) {
      set((s) => ({ answerProvider: { ...s.answerProvider, saving: false, notice: safeMessage(error) } }));
    }
  },
  saveVoiceProvider: async (providerId) => {
    const sessionId = get().selectedSessionId;
    if (!sessionId || get().audio.voiceSaving) return;
    set((s) => ({ audio: { ...s.audio, voiceSaving: true, notice: null } }));
    try {
      unwrap<void>(await window.desktop.sessions.setVoice(sessionId, providerId));
      set((s) => ({
        audio: {
          ...s.audio,
          voiceChoices: null,
          voiceSaving: false,
          notice: "Voice provider saved for this session. Press Listen to start.",
        },
      }));
    } catch (error) {
      set((s) => ({ audio: { ...s.audio, voiceSaving: false, notice: safeMessage(error) } }));
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
        {
          // The user pressed Listen, so a detected question is answered at once. Anything else, or
          // a question that arrives while an answer is still streaming, waits for the Ask button.
          const current = get();
          const session = current.sessions.find((x) => x.id === current.selectedSessionId);
          const answering =
            !!current.liveAnswer &&
            ["sending", "retrieving", "generating", "streaming"].includes(current.liveAnswer.phase);
          if (event.isQuestion && !event.truncated && session?.status === "ready" && !answering) {
            void current.askFromAudio(event.id);
          }
        }
        return;
      case "notice":
        set((s) => ({ audio: { ...s.audio, notice: event.message } }));
        return;
      case "stopped":
        endLocalCapture();
        set((s) => ({ audio: { ...s.audio, state: "idle", source: null, notice: event.message } }));
        if (event.reason === "voice_provider_required") void offerVoiceChoices();
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
      // A provider the user picked stays only while the server still offers it.
      const stillOffered = (value: string, list: { id: string }[]) =>
        list.some((choice) => choice.id === value) ? value : undefined;
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
          provider: stillOffered(current.provider, loaded.options.providers) ?? defaults.provider,
          sttProvider:
            stillOffered(current.sttProvider, loaded.options.voice_providers) ?? defaults.sttProvider,
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

/** Stops what belongs to the open session: screenshot countdown, listening and a running answer. */
async function leaveSession(get: () => AppState): Promise<void> {
  if (get().capture.phase !== "idle") await get().cancelCapture();
  if (get().audio.state !== "idle") await get().stopAudio();
  const live = get().liveAnswer;
  if (live && !["done", "failed", "cancelled"].includes(live.phase)) await get().cancel();
}

function replaceSession(set: StoreSet, updated: SessionSummary): void {
  set((state) => ({
    sessions: state.sessions.map((session) => (session.id === updated.id ? updated : session)),
  }));
}

/**
 * A management action failed. A session the server no longer has (deleted elsewhere) refreshes the
 * list; a network failure says nothing was changed; anything else shows the server's safe message.
 */
async function sessionActionFailed(
  get: () => AppState,
  set: StoreSet,
  error: unknown,
  action: string,
): Promise<void> {
  if (error instanceof StoreError && error.code === "session_not_found") {
    set({ sessionNotice: { text: "This session is no longer available.", tone: "error" } });
    await get().refreshSessions();
    return;
  }
  if (isNetworkError(error)) {
    set({
      connection: "unavailable",
      sessionNotice: {
        text: `Could not reach the backend, so the session was not changed (${action}). Try again when you are connected.`,
        tone: "error",
      },
    });
    return;
  }
  set({ sessionNotice: { text: safeMessage(error), tone: "error" } });
}

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

/**
 * The selected session has no voice-listening provider: load the providers the server offers so the
 * Audio panel can show a picker. If they cannot be loaded the notice already tells the user why
 * listening did not start, and Listen can simply be pressed again later.
 */
async function offerVoiceChoices(): Promise<void> {
  const store = useDesktopStore;
  try {
    const loaded = unwrap<CreateSessionOptions>(await window.desktop.sessions.createOptions());
    store.setState((s) => ({ audio: { ...s.audio, voiceChoices: loaded.options.voice_providers } }));
  } catch (error) {
    store.setState((s) => ({ audio: { ...s.audio, notice: safeMessage(error) } }));
  }
}

/** Failures that a different answer provider can fix (a timeout or a refused question cannot). */
const PROVIDER_FIX_CODES = new Set([
  "llm_quota_exhausted",
  "llm_unavailable",
  "llm_rate_limited",
  "invalid_provider_selection",
]);

/** Loads the answer providers the server offers so the Assistant page can show a picker. */
async function offerAnswerProviders(): Promise<void> {
  try {
    const loaded = unwrap<CreateSessionOptions>(await window.desktop.sessions.createOptions());
    useDesktopStore.setState({
      answerProvider: { choices: loaded.options.providers, saving: false, notice: null },
    });
  } catch {
    // The failure message on the answer card already explains what happened.
  }
}
