import { RECOVERY_NOTICE_TEXT } from "../../../shared/recovery";
import { useDesktopStore } from "../store";

/**
 * Crash-recovery notice and the "Copy diagnostics" action (M-5, D-70).
 *
 * The notice appears once, after the main process reloaded the window because its renderer stopped.
 * It says plainly what happened and what did NOT happen (nothing was restarted). The diagnostics text
 * is built and copied by the main process; this component never sees it, only how many characters
 * were copied. No stack traces or technical detail appear here.
 */
export function RecoveryBanner() {
  const notice = useDesktopStore((state) => state.recoveryNotice);
  const dismiss = useDesktopStore((state) => state.dismissRecoveryNotice);
  const copy = useDesktopStore((state) => state.copyDiagnostics);
  const busy = useDesktopStore((state) => state.diagnosticsBusy);
  const message = useDesktopStore((state) => state.diagnosticsMessage);
  if (!notice) return null;
  return (
    <section aria-label="Window recovered" className="recovery-banner" role="status">
      <p className="recovery-text">{RECOVERY_NOTICE_TEXT}</p>
      {message && <p className="settings-help">{message}</p>}
      <div className="capture-actions">
        <button className="button button-secondary" disabled={busy} onClick={() => void copy()} type="button">
          {busy ? "Copying…" : "Copy diagnostics"}
        </button>
        <button className="button button-secondary" onClick={dismiss} type="button">
          Dismiss
        </button>
      </div>
    </section>
  );
}

/** Settings, Support: copy a diagnostics report for whoever is helping. */
export function SupportSettings() {
  const copy = useDesktopStore((state) => state.copyDiagnostics);
  const busy = useDesktopStore((state) => state.diagnosticsBusy);
  const message = useDesktopStore((state) => state.diagnosticsMessage);
  return (
    <section aria-labelledby="support-title" className="settings-section">
      <h2 id="support-title">Support</h2>
      <p className="settings-help">
        If the app misbehaves, copy a diagnostics report and send it to whoever is helping you. It lists the
        app version, your system type, whether listening and screenshots are on or off, and recent window
        problems. It never contains your questions, answers, audio, screenshots, passwords or keys, and it
        is not sent anywhere by the app: it only goes to your clipboard when you press the button.
      </p>
      <button className="button button-secondary full-button" disabled={busy} onClick={() => void copy()} type="button">
        {busy ? "Copying…" : "Copy diagnostics"}
      </button>
      {message && (
        <p className="settings-help" role="status">
          {message}
        </p>
      )}
    </section>
  );
}
