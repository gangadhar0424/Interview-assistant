import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  globalShortcut,
  Menu,
  nativeImage,
  screen,
  Tray,
  ipcMain,
  safeStorage,
} from "electron";
import { randomUUID } from "node:crypto";
import { cpus, release, totalmem } from "node:os";
import { join, resolve } from "node:path";
import { DeepLinkInbox, extractLinkFromArgv, PROTOCOL_SCHEME } from "./deep-link";
import type { AudioIndicator } from "./audio-service";
import { captureElectronDisplay, listElectronDisplays, makeElectronPreview } from "./capture-electron";
import type { IndicatorState } from "./capture-service";
import { registerDesktopIpc } from "./ipc";
import { createSessionFiles } from "./session-files";
import { UpdateManager } from "./updater";
import { createLazyUpdateEngine } from "./updater-engine";
import {
  fetchManifestText,
  freeDiskBytesAt,
  readBuiltManifestKey,
  readBuiltUpdateConfig,
  sha512Base64OfFile,
} from "./updater-io";
import { HandoffChallenges } from "./handoff-challenge";
import { PRESENCE_PORT, startPresenceServer } from "./presence-server";
import type { Server } from "node:http";
import { SettingsStore } from "./settings-store";
import { isTrustedRenderer, rendererDevUrl } from "./ipc-security";
import {
  decideDisplayMedia,
  decideMediaPermission,
  IDLE_GATE,
  type AudioGateState,
  type AudioSource,
} from "./media-permissions";
import { applyWindowSettings, closeToTray, WINDOW_SIZE } from "./window-controller";
import { CapturePrivacyManager } from "./capture-privacy";
import { CAPTURE_PRIVACY_EXPLANATION, CAPTURE_PRIVACY_TITLE } from "../shared/capture-privacy";
import { WindowPlacementManager } from "./window-placement";
import {
  ACCEPTABLE_USE_INTRO,
  ACCEPTABLE_USE_NOTE,
  ACCEPTABLE_USE_STATEMENTS,
  ACCEPTABLE_USE_TITLE,
} from "../shared/acknowledgement";
import { buildReport, perf, ResourceSampler } from "./perf-marks";
import { ShortcutManager } from "./shortcuts";
import { diagnostics } from "./diagnostics";
import { RendererRecovery } from "./recovery";

// M-2 performance instrumentation. Marks are always recorded in memory (timing only); a report file
// is printed to stdout, and resources sampled, ONLY when the app is started with INTERVIEW_PERF=1. Nothing
// here is sent anywhere or exposed to the renderer.
const PERF_ENABLED = process.env.INTERVIEW_PERF === "1";
const STARTUP_TRACE = randomUUID();
if (perf.begin("startup", STARTUP_TRACE)) perf.mark(STARTUP_TRACE, "module_loaded");
const MODULE_LOAD_UPTIME_S = process.uptime();
const resourceSampler = new ResourceSampler(() => app.getAppMetrics());

/** Dev-server URL; always undefined in a packaged build (E-01). */
const RENDERER_DEV_URL = rendererDevUrl(process.env, app.isPackaged);

function writePerfReport(): void {
  if (!PERF_ENABLED) return;
  try {
    const cpu = cpus();
    const body = buildReport(
      perf,
      resourceSampler,
      {
        appVersion: app.getVersion(),
        electron: process.versions.electron ?? "unknown",
        node: process.versions.node,
        platform: process.platform,
        arch: process.arch,
        osRelease: release(),
        cpuModel: cpu[0]?.model ?? "unknown",
        cpuCount: cpu.length,
        totalMemoryMb: Math.round(totalmem() / (1024 * 1024)),
        processUptimeAtModuleLoadS: Math.round(MODULE_LOAD_UPTIME_S * 1000) / 1000,
      },
      new Date().toISOString(),
    );
    // Printed, not written: the main process has a standing rule that only the settings and token
    // stores touch the disk. A benchmark run captures this block from stdout (scripts/bench).
    process.stdout.write(`INTERVIEW_PERF_REPORT_BEGIN\n${body}\nINTERVIEW_PERF_REPORT_END\n`);
  } catch {
    // A report that cannot be printed must never affect the app.
  }
}

