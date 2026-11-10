import { randomUUID } from "node:crypto";
import type { BrowserWindow, IpcMain, IpcMainInvokeEvent } from "electron";
import { clipboard } from "electron";
import { DesktopApiClient, DesktopApiError, asDesktopError } from "./api-client";
import type { SafeStoragePort } from "./secure-refresh-store";
import { SecureRefreshTokenStore } from "./secure-refresh-store";
import { SettingsStore } from "./settings-store";
import { isTrustedRenderer } from "./ipc-security";
import { validateCopyText, validateSettingsPatch } from "./ipc-validation";
import type { DesktopError, DesktopResult, StreamMessage } from "../shared/contracts";
import { applyWindowSettings, setExpanded } from "./window-controller";

type IpcDependencies = {
  ipcMain: IpcMain;
  window: BrowserWindow;
  settings: SettingsStore;
  safeStorage: SafeStoragePort;
  secureTokenPath: string;
  rendererHtmlPath: string;
  devServerUrl: string | undefined;
};

type ActiveStream = { controller: AbortController; senderId: number };

function success<T>(data: T): DesktopResult<T> {
  return { ok: true, data };
}

function failure<T>(error: DesktopError): DesktopResult<T> {
  return { ok: false, error };
}

function sendToRenderer(window: BrowserWindow, message: StreamMessage): void {
  if (!window.isDestroyed()) window.webContents.send("stream:message", message);
}

function validateId(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f-]{36}$/i.test(value);
}

function isSessionExpired(error: unknown): boolean {
  return error instanceof DesktopApiError && error.details.status === 401;
}

function safeError(error: unknown): DesktopError {
  if (error instanceof DesktopApiError) return error.details;
  if (error instanceof Error && error.message.startsWith("Could not ")) {
    return { kind: "storage", code: "storage_error", message: error.message };
  }
  if (error instanceof Error && error.message) {
    return { kind: "invalid_input", code: "invalid_input", message: error.message };
  }
  return asDesktopError(error);
}

export function registerDesktopIpc(deps: IpcDependencies): () => void {
  const {
    ipcMain,
    window,
    settings: settingsStore,
    safeStorage,
    secureTokenPath,
    rendererHtmlPath,
    devServerUrl,
  } = deps;
  const tokenStore = new SecureRefreshTokenStore(secureTokenPath, safeStorage);
  let settings = settingsStore.load();
  let api = new DesktopApiClient(settings.backendBaseUrl, tokenStore);
  const streams = new Map<string, ActiveStream>();
  const handlers: string[] = [];

  const assertSender = (event: IpcMainInvokeEvent): void => {
    const url = event.senderFrame?.url ?? "";
    if (!isTrustedRenderer(url, devServerUrl, rendererHtmlPath)) {
      throw new Error("Untrusted IPC sender.");
    }
  };

  const handle = <T>(
    channel: string,
    handler: (event: IpcMainInvokeEvent, ...args: unknown[]) => Promise<T> | T,
    authenticated = false,
  ) => {
    ipcMain.handle(channel, async (event, ...args: unknown[]): Promise<DesktopResult<T>> => {
      try {
        assertSender(event);
        return success(await handler(event, ...args));
      } catch (error) {
        if (authenticated && isSessionExpired(error) && !window.isDestroyed()) {
          window.webContents.send("auth:expired");
        }
        return failure(safeError(error));
      }
    });
    handlers.push(channel);
  };

  handle("app:get-settings", () => settingsStore.current());
  handle("app:update-settings", (_event, input) => {
    const next = settingsStore.update(validateSettingsPatch(input));
    const backendChanged = next.backendBaseUrl !== settings.backendBaseUrl;
    if (backendChanged) {
      for (const stream of streams.values()) stream.controller.abort();
      streams.clear();
      api.updateBaseUrl(next.backendBaseUrl);
      if (!window.isDestroyed()) window.webContents.send("auth:expired");
    }
    if (next.expanded !== settings.expanded) setExpanded(window, next.expanded);
    applyWindowSettings(window, next);
    settings = next;
    return next;
  });
  handle("app:check-backend", async () => {
    await api.checkBackend();
    return { status: "connected" as const };
  });
  handle("app:set-expanded", (_event, expanded) => {
    if (typeof expanded !== "boolean") throw new Error("Expanded must be on or off.");
    settings = settingsStore.update({ expanded });
    setExpanded(window, expanded);
    return settings;
  });
  handle("app:minimize", () => window.minimize());
  handle("app:close-to-tray", () => window.hide());
  handle("app:copy-text", (_event, text) => {
    clipboard.writeText(validateCopyText(text));
  });

  handle("auth:get-state", async () => ({ user: await api.initialize() }));
  handle("auth:login", async (_event, email, password) => {
    if (typeof email !== "string" || typeof password !== "string") {
      throw new Error("Enter an email and password.");
    }
    return api.login(email, password);
  });
  handle("auth:signup", async (_event, email, password) => {
    if (typeof email !== "string" || typeof password !== "string") {
      throw new Error("Enter an email and password.");
    }
    return api.signup(email, password);
  });
  handle("auth:logout", async () => {
    for (const stream of streams.values()) stream.controller.abort();
    streams.clear();
    await api.logout();
  }, true);

  handle("sessions:list", () => api.listSessions(), true);
  handle("sessions:questions", (_event, sessionId) => {
    if (!validateId(sessionId)) throw new Error("Choose a valid session.");
    return api.listQuestions(sessionId);
  }, true);
  handle("sessions:stream-start", (event, requestId, sessionId, text) => {
    if (!validateId(requestId) || !validateId(sessionId)) {
      throw new Error("The session request is invalid.");
    }
    if (typeof text !== "string" || !text.trim() || text.length > 2000) {
      throw new Error("Enter a question of 1–2000 characters.");
    }
    if (streams.has(requestId)) throw new Error("This answer is already running.");
    const controller = new AbortController();
    streams.set(requestId, { controller, senderId: event.sender.id });
    const client = api;
    setImmediate(() => {
      void client
        .streamQuestion(sessionId, text.trim(), controller.signal, (qaEvent) => {
          sendToRenderer(window, { requestId, event: qaEvent });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) {
            sendToRenderer(window, { requestId, cancelled: true });
            return;
          }
          if (isSessionExpired(error) && !window.isDestroyed()) {
            window.webContents.send("auth:expired");
          }
          sendToRenderer(window, { requestId, error: asDesktopError(error) });
        })
        .finally(() => streams.delete(requestId));
    });
    return undefined;
  }, true);
  handle("sessions:stream-cancel", (event, requestId) => {
    if (typeof requestId !== "string") throw new Error("The stream request is invalid.");
    const active = streams.get(requestId);
    if (!active || active.senderId !== event.sender.id) {
      throw new Error("The answer is no longer running.");
    }
    active.controller.abort();
  }, true);

  const onClosed = () => {
    for (const stream of streams.values()) {
      if (stream.senderId === window.webContents.id) stream.controller.abort();
    }
  };
  window.webContents.once("destroyed", onClosed);

  return () => {
    window.webContents.removeListener("destroyed", onClosed);
    for (const stream of streams.values()) stream.controller.abort();
    streams.clear();
    for (const channel of handlers) ipcMain.removeHandler(channel);
  };
}

export function newRequestId(): string {
  return randomUUID();
}
