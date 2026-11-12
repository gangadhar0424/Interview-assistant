import { randomUUID } from "node:crypto";
import type { BrowserWindow, IpcMain, IpcMainInvokeEvent } from "electron";
import { clipboard } from "electron";
import { DesktopApiClient, DesktopApiError, asDesktopError } from "./api-client";
import { AudioRefusal, AudioService, type AudioIndicator } from "./audio-service";
import { CaptureRefusal, CaptureService, type CaptureHost } from "./capture-service";
import type { AudioGateState, AudioSource } from "./media-permissions";
import type { SafeStoragePort } from "./secure-refresh-store";
import { SecureRefreshTokenStore } from "./secure-refresh-store";
import { SettingsStore } from "./settings-store";
import { isTrustedRenderer } from "./ipc-security";
import type { DeepLinkInbox } from "./deep-link";
import { HandoffController } from "./handoff";
import { hasErrors, parseDraft, toCreateBody, validateDraft } from "../shared/session-form";
import {
  isAudioSource,
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
  /** Electron-bound tray/taskbar indication for audio input (Phase 12). */
  audioHost: { setIndicator(state: AudioIndicator, source: AudioSource | null): void };
  /** Lets the media-permission handlers (index.ts) read the live audio state; set here. */
  audioGate: { read: () => AudioGateState };
  /** Native (main-process) confirmation shown before audio input is turned on. */
  confirmAudioConsent: () => Promise<boolean>;
  /** Native (main-process) confirmation shown before screenshot input is turned on. */
  confirmScreenshotConsent: () => Promise<boolean>;
  /** Web -> desktop handoff (D-55): where links from the OS arrive, plus its two native pieces. */
  deepLinks: DeepLinkInbox;
  /** Native confirmation before a handoff switches away from a running capture. */
  confirmHandoffSwitch: () => Promise<boolean>;
  /** Native confirmation before a handoff signs another account in over the current one. */
  confirmHandoffAccountSwitch: () => Promise<boolean>;
  /** Restores and focuses the window so a handoff result is visible. */
  showWindow: () => void;
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
  if (error instanceof CaptureRefusal || error instanceof AudioRefusal) {
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
    audioHost,
    audioGate,
    confirmAudioConsent,
    deepLinks,
    confirmHandoffSwitch,
    confirmHandoffAccountSwitch,
    showWindow,
  } = deps;
  const tokenStore = new SecureRefreshTokenStore(secureTokenPath, safeStorage);
  let settings = settingsStore.load();
  let api = new DesktopApiClient(settings.backendBaseUrl, tokenStore);
  const streams = new Map<string, ActiveStream>();
  const handlers: string[] = [];
  // A sign-in/restore in flight: a handoff waits for it so two logins never race for the stored one.
  let authInFlight: Promise<unknown> | null = null;
  const noop = (): void => undefined;
  function trackAuth<T>(operation: Promise<T>): Promise<T> {
    const tracked = operation.finally(() => {
      if (authInFlight === tracked) authInFlight = null;
    });
    authInFlight = tracked;
    return tracked;
  }

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
  // Live audio (Phase 12, D-53): capture runs in the renderer (Chromium's media APIs), but the
  // permission, the on/off state, segmentation, buffers, upload and wiping are all here in main.
  const audio = new AudioService({
    isEnabled: () => settings.audioInputEnabled,
    isSignedIn: () => api.getUser() !== null,
    windowState: () => ({
      visible: !window.isDestroyed() && window.isVisible(),
      focused: !window.isDestroyed() && window.isFocused(),
      minimized: !window.isDestroyed() && window.isMinimized(),
    }),
    sessionReady: async (sessionId) =>
      (await api.listSessions()).some((item) => item.id === sessionId && item.status === "ready"),
    transcribe: async (wav, sessionId, signal) => {
      try {
        return await api.transcribeUtterance(sessionId, wav, signal);
      } catch (error) {
        if (isSessionExpired(error) && !window.isDestroyed()) notifySessionExpired();
        throw error;
      }
    },
    describeError: (error) => asDesktopError(error).message,
    isFatal: (error) => error instanceof DesktopApiError && error.details.code === "stt_unavailable",
    emit: (event) => {
      if (!window.isDestroyed()) window.webContents.send("audio:event", event);
    },
    setIndicator: (state, source) => audioHost.setIndicator(state, source),
    now: () => Date.now(),
  });
  audioGate.read = () => audio.gate();
  // Web -> desktop handoff (D-55). The code is redeemed only as this app's own signed-in user, is
  // never sent to the renderer, and READY never starts audio or screenshot capture.
  const handoff = new HandoffController({
    settled: () => (authInFlight ?? Promise.resolve()).then(noop, noop),
    currentUserId: () => api.getUser()?.id ?? null,
    redeem: (code) => api.redeemHandoff(code),
    isLive: () => capture.busy() || audio.state !== "idle",
    stopLive: () => {
      capture.discard("session");
      audio.stop("session");
    },
    resetForAccountChange: () => {
      for (const stream of streams.values()) stream.controller.abort();
      streams.clear();
    },
    confirmSwitch: confirmHandoffSwitch,
    confirmAccountSwitch: confirmHandoffAccountSwitch,
    emit: (state) => {
      if (!window.isDestroyed()) window.webContents.send("handoff:event", state);
    },
    showWindow,
    now: () => Date.now(),
  });
  deepLinks.attach((raw) => handoff.receiveLink(raw));
  function notifySessionExpired(): void {
    handoff.onSignedOut();
    capture.discard("signed_out");
    audio.stop("signed_out");
    if (!window.isDestroyed()) window.webContents.send("auth:expired");
  }
  // Window cleanup: a held image or a live capture never survives the window being hidden, closed
  // or reloaded.
  const onWindowHidden = () => {
    capture.discard("window");
    audio.stop("window");
  };
  const onWindowClosed = () => {
    capture.dispose();
    audio.dispose();
  };
  const onNavigated = () => {
    capture.discard("navigation");
    audio.stop("navigation");
  };
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
    // Same for audio: turning it ON needs a native confirmation the renderer cannot fake or skip.
    if (patch.audioInputEnabled === true && !settings.audioInputEnabled) {
      if (!(await confirmAudioConsent())) throw new Error("Audio input was not turned on.");
    }
    const next = settingsStore.update(patch);
    if (!next.screenshotInputEnabled) capture.discard("disabled");
    if (!next.audioInputEnabled) audio.stop("disabled");
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

  handle("auth:get-state", async () => ({ user: await trackAuth(api.initialize()) }));
  handle("auth:login", async (_event, email, password) => {
    if (typeof email !== "string" || typeof password !== "string") {
      throw new Error("Enter an email and password.");
    }
    return trackAuth(api.login(email, password));
  });
  handle("auth:signup", async (_event, email, password) => {
    if (typeof email !== "string" || typeof password !== "string") {
      throw new Error("Enter an email and password.");
    }
    return trackAuth(api.signup(email, password));
  });
  handle("auth:logout", async () => {
    handoff.onSignedOut();
    capture.discard("signed_out");
    audio.stop("signed_out");
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
    let inputSource: "screenshot" | "audio" | undefined;
    if (extractionId !== undefined) {
      if (!isUuid(extractionId)) throw new Error("The text is no longer available. Capture it again.");
      if (capture.consumeExtraction(extractionId, sessionId)) inputSource = "screenshot";
      else if (audio.consumeUtterance(extractionId, sessionId)) inputSource = "audio";
      else throw new Error("The text is no longer available. Capture it again.");
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
          inputSource,
        )
        .catch((error: unknown) => {
          // The question never started (refused or failed early): the user may simply try again.
          if (inputSource && !questionStarted && extractionId !== undefined) {
            if (inputSource === "screenshot") capture.restoreExtraction(extractionId, sessionId);
            else audio.restoreUtterance(extractionId, sessionId);
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
  handle("handoff:get-state", () => handoff.getState());
  handle("handoff:dismiss", () => handoff.dismiss());
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
  // ---- Live audio channels (Phase 12). Named and validated; chunks are one-way and ignored unless
  // listening was started here. ----
  handle("audio:start", async (_event, source, sessionId) => {
    if (!isAudioSource(source) || !validateId(sessionId)) {
      throw new Error("Choose an audio source and a session.");
    }
    await audio.start(source, sessionId);
  }, true);
  handle("audio:stop", () => audio.stop("user"), true);
  const onAudioChunk = (event: { senderFrame?: { url?: string } | null }, data: unknown): void => {
    const url = event.senderFrame?.url ?? "";
    if (!isTrustedRenderer(url, devServerUrl, rendererHtmlPath)) return; // untrusted: ignored
    audio.pushChunk(data instanceof ArrayBuffer ? new Uint8Array(data) : data);
  };
  ipcMain.on("audio:chunk", onAudioChunk as never);

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
    deepLinks.detach();
    capture.dispose();
    audio.dispose();
    ipcMain.removeListener("audio:chunk", onAudioChunk as never);
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
