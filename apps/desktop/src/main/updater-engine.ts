import type { EngineEvent, UpdateEngine } from "./updater";
import { classifyEngineError } from "./updater";

/**
 * Adapter from `electron-updater` to the `UpdateEngine` port (M-11). This is the only module that
 * imports it, and it is loaded lazily so tests and unpackaged runs never touch it.
 *
 * Security configuration (never loosened here):
 *  - autoDownload / autoInstallOnAppQuit are off: only an explicit user action downloads or installs;
 *  - allowDowngrade / allowPrerelease are off;
 *  - the Windows publisher check is left at the library default: the installer's Authenticode
 *    publisher must match the `publisherName` written into the build, and the SHA-512 in the
 *    manifest must match the downloaded file, before "update-downloaded" is emitted.
 * Raw library messages are reduced to a fixed error code and discarded.
 */
export async function createElectronUpdaterEngine(): Promise<UpdateEngine> {
  const { autoUpdater, CancellationToken } = await import("electron-updater");
  let token: InstanceType<typeof CancellationToken> | null = null;
  const listeners = new Set<(event: EngineEvent) => void>();
  const emit = (event: EngineEvent): void => {
    for (const listener of listeners) listener(event);
  };

  autoUpdater.on("update-available", (info) => emit({ type: "update-available", version: String(info?.version ?? "") }));
  autoUpdater.on("update-not-available", () => emit({ type: "update-not-available" }));
  autoUpdater.on("download-progress", (progress) => emit({ type: "download-progress", percent: Number(progress?.percent ?? 0) }));
  autoUpdater.on("update-downloaded", (event) =>
    emit({
      type: "update-downloaded",
      version: String(event?.version ?? ""),
      // Where the library stored the installer: the manager hashes it against the signed manifest.
      ...(typeof event?.downloadedFile === "string" ? { downloadedFile: event.downloadedFile } : {}),
    }),
  );
  autoUpdater.on("error", (error) => emit({ type: "error", code: classifyEngineError(error) }));

  return {
    configure() {
      autoUpdater.autoDownload = false;
      autoUpdater.autoInstallOnAppQuit = false;
      autoUpdater.autoRunAppAfterInstall = true;
      autoUpdater.allowDowngrade = false;
      autoUpdater.allowPrerelease = false;
      autoUpdater.disableWebInstaller = true;
      autoUpdater.logger = null;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    async checkForUpdates() {
      await autoUpdater.checkForUpdates();
    },
    async downloadUpdate() {
      token = new CancellationToken();
      try {
        await autoUpdater.downloadUpdate(token);
      } finally {
        token = null;
      }
    },
    cancelDownload() {
      token?.cancel();
    },
    quitAndInstall() {
      // Silent install, then start the new version. The user already chose "Restart and install".
      autoUpdater.quitAndInstall(true, true);
    },
  };
}

/**
 * An engine that loads `electron-updater` only when the updater is actually started, so an
 * unsigned, unconfigured or development build never imports it.
 */
export function createLazyUpdateEngine(): UpdateEngine {
  let ready: Promise<UpdateEngine> | null = null;
  const listeners = new Set<(event: EngineEvent) => void>();
  const load = (): Promise<UpdateEngine> => (ready ??= createElectronUpdaterEngine());
  let unsubscribe: (() => void) | null = null;
  return {
    configure() {
      void load()
        .then((engine) => {
          engine.configure();
          unsubscribe ??= engine.subscribe((event) => {
            for (const listener of listeners) listener(event);
          });
        })
        .catch(() => {
          for (const listener of listeners) listener({ type: "error", code: "unknown" });
        });
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    async checkForUpdates() {
      await (await load()).checkForUpdates();
    },
    async downloadUpdate() {
      await (await load()).downloadUpdate();
    },
    cancelDownload() {
      void load().then((engine) => engine.cancelDownload());
    },
    quitAndInstall() {
      void load().then((engine) => engine.quitAndInstall());
    },
  };
}
