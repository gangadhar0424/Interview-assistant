import { useEffect, useState } from "react";
import { useDesktopStore } from "../store";

/**
 * Screenshot input UI (Phase 11B, D-51). Shown only when the user turned the feature on in Settings.
 * The renderer never captures anything: it asks the main process, which owns the countdown, the
 * capture and the image. Only a small preview thumbnail ever reaches this component.
 */
export function ScreenshotPanel({ ready }: { ready: boolean }) {
  const capture = useDesktopStore((state) => state.capture);
  const notice = useDesktopStore((state) => state.captureNotice);
  const openCapture = useDesktopStore((state) => state.openCapture);
  const startCapture = useDesktopStore((state) => state.startCapture);
  const cancelCapture = useDesktopStore((state) => state.cancelCapture);
  const sendCapture = useDesktopStore((state) => state.sendCapture);
  const setReviewText = useDesktopStore((state) => state.setReviewText);
  const askFromScreenshot = useDesktopStore((state) => state.askFromScreenshot);
  const streaming = useDesktopStore(
    (state) =>
      !!state.liveAnswer &&
      ["sending", "retrieving", "generating", "streaming"].includes(state.liveAnswer.phase),
  );
  const [displayId, setDisplayId] = useState("");

  // Leaving the Assistant page discards anything held in the main process.
  useEffect(() => () => void useDesktopStore.getState().cancelCapture(), []);
  useEffect(() => {
    if (capture.phase === "choosing") {
      setDisplayId((current) =>
        capture.displays.some((display) => display.id === current)
          ? current
          : (capture.displays.find((display) => display.primary)?.id ??
            capture.displays[0]?.id ??
            ""),
      );
    }
  }, [capture]);

  return (
    <section aria-label="Screenshot input" className="capture-panel">
      <div className="capture-heading">
        <span className="eyebrow">SCREENSHOT INPUT</span>
        <span className="capture-hint">
          You choose when to capture. Nothing is sent without your confirmation.
        </span>
      </div>
      {notice && (
        <p className="inline-error capture-notice" role="alert">
          {notice}
        </p>
      )}

      {capture.phase === "idle" && (
        <button
          className="button button-secondary full-button"
          disabled={!ready || streaming}
          onClick={() => void openCapture()}
          type="button"
        >
          Capture screen…
        </button>
      )}

      {capture.phase === "choosing" && (
        <div className="capture-step">
          <p className="settings-help">
            Choose the display to capture. A 3-second countdown starts when you press Start.
          </p>
          {capture.displays.map((display) => (
            <label className="capture-display" key={display.id}>
              <input
                checked={displayId === display.id}
                name="capture-display"
                onChange={() => setDisplayId(display.id)}
                type="radio"
              />
              <span>{display.label}</span>
            </label>
          ))}
          <div className="capture-actions">
            <button
              className="button button-primary"
              disabled={!displayId}
              onClick={() => void startCapture(displayId)}
              type="button"
            >
              Start capture
            </button>
            <button
              className="button button-secondary"
              onClick={() => void cancelCapture()}
              type="button"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {capture.phase === "countdown" && (
        <div aria-live="assertive" className="capture-banner capture-banner-live" role="status">
          <strong>
            Capturing {capture.displayLabel} in {capture.seconds}…
          </strong>
          <span>Arrange the screen now. Press Cancel to stop.</span>
          <button className="button button-stop" onClick={() => void cancelCapture()} type="button">
            Cancel
          </button>
        </div>
      )}

      {capture.phase === "held" && (
        <div className="capture-step">
          <div className="capture-banner">
            <strong>Held in memory only — not sent.</strong>
            <span>
              {capture.width}×{capture.height} PNG · {(capture.bytes / 1024).toFixed(0)} KB. It is
              discarded if you cancel, switch session, hide the window or wait a minute.
            </span>
          </div>
          <img
            alt="Preview of the captured screen"
            className="capture-preview"
            src={capture.previewDataUrl}
          />
          <div className="capture-actions">
            <button className="button button-primary" onClick={() => void sendCapture()} type="button">
              Send for text extraction
            </button>
            <button
              className="button button-secondary"
              onClick={() => void cancelCapture()}
              type="button"
            >
              Discard
            </button>
          </div>
        </div>
      )}

      {capture.phase === "extracting" && (
        <div aria-live="polite" className="capture-banner" role="status">
          <strong>Sending the screenshot to read its text…</strong>
          <span>The image is discarded as soon as this finishes.</span>
          <button className="button button-stop" onClick={() => void cancelCapture()} type="button">
            Cancel
          </button>
        </div>
      )}

      {capture.phase === "review" && (
        <div className="capture-step">
          <div className="capture-banner">
            <strong>Screenshot discarded. Review the extracted text.</strong>
            <span>
              {capture.text
                ? "Edit it if needed. Nothing is answered until you press Ask."
                : "No question was found in the screenshot. You can type one below."}
              {capture.truncated ? " The text was shortened to the question limit." : ""}
            </span>
          </div>
          <label className="field-label" htmlFor="screenshot-text">
            Extracted question (editable)
          </label>
          <textarea
            className="field-control question-input"
            id="screenshot-text"
            maxLength={2000}
            onChange={(event) => setReviewText(event.target.value)}
            rows={4}
            value={capture.text}
          />
          <div className="capture-actions">
            <button
              className="button button-primary"
              disabled={!capture.text.trim() || streaming || !ready}
              onClick={() => void askFromScreenshot()}
              type="button"
            >
              <span>Ask</span>
              <span aria-hidden="true">↗</span>
            </button>
            <button
              className="button button-secondary"
              onClick={() => void cancelCapture()}
              type="button"
            >
              Discard text
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
