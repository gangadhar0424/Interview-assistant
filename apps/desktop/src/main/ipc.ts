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
import { perf } from "./perf-marks";
import { diagnostics } from "./diagnostics";
import type { RendererRecovery } from "./recovery";
import type { WindowPlacementManager } from "./window-placement";
import type { DisplayList } from "../shared/placement";
import type { ShortcutManager } from "./shortcuts";
import type { CopyDiagnosticsResult, RecoveryInfo } from "../shared/recovery";
import {
  DEFAULT_SHORTCUTS,
  validateShortcutConfig,
  type ShortcutState,
} from "../shared/shortcuts";
import { hasErrors, parseDraft, toCreateBody, validateDraft } from "../shared/session-form";
import {
  isAudioSource,
  isDisplayId,
  isProviderId,
  isUuid,
  validateAcknowledgementVersion,
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
  /** Window placement across displays (M-6). Optional: without it the old fixed sizes are used. */
  placement?: Pick<WindowPlacementManager, "setMode" | "listDisplays" | "moveToDisplay">;
  /** Crash recovery (M-5): only the one-time "window was reloaded" notice is read here. */
  recovery?: Pick<RendererRecovery, "takeNotice">;
  /** Global keyboard shortcuts (M-4). Optional: without it the shortcut settings report "off". */
  shortcuts?: Pick<ShortcutManager, "apply" | "status">;
  /** Native (main-process) acceptable-use confirmation (M-1); the renderer cannot fake or skip it. */
  confirmAcceptableUse: () => Promise<boolean>;
  /** Web -> desktop handoff (D-55): where links from the OS arrive, plus its two native pieces. */
  deepLinks: DeepLinkInbox;
  /** Native confirmation before a handoff switches away from a running capture. */
  confirmHandoffSwitch: () => Promise<boolean>;
  /** Native confirmation before a handoff signs another account in over the current one. */
  confirmHandoffAccountSwitch: () => Promise<boolean>;
  /** Restores and focuses the window so a handoff result is visible. */
  showWindow: () => void;
  /**
   * Native pieces of session management (M-9). Optional so an older host keeps working; without
   * it delete and export are refused rather than done unconfirmed.
   */
  sessionFiles?: SessionFiles;
};

