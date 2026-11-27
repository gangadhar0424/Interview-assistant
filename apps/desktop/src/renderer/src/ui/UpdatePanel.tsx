import { UPDATE_ERROR_MESSAGES, type UpdateDisabledReason } from "../../../shared/updates";
import { useDesktopStore } from "../store";

/**
 * Update notices (M-11, D-78). Everything shown here comes from the main process's update state;
 * nothing downloads or installs unless the user presses the button, and installing says plainly that
 * the app will restart and stop listening.
 */

const DISABLED_TEXT: Record<UpdateDisabledReason, string> = {
  development: "Updates are off in a development build.",
  not_configured: "This installer has no update source, so updates are off. Install a newer version from your administrator.",
  insecure_feed: "Updates are off because this installer's update source is not secure.",
  unsigned_build: "This is an unsigned test build. It cannot update itself; install new versions manually.",
  disabled_by_environment: "Update checks are turned off on this computer.",
};

/** A notice above the page when an update needs the user's attention. Hidden otherwise. */
export function UpdateBanner() {
  const update = useDesktopStore((state) => state.update);
  const download = useDesktopStore((state) => state.downloadUpdate);
  const install = useDesktopStore((state) => state.installUpdate);
  const check = useDesktopStore((state) => state.checkForUpdate);

  switch (update.status) {
    case "available":
      return (
        <div className="info-banner" data-testid="update-available" role="status">
          <p>Version {update.version} is available.</p>
          <button className="link-button" onClick={() => void download()} type="button">
            Download update
          </button>
        </div>
      );
    case "downloading":
      return (
        <div className="info-banner" data-testid="update-downloading" role="status">
          <p>Downloading version {update.version}… {update.percent}%</p>
        </div>
      );
    case "downloaded":
      return (
        <div className="info-banner" data-testid="update-downloaded" role="status">
          <p>
            Version {update.version} is ready. Installing restarts the app and stops any listening; nothing
            starts again by itself.
          </p>
          <button className="link-button" onClick={() => void install()} type="button">
            Restart and install
          </button>
        </div>
      );
    case "installing":
      return (
        <div className="info-banner" data-testid="update-installing" role="status">
          <p>Installing version {update.version}. The app will restart.</p>
        </div>
      );
    case "error":
      // A failed background check stays quiet; the user sees errors from actions they started.
      return update.retryable ? (
        <div className="error-banner" data-testid="update-error" role="alert">
          <p>{UPDATE_ERROR_MESSAGES[update.code]}</p>
          <button className="link-button" onClick={() => void check()} type="button">
            Check again
          </button>
        </div>
      ) : (
        <div className="error-banner" data-testid="update-error" role="alert">
          <p>{UPDATE_ERROR_MESSAGES[update.code]}</p>
        </div>
      );
    default:
      return null;
  }
}

/** Settings: current update status and a manual "Check for updates". */
export function UpdateSettings() {
  const update = useDesktopStore((state) => state.update);
  const check = useDesktopStore((state) => state.checkForUpdate);
  const busy = update.status === "checking" || update.status === "downloading" || update.status === "installing";

  return (
    <section aria-labelledby="updates-title" className="settings-section">
      <h2 id="updates-title">Updates</h2>
      {update.status === "disabled" ? (
        <p className="settings-help" data-testid="update-disabled">
          {DISABLED_TEXT[update.reason]}
        </p>
      ) : (
        <>
          <p className="settings-help">
            The app checks quietly for a new version about every six hours. A check sends only a request for
            a small version file to the update server. Downloading and installing happen only when you press
            the buttons, and only for installers signed by the publisher built into this app.
          </p>
          <button className="button button-secondary full-button" disabled={busy} onClick={() => void check()} type="button">
            {update.status === "checking" ? "Checking…" : "Check for updates"}
          </button>
          {update.status === "none" && (
            <p className="settings-help" role="status">
              You have the latest version.
            </p>
          )}
        </>
      )}
    </section>
  );
}
