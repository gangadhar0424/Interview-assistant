import { app, BrowserWindow, dialog, Menu, nativeImage, Tray, ipcMain, safeStorage } from "electron";
import { join } from "node:path";
import { registerDesktopIpc } from "./ipc";
import { SettingsStore } from "./settings-store";
import { isTrustedRenderer } from "./ipc-security";
import { applyWindowSettings, closeToTray, setExpanded } from "./window-controller";

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let disposeIpc: (() => void) | null = null;

const settingsStore = new SettingsStore(join(app.getPath("userData"), "settings.json"));

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
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => {
    callback(false);
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