/** What the main process needs from the operating system to delete or export a session (M-9). */
export type SessionFiles = {
  /** Native confirmation before a session and its history are deleted. */
  confirmDelete(): Promise<boolean>;
  /**
   * Shows what an export contains and that it writes the user's interview content to disk, then
   * asks where to save it. Returns the chosen path, or null when the user cancelled.
   */
  chooseExportFile(defaultName: string, format: "markdown" | "json"): Promise<string | null>;
  /** Writes the export where the user chose. */
  writeFile(path: string, text: string): Promise<void>;
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

export function registerDesktopIpc(deps: IpcDependencies): (() => void) & { cleanupRenderer(): void } {
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
    confirmAcceptableUse,
    shortcuts,
    recovery,
    placement,
    audioHost,
    audioGate,
    confirmAudioConsent,
    deepLinks,
    confirmHandoffSwitch,
    confirmHandoffAccountSwitch,
    showWindow,
    sessionFiles,
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
    isAcknowledged: () => settingsStore.isAcknowledged(api.getUser()?.id ?? null),
    windowState: () => ({
      visible: !window.isDestroyed() && window.isVisible(),
      focused: !window.isDestroyed() && window.isFocused(),
      minimized: !window.isDestroyed() && window.isMinimized(),
    }),
    sessionReady: async (sessionId) =>
      (await api.listSessions()).some((item) => item.id === sessionId && item.status === "ready"),
    upload: (png, sessionId, signal, onServerMs) =>
      api.extractScreenshot(sessionId, png, signal, onServerMs),
    perf,
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
    isAcknowledged: () => settingsStore.isAcknowledged(api.getUser()?.id ?? null),
    windowState: () => ({
      visible: !window.isDestroyed() && window.isVisible(),
      focused: !window.isDestroyed() && window.isFocused(),
      minimized: !window.isDestroyed() && window.isMinimized(),
    }),
    sessionReady: async (sessionId) =>
      (await api.listSessions()).some((item) => item.id === sessionId && item.status === "ready"),
    voiceProviderChosen: async (sessionId) => (await api.getSessionVoiceProvider(sessionId)) !== null,
    transcribe: async (wav, sessionId, signal, onServerMs) => {
      try {
        return await api.transcribeUtterance(sessionId, wav, signal, onServerMs);
      } catch (error) {
        if (isSessionExpired(error) && !window.isDestroyed()) notifySessionExpired();
        throw error;
      }
    },
    describeError: (error) => asDesktopError(error).message,
    fatalReason: (error) => {
      if (!(error instanceof DesktopApiError)) return null;
      if (error.details.code === "stt_unavailable") return "unavailable";
      if (error.details.code === "stt_provider_required") return "voice_provider_required";
      return null;
    },
    emit: (event) => {
      if (!window.isDestroyed()) window.webContents.send("audio:event", event);
    },
    setIndicator: (state, source) => audioHost.setIndicator(state, source),
    perf,
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
  // The renderer process died (M-5, D-70): everything that belonged to the window stops, including
  // answers still streaming to it. Safe to run more than once (the recovery module calls it too).
  const onRendererGone = () => {
    onWindowClosed();
    for (const stream of streams.values()) stream.controller.abort();
    streams.clear();
  };
  window.webContents.on("render-process-gone", onRendererGone);

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
  // Acceptable-use acknowledgement (M-1, D-68). Acceptance is recorded ONLY here, after a native
  // confirmation in the main process; capture is refused in the services until it exists.
  handle("app:get-acknowledgement", () => settingsStore.acknowledgement(api.getUser()?.id ?? null));
  // Keyboard shortcuts (M-4, D-69): preferences are validated here before they are saved or
  // registered. Shortcuts never start capture (see shortcuts.ts).
  const shortcutState = (): ShortcutState => {
    const config = settingsStore.shortcutConfig();
    const status = shortcuts
      ? shortcuts.status()
      : (Object.fromEntries(
          Object.keys(DEFAULT_SHORTCUTS).map((action) => [
            action,
            { kind: "disabled", message: "Shortcuts are not available in this build." },
          ]),
        ) as ShortcutState["status"]);
    return { config, defaults: { ...DEFAULT_SHORTCUTS }, status };
  };
  // Diagnostics (M-5, D-70): live service state as typed booleans and enums only. No ids, no host
  // names, no content. The report text goes to the clipboard from here and never to the renderer.
  diagnostics.provide("services", () => {
    const user = api.getUser();
    const current = settingsStore.current();
    let backend: "local" | "remote" = "remote";
    try {
      const host = new URL(current.backendBaseUrl).hostname;
      if (host === "localhost" || host === "127.0.0.1" || host === "[::1]" || host === "::1") backend = "local";
    } catch {
      // unparseable: reported as remote
    }
    const status: Record<string, { kind: string }> = shortcuts ? shortcuts.status() : {};
    return {
      signedIn: user !== null,
      audioState: audio.gate().state,
      captureBusy: capture.busy(),
      window: {
        visible: !window.isDestroyed() && window.isVisible(),
        focused: !window.isDestroyed() && window.isFocused(),
        minimized: !window.isDestroyed() && window.isMinimized(),
      },
      settings: {
        audioInputEnabled: current.audioInputEnabled,
        screenshotInputEnabled: current.screenshotInputEnabled,
        alwaysOnTop: current.alwaysOnTop,
        expanded: current.expanded,
      },
      backend,
      acknowledgementAccepted: settingsStore.isAcknowledged(user?.id ?? null),
      shortcuts: Object.fromEntries(Object.entries(status).map(([action, s]) => [action, s.kind])),
    };
  });
  // Display selection (M-6, D-71): the renderer sees descriptions only and can request one action,
  // "move to this display", which main validates against the connected displays.
  handle("app:get-displays", (): DisplayList => ({ displays: placement ? placement.listDisplays() : [] }));
  handle("app:move-to-display", (_event, displayId): DisplayList => {
    if (typeof displayId !== "number" || !Number.isInteger(displayId) || displayId < 0) {
      throw new Error("Choose a display from the list.");
    }
    if (!placement) throw new Error("Moving the window is not available in this build.");
    placement.moveToDisplay(displayId);
    return { displays: placement.listDisplays() };
  });
  handle("app:get-recovery", (): RecoveryInfo => ({ notice: recovery?.takeNotice() ? "recovered" : null }));
  handle("app:copy-diagnostics", (): CopyDiagnosticsResult => {
    const { text, truncated } = diagnostics.report();
    clipboard.writeText(text);
    diagnostics.record("diagnostics_copied");
    return { characters: text.length, truncated };
  });
  handle("app:get-shortcuts", () => shortcutState());
  handle("app:set-shortcuts", (_event, input) => {
    const config = validateShortcutConfig(input);
    settingsStore.setShortcuts(config);
    shortcuts?.apply(config);
    return shortcutState();
  });
  let acknowledging = false;
  handle("app:accept-acknowledgement", async (_event, version) => {
    validateAcknowledgementVersion(version);
    const user = api.getUser();
    if (!user) throw new Error("Sign in before accepting the acceptable-use notice.");
    if (settingsStore.isAcknowledged(user.id)) return settingsStore.acknowledgement(user.id);
    if (acknowledging) throw new Error("The acceptable-use notice is already open.");
    acknowledging = true;
    try {
      if (!(await confirmAcceptableUse())) throw new Error("The acceptable-use notice was not accepted.");
      return settingsStore.acknowledge(user.id, new Date().toISOString());
    } finally {
      acknowledging = false;
    }
  });
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
    if (next.expanded !== settings.expanded) {
      if (placement) placement.setMode(next.expanded);
      else setExpanded(window, next.expanded);
    }
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
    if (placement) placement.setMode(expanded);
    else setExpanded(window, expanded);
    return settings;
  });
  handle("app:minimize", () => window.minimize());
  handle("app:close-to-tray", () => window.hide());
  handle("app:copy-text", (_event, text) => {
    clipboard.writeText(validateCopyText(text));
  });

  handle("auth:get-state", async () => ({ user: await trackAuth(api.initialize()) }));
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
  handle("sessions:stream-start", (event, requestId, sessionId, text, extractionId, detail) => {
    if (!validateId(requestId) || !validateId(sessionId)) {
      throw new Error("The session request is invalid.");
    }
    if (typeof text !== "string" || !text.trim() || text.length > 2000) {
      throw new Error("Enter a question of 1–2000 characters.");
    }
    if (detail !== undefined && detail !== "summary") throw new Error("The answer detail is invalid.");
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
    // Timing only (M-2): the request id is the trace id; the extraction/utterance id (if any)
    // chains this answer to the capture or phrase it came from. No text is ever passed.
    perf.begin("answer", requestId, extractionId);
    perf.mark(requestId, "submitted");
    let sawFirstEvent = false;
    let sawFirstToken = false;
    setImmediate(() => {
      void client
        .streamQuestion(
          sessionId,
          text.trim(),
          controller.signal,
          (qaEvent) => {
            if (!sawFirstEvent) {
              sawFirstEvent = true;
              perf.mark(requestId, "first_event");
            }
            if (qaEvent.type === "retrieval.done") {
              perf.mark(requestId, "retrieval_done");
              perf.value(requestId, "retrieval_ms", qaEvent.ms);
            } else if (qaEvent.type === "answer.delta" && !sawFirstToken) {
              sawFirstToken = true;
              perf.mark(requestId, "first_token");
            } else if (qaEvent.type === "answer.done") {
              perf.mark(requestId, "done");
              perf.end(requestId, "ok");
            } else if (qaEvent.type === "answer.error") {
              perf.end(requestId, "error");
            }
            if (qaEvent.type === "question.created") questionStarted = true;
            sendToRenderer(window, { requestId, event: qaEvent });
          },
          inputSource,
          detail,
        )
        .catch((error: unknown) => {
          // The question never started (refused or failed early): the user may simply try again.
          if (inputSource && !questionStarted && extractionId !== undefined) {
            if (inputSource === "screenshot") capture.restoreExtraction(extractionId, sessionId);
            else audio.restoreUtterance(extractionId, sessionId);
          }
          perf.end(requestId, controller.signal.aborted ? "cancelled" : "error");
          if (controller.signal.aborted) {
            sendToRenderer(window, { requestId, cancelled: true });
            return;
          }
          if (isSessionExpired(error) && !window.isDestroyed()) notifySessionExpired();
          sendToRenderer(window, { requestId, error: asDesktopError(error) });
        })
        .finally(() => {
          perf.end(requestId, "cancelled"); // no-op unless the stream ended without a terminal event
          streams.delete(requestId);
        });
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
  // M-9: a retry of the same create (after a lost response) carries the same Idempotency-Key, so
  // the backend returns the session it already made instead of a second one. Memory only; a
  // changed draft gets a new key.
  let pendingCreate: { fingerprint: string; key: string } | null = null;
  handle("sessions:create", async (_event, input) => {
    const draft = parseDraft(input); // exact keys and types, or a safe refusal
    const issues = validateDraft(draft, null);
    if (hasErrors(issues)) {
      throw new Error(Object.values(issues)[0] ?? "The session details are not valid.");
    }
    // The backend stays authoritative for readiness, ownership, limits and the enum values.
    const body = toCreateBody(draft);
    const fingerprint = JSON.stringify(body);
    const key = pendingCreate?.fingerprint === fingerprint ? pendingCreate.key : randomUUID();
    pendingCreate = { fingerprint, key };
    const created = await api.createSession(body, key);
    pendingCreate = null;
    return created;
  }, true);

  // ---- Session management (M-9). Each channel takes a validated UUID; the backend checks owner. ----
  handle("sessions:rename", async (_event, sessionId, title) => {
    if (!isUuid(sessionId)) throw new Error("Choose a valid session.");
    if (typeof title !== "string" || title.trim().length > 200) {
      throw new Error("Use a name of up to 200 characters.");
    }
    return api.renameSession(sessionId, title.trim());
  }, true);
  handle("sessions:end", async (_event, sessionId) => {
    if (!isUuid(sessionId)) throw new Error("Choose a valid session.");
    // Completing a session ends what was running for it: nothing keeps listening or capturing.
    audio.stop("session");
    capture.discard("session");
    return api.endSession(sessionId);
  }, true);
  handle("sessions:reopen", async (_event, sessionId) => {
    if (!isUuid(sessionId)) throw new Error("Choose a valid session.");
    return api.reopenSession(sessionId); // starts no audio and no capture
  }, true);
  handle("sessions:delete", async (_event, sessionId) => {
    if (!isUuid(sessionId)) throw new Error("Choose a valid session.");
    if (!sessionFiles) throw new Error("Deleting a session is not available.");
    if (!(await sessionFiles.confirmDelete())) return { deleted: false };
    audio.stop("session");
    capture.discard("session");
    await api.deleteSession(sessionId);
    return { deleted: true };
  }, true);
  handle("sessions:export", async (_event, sessionId, format) => {
    if (!isUuid(sessionId) || (format !== "markdown" && format !== "json")) {
      throw new Error("Choose a session and an export format.");
    }
    if (!sessionFiles) throw new Error("Export is not available.");
    const extension = format === "markdown" ? "md" : "json";
    const target = await sessionFiles.chooseExportFile(
      `session-${sessionId.slice(0, 8)}.${extension}`,
      format,
    );
    if (!target) return { saved: false, fileName: null };
    // Fetched only after the user chose a path; nothing is written when the request fails.
    const text = await api.exportSession(sessionId, format);
    await sessionFiles.writeFile(target, text);
    return { saved: true, fileName: target.split(/[\\/]/).pop() ?? null };
  }, true);

  // The user picks a voice provider for a session that has none (created before D-57). Only a UUID
  // and a short provider token are accepted; the backend validates it against its allowlist.
  handle("sessions:set-voice", async (_event, sessionId, provider) => {
    if (!isUuid(sessionId) || !isProviderId(provider)) {
      throw new Error("Choose a session and a voice provider.");
    }
    await api.setSessionVoiceProvider(sessionId, provider);
  }, true);

  handle("sessions:set-reasoning", async (_event, sessionId, provider) => {
    if (!isUuid(sessionId) || !isProviderId(provider)) {
      throw new Error("Choose a session and an answer provider.");
    }
    await api.setSessionReasoningProvider(sessionId, provider);
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

  const dispose = () => {
    diagnostics.provide("services", undefined);
    deepLinks.detach();
    capture.dispose();
    audio.dispose();
    ipcMain.removeListener("audio:chunk", onAudioChunk as never);
    window.removeListener("hide", onWindowHidden);
    window.removeListener("minimize", onWindowHidden);
    window.removeListener("closed", onWindowClosed);
    window.webContents.removeListener("did-start-navigation", onNavigated);
    window.webContents.removeListener("render-process-gone", onRendererGone);
    window.webContents.removeListener("destroyed", onClosed);
    for (const stream of streams.values()) stream.controller.abort();
    streams.clear();
    for (const channel of handlers) ipcMain.removeHandler(channel);
  };
  return Object.assign(dispose, { cleanupRenderer: onRendererGone });
}

export function newRequestId(): string {
  return randomUUID();
}