// Screen-capture privacy (M-15): Electron documents that content protection ALSO applies inside
// Windows remote sessions, where it would hide this window from remote-access tools that capture the
// desktop. This app must not conceal itself from anyone entitled to monitor the device, so that
// behaviour is switched off: in a remote session the window is not excluded. Must run before ready.
app.commandLine.appendSwitch("disable-features", "AllowWindowCaptureExclusionInRemoteSessions");

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let disposeIpc: (() => void) | null = null;
// Stops everything the renderer owned (listening, held screenshots, running answers); see ipc.ts.
let cleanupRenderer: (() => void) | null = null;
let presenceServer: Server | null = null;
// Read by the media-permission handlers below; the live audio service (ipc.ts) replaces `read`.
const audioGate: { read: () => AudioGateState } = { read: () => IDLE_GATE };

// Web -> desktop handoff (D-55): links from the OS land here and are handed to the handoff
// controller (ipc.ts) once it exists. Links are held in memory only and never logged.
const deepLinks = new DeepLinkInbox();
// SEC-08: verifiers for the handoff challenges the loopback beacon hands to the web page (memory only).
const handoffChallenges = new HandoffChallenges();

const settingsStore = new SettingsStore(join(app.getPath("userData"), "settings.json"));

// Secure updates (M-11, D-78). Inert unless this is a packaged, signed build with an https feed and a
// pinned publisher; downloads and installs only ever happen on an explicit user action.
const updateManager = new UpdateManager({
  engine: createLazyUpdateEngine(),
  currentVersion: app.getVersion(),
  isPackaged: app.isPackaged,
  disabledByEnvironment: process.env.INTERVIEW_DISABLE_UPDATE_CHECK === "1",
  readUpdateConfig: () => readBuiltUpdateConfig(process.resourcesPath),
  fetchManifest: (url, timeoutMs) => fetchManifestText(url, timeoutMs, app.getVersion()),
  fetchSignature: (url, timeoutMs) => fetchManifestText(url, timeoutMs, app.getVersion()),
  readManifestKey: () => readBuiltManifestKey(process.resourcesPath),
  hashFile: (path) => sha512Base64OfFile(path),
  freeDiskBytes: async () => freeDiskBytesAt(app.getPath("userData")),
  prepareForInstall: () => undefined, // the IPC handler stops capture first (ipc.ts)
  now: () => Date.now(),
  random: Math.random,
  setTimer: (callback, ms) => {
    const handle = setTimeout(callback, ms);
    handle.unref();
    return { cancel: () => clearTimeout(handle) };
  },
  emit: (state) => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("updates:event", state);
  },
  // The main process never logs (a guarded rule); the outcome is visible to the user as update state.
  log: () => undefined,
});

const TRAY_TOOLTIPS: Record<IndicatorState, string> = {
  idle: "Interview Assistant — visible desktop assistant",
  countdown: "Interview Assistant — a screenshot will be captured shortly (cancel in the app)",
  capturing: "Interview Assistant — capturing the screen…",
  held: "Interview Assistant — holding a screenshot in memory (not sent)",
};

/** Main-process-owned, renderer-independent indication that a capture is happening. */
function setCaptureIndicator(state: IndicatorState): void {
  tray?.setToolTip(TRAY_TOOLTIPS[state]);
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.flashFrame(state === "capturing");
}

const AUDIO_TOOLTIPS: Record<AudioSource, string> = {
  microphone: "Interview Assistant — RECORDING your microphone (nothing is saved)",
  system: "Interview Assistant — RECORDING system audio (nothing is saved)",
};

/** Main-process-owned, renderer-independent indication that audio is being captured. */
function setAudioIndicator(state: AudioIndicator, source: AudioSource | null): void {
  tray?.setToolTip(
    state === "idle" || !source ? TRAY_TOOLTIPS.idle : AUDIO_TOOLTIPS[source],
  );
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.flashFrame(state === "arming");
}

