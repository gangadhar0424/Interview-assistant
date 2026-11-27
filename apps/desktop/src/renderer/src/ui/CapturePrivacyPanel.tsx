import { useEffect, useId } from "react";
import {
  CAPTURE_PRIVACY_EXPLANATION,
  CAPTURE_PRIVACY_INDICATOR,
  CAPTURE_PRIVACY_TITLE,
} from "../../../shared/capture-privacy";
import { useDesktopStore } from "../store";

/**
 * Screen-capture privacy (M-15). The banner is the in-window indicator the security review requires
 * while the setting is on: it is shown on every page and cannot be hidden by the user, so the
 * setting is never silent. It also shows when the request failed, so the window is never described
 * as protected when it is not. The settings section always shows the explanation first.
 */
export function CapturePrivacyBanner() {
  const state = useDesktopStore((s) => s.capturePrivacy);
  const load = useDesktopStore((s) => s.loadCapturePrivacy);

  useEffect(() => {
    void load();
    // Display changes and recovery can change the status: refresh when the window is used again.
    const refresh = () => void load();
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [load]);

  if (!state || !state.enabled) return null;
  const active = state.status === "requested";
  return (
    <section
      aria-label="Screen capture privacy"
      className={`capture-privacy-banner ${active ? "" : "capture-privacy-banner-warning"}`}
      role="status"
    >
      <p className="recovery-text">{active ? CAPTURE_PRIVACY_INDICATOR : state.message}</p>
    </section>
  );
}

export function CapturePrivacySettings() {
  const state = useDesktopStore((s) => s.capturePrivacy);
  const busy = useDesktopStore((s) => s.capturePrivacyBusy);
  const notice = useDesktopStore((s) => s.capturePrivacyNotice);
  const load = useDesktopStore((s) => s.loadCapturePrivacy);
  const set = useDesktopStore((s) => s.setCapturePrivacy);
  const titleId = useId();
  const helpId = useId();

  useEffect(() => {
    void load();
  }, [load]);

  const on = !!state?.enabled && state.status === "requested";
  return (
    <section aria-labelledby={titleId} className="settings-section">
      <h2 id={titleId}>{CAPTURE_PRIVACY_TITLE}</h2>
      <div id={helpId}>
        {CAPTURE_PRIVACY_EXPLANATION.map((paragraph) => (
          <p className="settings-help" key={paragraph}>
            {paragraph}
          </p>
        ))}
      </div>
      {state && (
        <p className="settings-help" role="status">
          {state.message}
        </p>
      )}
      <div className="capture-actions">
        {on ? (
          <button
            aria-describedby={helpId}
            className="button button-secondary"
            disabled={busy}
            onClick={() => void set(false)}
            type="button"
          >
            {busy ? "Working…" : "Turn off"}
          </button>
        ) : (
          <button
            aria-describedby={helpId}
            className="button button-secondary"
            disabled={busy || !state || !state.supported}
            onClick={() => void set(true)}
            type="button"
          >
            {busy ? "Waiting for confirmation…" : "Turn on…"}
          </button>
        )}
      </div>
      {notice && (
        <p className="inline-error" role="alert">
          {notice}
        </p>
      )}
    </section>
  );
}
