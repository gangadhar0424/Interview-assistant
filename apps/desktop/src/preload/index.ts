import { contextBridge, ipcRenderer } from "electron";
import type { AcknowledgementState } from "../shared/acknowledgement";
import type { CapturePrivacyState } from "../shared/capture-privacy";
import type { DisplayList } from "../shared/placement";
import type { CopyDiagnosticsResult, RecoveryInfo } from "../shared/recovery";
import type { ShortcutEvent, ShortcutState } from "../shared/shortcuts";
import type { UpdateState } from "../shared/updates";
import type {
  AudioEvent,
  CaptureDisplay,
  CaptureEvent,
  DesktopBridge,
  DesktopResult,
  CreateSessionOptions,
  DesktopSettings,
  ExtractionResult,
  HandoffState,
  SessionExportResult,
  SessionSummary,
  StreamMessage,
} from "../shared/contracts";

const bridge: DesktopBridge = {
  app: {
    getSettings: () => ipcRenderer.invoke("app:get-settings") as Promise<DesktopResult<DesktopSettings>>,
    updateSettings: (patch) =>
      ipcRenderer.invoke("app:update-settings", patch) as Promise<DesktopResult<DesktopSettings>>,
    checkBackend: () =>
      ipcRenderer.invoke("app:check-backend") as Promise<
        DesktopResult<{ status: "connected" }>
      >,
    setExpanded: (expanded) =>
      ipcRenderer.invoke("app:set-expanded", expanded) as Promise<
        DesktopResult<DesktopSettings>
      >,
    minimize: () => ipcRenderer.invoke("app:minimize") as Promise<DesktopResult<void>>,
    closeToTray: () => ipcRenderer.invoke("app:close-to-tray") as Promise<DesktopResult<void>>,
    copyText: (text) => ipcRenderer.invoke("app:copy-text", text) as Promise<DesktopResult<void>>,
    getAcknowledgement: () =>
      ipcRenderer.invoke("app:get-acknowledgement") as Promise<DesktopResult<AcknowledgementState>>,
    acceptAcknowledgement: (version) =>
      ipcRenderer.invoke("app:accept-acknowledgement", version) as Promise<
        DesktopResult<AcknowledgementState>
      >,
    getShortcuts: () => ipcRenderer.invoke("app:get-shortcuts") as Promise<DesktopResult<ShortcutState>>,
    setShortcuts: (config) =>
      ipcRenderer.invoke("app:set-shortcuts", config) as Promise<DesktopResult<ShortcutState>>,
    getRecovery: () => ipcRenderer.invoke("app:get-recovery") as Promise<DesktopResult<RecoveryInfo>>,
    copyDiagnostics: () =>
      ipcRenderer.invoke("app:copy-diagnostics") as Promise<DesktopResult<CopyDiagnosticsResult>>,
    getCapturePrivacy: () =>
      ipcRenderer.invoke("app:get-capture-privacy") as Promise<DesktopResult<CapturePrivacyState>>,
    setCapturePrivacy: (enabled) =>
      ipcRenderer.invoke("app:set-capture-privacy", enabled) as Promise<DesktopResult<CapturePrivacyState>>,
    getDisplays: () => ipcRenderer.invoke("app:get-displays") as Promise<DesktopResult<DisplayList>>,
    moveToDisplay: (displayId) =>
      ipcRenderer.invoke("app:move-to-display", displayId) as Promise<DesktopResult<DisplayList>>,
  },
  auth: {
    getState: () => ipcRenderer.invoke("auth:get-state"),
    logout: () => ipcRenderer.invoke("auth:logout"),
  },
  sessions: {
    list: () => ipcRenderer.invoke("sessions:list"),
    questions: (sessionId) => ipcRenderer.invoke("sessions:questions", sessionId),
    startQuestion: (requestId, sessionId, text, extractionId, detail) =>
      detail !== undefined
        ? ipcRenderer.invoke("sessions:stream-start", requestId, sessionId, text, extractionId, detail)
        : extractionId === undefined
          ? ipcRenderer.invoke("sessions:stream-start", requestId, sessionId, text)
          : ipcRenderer.invoke("sessions:stream-start", requestId, sessionId, text, extractionId),
    cancelQuestion: (requestId) =>
      ipcRenderer.invoke("sessions:stream-cancel", requestId),
    createOptions: () =>
      ipcRenderer.invoke("sessions:create-options") as Promise<DesktopResult<CreateSessionOptions>>,
    create: (draft) =>
      ipcRenderer.invoke("sessions:create", draft) as Promise<DesktopResult<SessionSummary>>,
    setVoice: (sessionId, provider) =>
      ipcRenderer.invoke("sessions:set-voice", sessionId, provider) as Promise<DesktopResult<void>>,
    setReasoning: (sessionId, provider) =>
      ipcRenderer.invoke("sessions:set-reasoning", sessionId, provider) as Promise<DesktopResult<void>>,
    rename: (sessionId, title) =>
      ipcRenderer.invoke("sessions:rename", sessionId, title) as Promise<DesktopResult<SessionSummary>>,
    end: (sessionId) =>
      ipcRenderer.invoke("sessions:end", sessionId) as Promise<DesktopResult<SessionSummary>>,
    reopen: (sessionId) =>
      ipcRenderer.invoke("sessions:reopen", sessionId) as Promise<DesktopResult<SessionSummary>>,
    remove: (sessionId) =>
      ipcRenderer.invoke("sessions:delete", sessionId) as Promise<DesktopResult<{ deleted: boolean }>>,
    exportToFile: (sessionId, format) =>
      ipcRenderer.invoke("sessions:export", sessionId, format) as Promise<
        DesktopResult<SessionExportResult>
      >,
  },
  updates: {
    getState: () => ipcRenderer.invoke("updates:get-state") as Promise<DesktopResult<UpdateState>>,
    check: () => ipcRenderer.invoke("updates:check") as Promise<DesktopResult<UpdateState>>,
    download: () => ipcRenderer.invoke("updates:download") as Promise<DesktopResult<UpdateState>>,
    install: () => ipcRenderer.invoke("updates:install") as Promise<DesktopResult<UpdateState>>,
  },
  handoff: {
    getState: () => ipcRenderer.invoke("handoff:get-state") as Promise<DesktopResult<HandoffState>>,
    dismiss: () => ipcRenderer.invoke("handoff:dismiss") as Promise<DesktopResult<void>>,
  },
  capture: {
    listDisplays: () =>
      ipcRenderer.invoke("capture:list-displays") as Promise<DesktopResult<CaptureDisplay[]>>,
    start: (displayId, sessionId) =>
      ipcRenderer.invoke("capture:start", displayId, sessionId) as Promise<DesktopResult<void>>,
    cancel: () => ipcRenderer.invoke("capture:cancel") as Promise<DesktopResult<void>>,
    extract: (captureId) =>
      ipcRenderer.invoke("capture:extract", captureId) as Promise<DesktopResult<ExtractionResult>>,
  },
  audio: {
    start: (source, sessionId) =>
      ipcRenderer.invoke("audio:start", source, sessionId) as Promise<DesktopResult<void>>,
    stop: () => ipcRenderer.invoke("audio:stop") as Promise<DesktopResult<void>>,
    sendChunk: (chunk) => ipcRenderer.send("audio:chunk", chunk),
  },
  onAudioEvent: (listener) => {
    const wrapped = (_event: Electron.IpcRendererEvent, event: AudioEvent) => listener(event);
    ipcRenderer.on("audio:event", wrapped);
    return () => ipcRenderer.removeListener("audio:event", wrapped);
  },
  onShortcutEvent: (listener) => {
    const wrapped = (_event: Electron.IpcRendererEvent, event: ShortcutEvent) => listener(event);
    ipcRenderer.on("shortcut:event", wrapped);
    return () => ipcRenderer.removeListener("shortcut:event", wrapped);
  },
  onCaptureEvent: (listener) => {
    const wrapped = (_event: Electron.IpcRendererEvent, event: CaptureEvent) => listener(event);
    ipcRenderer.on("capture:event", wrapped);
    return () => ipcRenderer.removeListener("capture:event", wrapped);
  },
  onStreamMessage: (listener) => {
    const wrapped = (_event: Electron.IpcRendererEvent, message: StreamMessage) =>
      listener(message);
    ipcRenderer.on("stream:message", wrapped);
    return () => ipcRenderer.removeListener("stream:message", wrapped);
  },
  onHandoffEvent: (listener) => {
    const wrapped = (_event: Electron.IpcRendererEvent, state: HandoffState) => listener(state);
    ipcRenderer.on("handoff:event", wrapped);
    return () => ipcRenderer.removeListener("handoff:event", wrapped);
  },
  onUpdateEvent: (listener) => {
    const wrapped = (_event: Electron.IpcRendererEvent, state: UpdateState) => listener(state);
    ipcRenderer.on("updates:event", wrapped);
    return () => ipcRenderer.removeListener("updates:event", wrapped);
  },
  onSessionExpired: (listener) => {
    const wrapped = () => listener();
    ipcRenderer.on("auth:expired", wrapped);
    return () => ipcRenderer.removeListener("auth:expired", wrapped);
  },
};

contextBridge.exposeInMainWorld("desktop", bridge);