/** Native acceptable-use confirmation (M-1): the same wording the app shows, in a dialog the renderer cannot fake. */
async function confirmAcceptableUse(window: BrowserWindow): Promise<boolean> {
  const result = await dialog.showMessageBox(window, {
    type: "question",
    title: ACCEPTABLE_USE_TITLE,
    message: ACCEPTABLE_USE_INTRO,
    detail: `${ACCEPTABLE_USE_STATEMENTS.map((line, index) => `${index + 1}. ${line}`).join("\n\n")}\n\n${ACCEPTABLE_USE_NOTE}`,
    buttons: ["I understand and agree", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  });
  return result.response === 0;
}

/** Native confirmation before audio input is turned on (a compromised renderer cannot fake it). */
async function confirmAudioConsent(window: BrowserWindow): Promise<boolean> {
  const result = await dialog.showMessageBox(window, {
    type: "warning",
    title: "Turn on audio input?",
    message: "Audio input lets this app listen to your microphone or the audio playing on this PC.",
    detail:
      "Listening starts only when you press Start and stops when you press Stop, hide the window or sign out. " +
      "Short phrases are sent to your Interview Assistant server to be turned into text; the audio is not saved. " +
      "Other people's voices may be captured: you are responsible for following local law and the " +
      "interview platform's rules.",
    buttons: ["Turn on", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  });
  return result.response === 0;
}

/** Native confirmation before screenshot input is turned on (a compromised renderer cannot fake it). */
async function confirmScreenshotConsent(window: BrowserWindow): Promise<boolean> {
  const result = await dialog.showMessageBox(window, {
    type: "warning",
    title: "Turn on screenshot input?",
    message: "Screenshot input captures everything visible on the display you choose.",
    detail:
      "The image is shown to you first, sent to your Interview Assistant server only when you confirm, " +
      "and may be forwarded to an AI provider to read the text. It is not saved and is discarded after " +
      "text extraction. Close anything private before capturing.",
    buttons: ["Turn on", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  });
  return result.response === 0;
}

/** Native confirmation before a handoff switches away from a running capture (a compromised renderer cannot fake it). */
async function confirmHandoffSwitch(window: BrowserWindow): Promise<boolean> {
  const result = await dialog.showMessageBox(window, {
    type: "warning",
    title: "Switch to a new prepared session?",
    message: "The web app asked to open a prepared session while a capture is running here.",
    detail:
      "Switching stops the current screenshot or audio capture and discards anything not yet sent. " +
      "Nothing new starts recording: you will still turn audio on yourself.",
    buttons: ["Switch session", "Keep current capture"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  });
  return result.response === 0;
}

/** Native confirmation before a handoff replaces the signed-in account (no account details shown). */
async function confirmHandoffAccountSwitch(window: BrowserWindow): Promise<boolean> {
  const result = await dialog.showMessageBox(window, {
    type: "warning",
    title: "Switch account?",
    message: "The web app started an interview for a different account than the one signed in here.",
    detail:
      "Continuing signs this app in as the web app's account and ends anything running for the " +
      "current one. Nothing starts recording: you will still turn audio on yourself.",
    buttons: ["Switch account", "Keep current account"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  });
  return result.response === 0;
}

/**
 * Window placement across displays (M-6, D-71). The window opens where the user last left it, on a
 * display that still exists, and is brought back to a valid place after a display change. It never
 * moves during normal use, and nothing here touches always-on-top, capture or audio. Displays are
 * read from Electron's `screen` only after the app is ready.
 */
/**
 * Screen-capture privacy (M-15). Off by default; turned on only by the user's explicit request
 * (see capture-privacy.ts). Re-applied to the window after creation, load, show, layout and display
 * changes so a saved choice survives recreation and recovery. It never hides the tray or taskbar.
 */
const capturePrivacy = new CapturePrivacyManager({
  getWindow: () => mainWindow,
  store: settingsStore,
  platform: process.platform,
  osRelease: release(),
  confirm: async () => {
    const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined;
    const options = {
      type: "warning" as const,
      title: CAPTURE_PRIVACY_TITLE,
      message: `${CAPTURE_PRIVACY_TITLE}?`,
      detail: CAPTURE_PRIVACY_EXPLANATION.join("\n\n"),
      buttons: ["I understand, turn on", "Cancel"],
      defaultId: 1,
      cancelId: 1,
      noLink: true,
    };
    const result = parent ? await dialog.showMessageBox(parent, options) : await dialog.showMessageBox(options);
    return result.response === 0;
  },
});

const placement = new WindowPlacementManager({
  displays: () =>
    screen.getAllDisplays().map((d) => ({ id: d.id, bounds: d.bounds, workArea: d.workArea, scaleFactor: d.scaleFactor })),
  primaryId: () => screen.getPrimaryDisplay().id,
  window: {
    getBounds: () => (mainWindow ? mainWindow.getBounds() : { x: 0, y: 0, width: 0, height: 0 }),
    setBounds: (bounds) => mainWindow?.setBounds(bounds),
    isMinimized: () => !!mainWindow && !mainWindow.isDestroyed() && mainWindow.isMinimized(),
    isVisible: () => !!mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible(),
    isDestroyed: () => !mainWindow || mainWindow.isDestroyed(),
  },
  load: () => settingsStore.windowPlacements(),
  save: (mode, saved) => settingsStore.setWindowPlacement(mode, saved),
  defaultSize: (mode) => WINDOW_SIZE[mode],
  schedule: (ms, fn) => {
    const timer = setTimeout(fn, ms);
    return () => clearTimeout(timer);
  },
});
const onDisplaysChanged = () => {
  placement.scheduleReconcile();
  capturePrivacy.reapply();
};

/**
 * Renderer crash recovery and local diagnostics (M-5, D-70). When the renderer process dies the
 * window's work is stopped first, the page is reloaded once automatically, and if that does not
 * work the user gets a native dialog (Try again / Copy diagnostics / Close). Nothing here can start
 * audio, a screenshot or a session. Diagnostics stay in memory, are never sent anywhere, and reach
 * the clipboard only when the user chooses.
 */
diagnostics.record("app_started");
const recovery = new RendererRecovery({
  reload: () => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.reload();
  },
  cleanup: () => cleanupRenderer?.(),
  isQuitting: () => quitting,
  isWindowAlive: () => !!mainWindow && !mainWindow.isDestroyed(),
  promptFailure: async () => {
    const parent = mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible() ? mainWindow : undefined;
    const options = {
      type: "error" as const,
      title: "Interview Assistant",
      message: "The assistant window stopped working.",
      detail:
        "Listening and screenshots were stopped and nothing was restarted. You are still signed in. " +
        "Choose Try again to reload the window, or Copy diagnostics to share with support (it contains no " +
        "questions, answers, audio, screenshots or passwords).",
      buttons: ["Try again", "Copy diagnostics", "Close"],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
    };
    const result = parent ? await dialog.showMessageBox(parent, options) : await dialog.showMessageBox(options);
    return result.response === 0 ? "retry" : result.response === 1 ? "copy" : "dismiss";
  },
  copyDiagnostics: () => {
    clipboard.writeText(diagnostics.report().text);
    diagnostics.record("diagnostics_copied");
  },
  schedule: (ms, fn) => {
    const timer = setTimeout(fn, ms);
    return () => clearTimeout(timer);
  },
  now: () => Date.now(),
});
diagnostics.provide("app", () => ({
  version: app.getVersion(),
  electron: process.versions.electron ?? "unknown",
  chrome: process.versions.chrome ?? "unknown",
  node: process.versions.node,
  platform: process.platform,
  arch: process.arch,
  osRelease: release(),
  uptimeSeconds: process.uptime(),
}));
diagnostics.provide("recovery", () => recovery.snapshot());
diagnostics.provide("perf", () => perf.summary());

/**
 * Global keyboard shortcuts (M-4, D-69). They are registered only after the app is ready and removed
 * when it quits. The window actions below are ALL a shortcut can do: show, hide, focus, and ask the
 * renderer to move to a panel. There is deliberately no path from here to audio or screenshot
 * capture (tests/shortcut-guards.test.ts checks this).
 */
const shortcutManager = new ShortcutManager({
  registrar: {
    register: (accelerator, callback) => globalShortcut.register(accelerator, callback),
    unregister: (accelerator) => globalShortcut.unregister(accelerator),
  },
  window: {
    isVisible: () => !!mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible(),
    isFocused: () => !!mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused(),
    isMinimized: () => !!mainWindow && !mainWindow.isDestroyed() && mainWindow.isMinimized(),
    restore: () => mainWindow?.restore(),
    show: () => mainWindow?.show(),
    focus: () => mainWindow?.focus(),
    hide: () => mainWindow?.hide(),
    showPanel: (panel) => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("shortcut:event", { panel });
    },
  },
  now: () => Date.now(),
});

function showMainWindow(): void {
  const window = mainWindow;
  if (!window || window.isDestroyed()) return;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

/**
 * Registers `interviewassistant://` for this app. The installer registers it for the packaged app
 * (electron-builder `protocols`); this call is a harmless safety net there. In development
 * (`electron-vite dev`) the OS needs the electron binary plus the app path instead; that path is
 * development-only and is documented and tested manually, not relied on in production.
 */
function registerProtocol(): void {
  if (process.defaultApp && process.argv[1]) {
    app.setAsDefaultProtocolClient(PROTOCOL_SCHEME, process.execPath, [resolve(process.argv[1])]);
  } else {
    app.setAsDefaultProtocolClient(PROTOCOL_SCHEME);
  }
}

/**
 * A development run registers the OS protocol handler against the electron binary in this
 * checkout (see registerProtocol). Without cleanup that registration outlives the dev session and
 * keeps launching the dev build after the packaged app is uninstalled. Only the exact dev
 * registration is removed (same binary and arguments), never a packaged app's.
 */
function unregisterDevProtocol(): void {
  if (!process.defaultApp || !process.argv[1]) return;
  try {
    app.removeAsDefaultProtocolClient(PROTOCOL_SCHEME, process.execPath, [resolve(process.argv[1])]);
  } catch {
    // best effort; a failure here must never block quitting
  }
}

function reportStartupFailure(): void {
  dialog.showErrorBox(
    "Interview Assistant could not start",
    "The desktop app could not initialize. Check its settings and try again.",
  );
  app.quit();
}

function createWindow(): BrowserWindow {
  const settings = settingsStore.current();
  // Where the user last left the window (or a sensible default) on a display that exists now.
  const initial = placement.initialBounds(settings.expanded ? "expanded" : "compact");
  const window = new BrowserWindow({
    ...initial,
    minWidth: 360,
    minHeight: 460,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    resizable: true,
    maximizable: false,
    fullscreenable: false,
    alwaysOnTop: settings.alwaysOnTop,
    opacity: settings.opacity,
    title: "Interview Assistant",
    webPreferences: {
      preload: join(__dirname, "../preload/index.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
    },
  });
  mainWindow = window; // known now, so the capture-privacy re-apply below targets this window
  window.removeMenu();
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, target) => {
    const allowed = RENDERER_DEV_URL;
    const rendererHtmlPath = join(__dirname, "../renderer/index.html");
    if (!isTrustedRenderer(target, allowed, rendererHtmlPath)) event.preventDefault();
  });
  // Every permission is denied EXCEPT audio-only microphone access while the user has pressed Start
  // (Phase 12, D-53). Video, display capture and all other permissions stay denied. The rules are
  // the pure functions in media-permissions.ts, driven by the live audio state.
  const trusted = (url: string): boolean =>
    isTrustedRenderer(url, RENDERER_DEV_URL, join(__dirname, "../renderer/index.html"));
  window.webContents.session.setPermissionRequestHandler((contents, permission, callback, details) => {
    const mediaTypes = "mediaTypes" in details && Array.isArray(details.mediaTypes) ? details.mediaTypes : [];
    callback(
      decideMediaPermission({
        kind: "request",
        permission,
        mediaTypes,
        requestingUrl: details.requestingUrl || contents.getURL(),
        gate: audioGate.read(),
        isTrustedRenderer: trusted,
      }),
    );
  });
  window.webContents.session.setPermissionCheckHandler((contents, permission, requestingOrigin, details) =>
    decideMediaPermission({
      kind: "check",
      permission,
      mediaTypes: details.mediaType ? [details.mediaType] : [],
      requestingUrl: details.requestingUrl || requestingOrigin || contents?.getURL() || "",
      gate: audioGate.read(),
      isTrustedRenderer: trusted,
    }),
  );
  // `getDisplayMedia` only ever gets system AUDIO (never a video source) while a system-audio
  // capture is arming; otherwise it is refused.
  window.webContents.session.setDisplayMediaRequestHandler((request, callback) => {
    callback(decideDisplayMedia(audioGate.read(), request.frame?.url ?? "", trusted));
  });
  // The two events can arrive in either order; the startup trace ends when both have happened.
  let shown = false;
  let loaded = false;
  const finishStartup = () => {
    if (shown && loaded) perf.end(STARTUP_TRACE, "ok");
  };
  window.once("ready-to-show", () => {
    perf.mark(STARTUP_TRACE, "ready_to_show");
    diagnostics.record("window_ready");
    window.show();
    shown = true;
    finishStartup();
  });
  window.webContents.once("did-finish-load", () => {
    perf.mark(STARTUP_TRACE, "did_finish_load");
    loaded = true;
    finishStartup();
  });
  window.on("close", (event) => {
    closeToTray(event, window, quitting);
  });
  const rendererHtmlPath = join(__dirname, "../renderer/index.html");
  const ipcHandle = registerDesktopIpc({
    ipcMain,
    window,
    settings: settingsStore,
    safeStorage,
    secureTokenPath: join(app.getPath("userData"), "refresh-token.bin"),
    rendererHtmlPath,
    devServerUrl: RENDERER_DEV_URL,
    captureHost: {
      listDisplays: listElectronDisplays,
      captureDisplay: captureElectronDisplay,
      makePreview: makeElectronPreview,
      setIndicator: setCaptureIndicator,
    },
    confirmScreenshotConsent: () => confirmScreenshotConsent(window),
    confirmAcceptableUse: () => confirmAcceptableUse(window),
    shortcuts: shortcutManager,
    capturePrivacy,
    recovery,
    placement,
    audioHost: { setIndicator: setAudioIndicator },
    audioGate,
    confirmAudioConsent: () => confirmAudioConsent(window),
    deepLinks,
    handoffChallenges,
    confirmHandoffSwitch: () => confirmHandoffSwitch(window),
    confirmHandoffAccountSwitch: () => confirmHandoffAccountSwitch(window),
    showWindow: showMainWindow,
    sessionFiles: createSessionFiles(window),
    updates: updateManager,
  });
  disposeIpc = ipcHandle;
  cleanupRenderer = ipcHandle.cleanupRenderer;
  // Crash recovery events (M-5). Shutdown is never a crash: the module checks `quitting`.
  diagnostics.record("window_created");
  window.webContents.on("render-process-gone", (_event, details) => recovery.onRendererGone(details));
  window.webContents.on("did-finish-load", () => recovery.onLoadFinished());
  // Re-apply the user's saved screen-capture-privacy choice to this (possibly new or reloaded) window.
  capturePrivacy.reapply();
  window.webContents.on("did-finish-load", () => capturePrivacy.reapply());
  window.on("show", () => capturePrivacy.reapply());
  window.on("restore", () => capturePrivacy.reapply());
  window.webContents.on("did-fail-load", (_event, errorCode, _description, _url, isMainFrame) => {
    if (isMainFrame) recovery.onLoadFailed(errorCode);
  });
  // Placement (M-6): remember where the user puts the window; after a display change or when it is
  // shown again, bring it back to a valid place only if it needs it.
  window.on("move", () => placement.onBoundsEvent());
  window.on("resize", () => placement.onBoundsEvent());
  window.on("hide", () => placement.settle()); // catches a move that produced no move event
  window.on("show", () => placement.scheduleReconcile());
  window.on("restore", () => placement.scheduleReconcile());
  window.webContents.on("unresponsive", () => diagnostics.record("renderer_unresponsive"));
  window.webContents.on("responsive", () => diagnostics.record("renderer_responsive"));
  // Windows logoff or shutdown closes the renderers: that is quitting, not a crash.
  window.on("session-end", () => {
    quitting = true;
  });
  if (RENDERER_DEV_URL) {
    void window.loadURL(RENDERER_DEV_URL).catch(reportStartupFailure);
  } else {
    void window.loadFile(rendererHtmlPath).catch(reportStartupFailure);
  }
  return window;
}

function createTray(window: BrowserWindow): Tray {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32"><rect x="2" y="2" width="28" height="28" rx="9" fill="#67e8f9"/><path d="M9 10h14v3H9zm0 6h14v3H9zm0 6h9v3H9z" fill="#082f49"/></svg>`;
  const image = nativeImage.createFromDataURL(
    `data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`,
  );
  const instance = new Tray(image);
  instance.setToolTip("Interview Assistant — visible desktop assistant");
  instance.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: "Show Assistant",
        click: () => {
          window.show();
          window.focus();
        },
      },
      {
        label: "Reload window",
        click: () => recovery.reloadNow(),
      },
      { type: "separator" },
      {
        label: "Quit Interview Assistant",
        click: () => {
          quitting = true;
          app.quit();
        },
      },
    ]),
  );
  instance.on("double-click", () => {
    window.show();
    window.focus();
  });
  return instance;
}

