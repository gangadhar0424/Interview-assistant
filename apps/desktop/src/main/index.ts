import { app, BrowserWindow, dialog, Menu, nativeImage, Tray, ipcMain, safeStorage } from "electron";
import { join, resolve } from "node:path";
import { DeepLinkInbox, extractLinkFromArgv, PROTOCOL_SCHEME } from "./deep-link";
import type { AudioIndicator } from "./audio-service";
import { captureElectronDisplay, listElectronDisplays, makeElectronPreview } from "./capture-electron";
import type { IndicatorState } from "./capture-service";
import { registerDesktopIpc } from "./ipc";
import { SettingsStore } from "./settings-store";
import { isTrustedRenderer } from "./ipc-security";
import {
  decideDisplayMedia,
  decideMediaPermission,
  IDLE_GATE,
  type AudioGateState,
  type AudioSource,
} from "./media-permissions";
import { applyWindowSettings, closeToTray, setExpanded } from "./window-controller";

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let disposeIpc: (() => void) | null = null;
// Read by the media-permission handlers below; the live audio service (ipc.ts) replaces `read`.
const audioGate: { read: () => AudioGateState } = { read: () => IDLE_GATE };

// Web -> desktop handoff (D-55): links from the OS land here and are handed to the handoff
// controller (ipc.ts) once it exists. Links are held in memory only and never logged.
const deepLinks = new DeepLinkInbox();

const settingsStore = new SettingsStore(join(app.getPath("userData"), "settings.json"));

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

function reportStartupFailure(): void {
  dialog.showErrorBox(
    "Interview Assistant could not start",
    "The desktop app could not initialize. Check its settings and try again.",
  );
  app.quit();
}

function createWindow(): BrowserWindow {
  const settings = settingsStore.current();
  const size = settings.expanded
    ? { width: 760, height: 740 }
    : { width: 390, height: 520 };
  const window = new BrowserWindow({
    ...size,
    minWidth: 360,
    minHeight: 460,
    width: size.width,
    height: size.height,
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
  window.removeMenu();
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, target) => {
    const allowed = process.env.ELECTRON_RENDERER_URL;
    const rendererHtmlPath = join(__dirname, "../renderer/index.html");
    if (!isTrustedRenderer(target, allowed, rendererHtmlPath)) event.preventDefault();
  });
  // Every permission is denied EXCEPT audio-only microphone access while the user has pressed Start
  // (Phase 12, D-53). Video, display capture and all other permissions stay denied. The rules are
  // the pure functions in media-permissions.ts, driven by the live audio state.
  const trusted = (url: string): boolean =>
    isTrustedRenderer(url, process.env.ELECTRON_RENDERER_URL, join(__dirname, "../renderer/index.html"));
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
  window.once("ready-to-show", () => window.show());
  window.on("close", (event) => {
    closeToTray(event, window, quitting);
  });
  const rendererHtmlPath = join(__dirname, "../renderer/index.html");
  disposeIpc = registerDesktopIpc({
    ipcMain,
    window,
    settings: settingsStore,
    safeStorage,
    secureTokenPath: join(app.getPath("userData"), "refresh-token.bin"),
    rendererHtmlPath,
    devServerUrl: process.env.ELECTRON_RENDERER_URL,
    captureHost: {
      listDisplays: listElectronDisplays,
      captureDisplay: captureElectronDisplay,
      makePreview: makeElectronPreview,
      setIndicator: setCaptureIndicator,
    },
    confirmScreenshotConsent: () => confirmScreenshotConsent(window),
    audioHost: { setIndicator: setAudioIndicator },
    audioGate,
    confirmAudioConsent: () => confirmAudioConsent(window),
    deepLinks,
    confirmHandoffSwitch: () => confirmHandoffSwitch(window),
    confirmHandoffAccountSwitch: () => confirmHandoffAccountSwitch(window),
    showWindow: showMainWindow,
  });
  if (process.env.ELECTRON_RENDERER_URL) {
    void window.loadURL(process.env.ELECTRON_RENDERER_URL).catch(reportStartupFailure);
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
    const settings = settingsStore.load();
    mainWindow = createWindow();
    applyWindowSettings(mainWindow, settings);
    setExpanded(mainWindow, settings.expanded);
    tray = createTray(mainWindow);
    app.on("activate", () => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.show();
      else mainWindow = createWindow();
    });
  }).catch(reportStartupFailure);
}

app.on("before-quit", () => {
  quitting = true;
  disposeIpc?.();
  disposeIpc = null;
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin" && quitting) app.quit();
});

app.on("will-quit", () => {
  tray?.destroy();
  tray = null;
});
