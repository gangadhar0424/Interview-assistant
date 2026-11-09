import { contextBridge, ipcRenderer } from "electron";
import type { DesktopBridge, DesktopResult, DesktopSettings, StreamMessage } from "../shared/contracts";

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
    startQuestion: (requestId, sessionId, text) =>
      ipcRenderer.invoke("sessions:stream-start", requestId, sessionId, text),
    cancelQuestion: (requestId) =>
      ipcRenderer.invoke("sessions:stream-cancel", requestId),
  },
  onStreamMessage: (listener) => {
    const wrapped = (_event: Electron.IpcRendererEvent, message: StreamMessage) =>
      listener(message);
    ipcRenderer.on("stream:message", wrapped);
    return () => ipcRenderer.removeListener("stream:message", wrapped);
  },
  onSessionExpired: (listener) => {
    const wrapped = () => listener();
    ipcRenderer.on("auth:expired", wrapped);
    return () => ipcRenderer.removeListener("auth:expired", wrapped);
  },
};

contextBridge.exposeInMainWorld("desktop", bridge);