// One instance only: a second launch (the OS opening a handoff link while the app is already
// running) hands its link to the running instance and exits.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    showMainWindow();
    const link = extractLinkFromArgv(argv);
    if (link) deepLinks.deliver(link);
  });
  // macOS delivers links as an event rather than argv (the MVP targets Windows).
  app.on("open-url", (event, url) => {
    event.preventDefault();
    deepLinks.deliver(url);
  });
  const coldStartLink = extractLinkFromArgv(process.argv);
  if (coldStartLink) deepLinks.deliver(coldStartLink);
  registerProtocol();
  startApp();
}

function startApp(): void {
  app.whenReady().then(() => {
    perf.mark(STARTUP_TRACE, "app_ready");
    if (PERF_ENABLED) {
      perf.onTraceEnd = () => resourceSampler.sample();
      resourceSampler.sample();
      const exitAfter = Number(process.env.INTERVIEW_PERF_EXIT_MS);
      if (Number.isFinite(exitAfter) && exitAfter > 0) setTimeout(() => app.quit(), exitAfter);
    }
    const settings = settingsStore.load();
    void startPresenceServer(PRESENCE_PORT, handoffChallenges).then((server) => {
      presenceServer = server;
    });
    mainWindow = createWindow();
    updateManager.start(); // quiet metadata check after a delay; a no-op in unsigned or development builds
    perf.mark(STARTUP_TRACE, "window_created");
    applyWindowSettings(mainWindow, settings);
    screen.on("display-added", onDisplaysChanged);
    screen.on("display-removed", onDisplaysChanged);
    screen.on("display-metrics-changed", onDisplaysChanged);
    tray = createTray(mainWindow);
    shortcutManager.apply(settingsStore.shortcutConfig());
    app.on("activate", () => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.show();
      else mainWindow = createWindow();
    });
  }).catch(reportStartupFailure);
}

app.on("child-process-gone", (_event, details) => {
  diagnostics.record("child_process_gone", {
    reason: details.type === "GPU" ? "gpu" : details.type === "Utility" ? "utility" : "other",
    code: details.exitCode,
  });
});

app.on("before-quit", () => {
  resourceSampler.sample();
  writePerfReport();
  quitting = true;
  disposeIpc?.();
  disposeIpc = null;
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin" && quitting) app.quit();
});

app.on("will-quit", () => {
  screen.removeListener("display-added", onDisplaysChanged);
  screen.removeListener("display-removed", onDisplaysChanged);
  screen.removeListener("display-metrics-changed", onDisplaysChanged);
  placement.dispose();
  updateManager.dispose();
  recovery.dispose();
  shortcutManager.dispose();
  tray?.destroy();
  tray = null;
  presenceServer?.close();
  presenceServer = null;
  unregisterDevProtocol();
});
