import { contextBridge, ipcRenderer } from "electron";
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
  },
  auth: {
    getState: () => ipcRenderer.invoke("auth:get-state"),
    login: (email, password) => ipcRenderer.invoke("auth:login", email, password),
    signup: (email, password) => ipcRenderer.invoke("auth:signup", email, password),
    logout: () => ipcRenderer.invoke("auth:logout"),
  },
  sessions: {
    list: () => ipcRenderer.invoke("sessions:list"),
    questions: (sessionId) => ipcRenderer.invoke("sessions:questions", sessionId),
    startQuestion: (requestId, sessionId, text, extractionId) =>
      extractionId === undefined
        ? ipcRenderer.invoke("sessions:stream-start", requestId, sessionId, text)
        : ipcRenderer.invoke("sessions:stream-start", requestId, sessionId, text, extractionId),
    cancelQuestion: (requestId) =>
      ipcRenderer.invoke("sessions:stream-cancel", requestId),
    createOptions: () =>
      ipcRenderer.invoke("sessions:create-options") as Promise<DesktopResult<CreateSessionOptions>>,
    create: (draft) =>
      ipcRenderer.invoke("sessions:create", draft) as Promise<DesktopResult<SessionSummary>>,
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
  onSessionExpired: (listener) => {
    const wrapped = () => listener();
    ipcRenderer.on("auth:expired", wrapped);
    return () => ipcRenderer.removeListener("auth:expired", wrapped);
  },
};

contextBridge.exposeInMainWorld("desktop", bridge);
