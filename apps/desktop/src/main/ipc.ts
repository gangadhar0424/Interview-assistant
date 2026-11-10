import { randomUUID } from "node:crypto";
import type { BrowserWindow, IpcMain, IpcMainInvokeEvent } from "electron";
import { clipboard } from "electron";
import { DesktopApiClient, DesktopApiError, asDesktopError } from "./api-client";
import { CaptureRefusal, CaptureService, type CaptureHost } from "./capture-service";
import type { SafeStoragePort } from "./secure-refresh-store";
import { SecureRefreshTokenStore } from "./secure-refresh-store";
import { SettingsStore } from "./settings-store";
import { isTrustedRenderer } from "./ipc-security";
import { hasErrors, parseDraft, toCreateBody, validateDraft } from "../shared/session-form";
import {
  isDisplayId,
  isUuid,
  validateCopyText,
  validateSettingsPatch,
} from "./ipc-validation";
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
  /** Electron-bound screen/tray pieces for screenshot input (Phase 11B). */
  captureHost: CaptureHost;
  /** Native (main-process) confirmation shown before screenshot input is turned on. */
  confirmScreenshotConsent: () => Promise<boolean>;
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
  if (error instanceof CaptureRefusal) {
    return { kind: "invalid_input", code: error.code, message: error.message };
  }
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
    captureHost,
    confirmScreenshotConsent,
  } = deps;
  const tokenStore = new SecureRefreshTokenStore(secureTokenPath, safeStorage);
  let settings = settingsStore.load();
  let api = new DesktopApiClient(settings.backendBaseUrl, tokenStore);
  const streams = new Map<string, ActiveStream>();
  const handlers: string[] = [];

  // Screenshot input (Phase 11B, D-51): capture logic is main-process only. The renderer gets named,
  // validated channels and a small preview; never the full image, never a generic capture API.
  const capture = new CaptureService({
    ...captureHost,
    isEnabled: () => settings.screenshotInputEnabled,
    isSignedIn: () => api.getUser() !== null,
    windowState: () => ({
      visible: !window.isDestroyed() && window.isVisible(),
      focused: !window.isDestroyed() && window.isFocused(),
      minimized: !window.isDestroyed() && window.isMinimized(),
    }),
    sessionReady: async (sessionId) =>
      (await api.listSessions()).some((item) => item.id === sessionId && item.status === "ready"),
    upload: (png, sessionId, signal) => api.extractScreenshot(sessionId, png, signal),
    emit: (event) => {
      if (!window.isDestroyed()) window.webContents.send("capture:event", event);
    },
    now: () => Date.now(),
  });
  const notifySessionExpired = (): void => {
    capture.discard("signed_out");
    if (!window.isDestroyed()) window.webContents.send("auth:expired");
  };
  // Window cleanup: a held image never survives the window being hidden, closed or reloaded.
  const onWindowHidden = () => capture.discard("window");
  const onWindowClosed = () => capture.dispose();
  const onNavigated = () => capture.discard("navigation");
  window.on("hide", onWindowHidden);
  window.on("minimize", onWindowHidden);
  window.on("closed", onWindowClosed);
  window.webContents.on("did-start-navigation", onNavigated);
  window.webContents.on("render-process-gone", onWindowClosed);

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
        if (authenticated && isSessionExpired(error) && !window.isDestroyed()) notifySessionExpired();
        return failure(safeError(error));
      }
    });
    handlers.push(channel);
  };

  handle("app:get-settings", () => settingsStore.current());
  handle("app:update-settings", async (_event, input) => {
    const patch = validateSettingsPatch(input);
    // Turning screenshot input ON needs a native confirmation the renderer cannot fake or skip.
    if (patch.screenshotInputEnabled === true && !settings.screenshotInputEnabled) {
      if (!(await confirmScreenshotConsent())) throw new Error("Screenshot input was not turned on.");
    }
    const next = settingsStore.update(patch);
    if (!next.screenshotInputEnabled) capture.discard("disabled");
    const backendChanged = next.backendBaseUrl !== settings.backendBaseUrl;
    if (backendChanged) {
      for (const stream of streams.values()) stream.controller.abort();
      streams.clear();
      api.updateBaseUrl(next.backendBaseUrl);
      notifySessionExpired();
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
    capture.discard("signed_out");
    for (const stream of streams.values()) stream.controller.abort();
    streams.clear();
    await api.logout();
  }, true);

  handle("sessions:list", () => api.listSessions(), true);
  handle("sessions:questions", (_event, sessionId) => {
    if (!validateId(sessionId)) throw new Error("Choose a valid session.");
    return api.listQuestions(sessionId);
  }, true);
  handle("sessions:stream-start", (event, requestId, sessionId, text, extractionId) => {
    if (!validateId(requestId) || !validateId(sessionId)) {
      throw new Error("The session request is invalid.");
    }
    if (typeof text !== "string" || !text.trim() || text.length > 2000) {
      throw new Error("Enter a question of 1–2000 characters.");
    }
    if (streams.has(requestId)) throw new Error("This answer is already running.");
    // `input_source = screenshot` only follows a completed screenshot extraction in this session.
    let fromScreenshot = false;
    if (extractionId !== undefined) {
      if (!isUuid(extractionId) || !capture.consumeExtraction(extractionId, sessionId)) {
        throw new Error("The screenshot text is no longer available. Capture again.");
      }
      fromScreenshot = true;
    }
    const controller = new AbortController();
    streams.set(requestId, { controller, senderId: event.sender.id });
    const client = api;
    let questionStarted = false;
    setImmediate(() => {
      void client
        .streamQuestion(
          sessionId,
          text.trim(),
          controller.signal,
          (qaEvent) => {
            if (qaEvent.type === "question.created") questionStarted = true;
            sendToRenderer(window, { requestId, event: qaEvent });
          },
          fromScreenshot ? "screenshot" : undefined,
        )
        .catch((error: unknown) => {
          // The question never started (refused or failed early): the user may simply try again.
          if (fromScreenshot && !questionStarted && extractionId !== undefined) {
            capture.restoreExtraction(extractionId, sessionId);
          }
          if (controller.signal.aborted) {
            sendToRenderer(window, { requestId, cancelled: true });
            return;
          }
          if (isSessionExpired(error) && !window.isDestroyed()) notifySessionExpired();
          sendToRenderer(window, { requestId, error: asDesktopError(error) });
        })
        .finally(() => streams.delete(requestId));
    });
    return undefined;
  }, true);
  // ---- Desktop session creation (Phase 11.5). Narrow channels; the renderer never calls the API. ----
  handle("sessions:create-options", async () => {
    const [resumes, documents, options] = await Promise.all([
      api.listFiles("resumes"),
      api.listFiles("documents"),
      api.getSessionOptions(),
    ]);
    return { resumes, documents, options };
  }, true);
  handle("sessions:create", async (_event, input) => {
    const draft = parseDraft(input); // exact keys and types, or a safe refusal
    const issues = validateDraft(draft, null);
    if (hasErrors(issues)) {
      throw new Error(Object.values(issues)[0] ?? "The session details are not valid.");
    }
    // The backend stays authoritative for readiness, ownership, limits and the enum values.
    return api.createSession(toCreateBody(draft));
  }, true);

  // ---- Screenshot input channels (Phase 11B). Named and validated; no generic capture API. ----
  handle("capture:list-displays", () => capture.listDisplays(), true);
  handle("capture:start", async (_event, displayId, sessionId) => {
    if (!isDisplayId(displayId) || !validateId(sessionId)) {
      throw new Error("Choose a display and a session.");
    }
    await capture.start(displayId, sessionId);
  }, true);
  handle("capture:cancel", () => capture.cancel(), true);
  handle("capture:extract", (_event, captureId) => {
    if (!isUuid(captureId)) throw new Error("That screenshot is no longer available.");
    return capture.extract(captureId);
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
    capture.dispose();
    window.removeListener("hide", onWindowHidden);
    window.removeListener("minimize", onWindowHidden);
    window.removeListener("closed", onWindowClosed);
    window.webContents.removeListener("did-start-navigation", onNavigated);
    window.webContents.removeListener("render-process-gone", onWindowClosed);
    window.webContents.removeListener("destroyed", onClosed);
    for (const stream of streams.values()) stream.controller.abort();
    streams.clear();
    for (const channel of handlers) ipcMain.removeHandler(channel);
  };
}

export function newRequestId(): string {
  return randomUUID();
}
