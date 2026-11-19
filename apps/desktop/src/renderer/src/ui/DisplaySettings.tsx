import { useEffect, useId } from "react";
import { useDesktopStore } from "../store";

/**
 * Display settings (M-6, D-71). Lists the connected displays, shows which one the assistant is on,
 * and can move it to the middle of another. The app remembers where you put it, brings it back to a
 * connected display if its monitor is unplugged, and returns it when the monitor comes back. This
 * only moves the assistant window: it does not capture anything, and the screenshot display chooser
 * is separate.
 */
export function DisplaySettings() {
  const displays = useDesktopStore((s) => s.displays);
  const busy = useDesktopStore((s) => s.displaysBusy);
  const notice = useDesktopStore((s) => s.displaysNotice);
  const load = useDesktopStore((s) => s.loadDisplays);
  const move = useDesktopStore((s) => s.moveToDisplay);
  const helpId = useId();

  useEffect(() => {
    void load();
    // Monitors can be connected while this page is open: refresh when the window is used again.
    const refresh = () => void load();
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [load]);

  return (
    <section aria-labelledby={`${helpId}-title`} className="settings-section">
      <h2 id={`${helpId}-title`}>Displays</h2>
      <p className="settings-help" id={helpId}>
        The assistant opens where you last left it. If that monitor is disconnected it moves to a display
        that is connected, and goes back when the monitor returns, unless you moved it in the meantime.
        Choose a display below to move the assistant there now.
      </p>
      {displays === null ? null : displays.length === 0 ? (
        <p className="settings-help">No display information is available.</p>
      ) : (
        <ul aria-describedby={helpId} className="display-list">
          {displays.map((display) => (
            <li className={`display-row ${display.current ? "display-row-current" : ""}`} key={display.id}>
              <span className="display-label">
                {display.label}
                {display.current ? " (the assistant is here)" : ""}
              </span>
              <button
                aria-label={`Move the assistant to ${display.label}`}
                className="button button-secondary"
                disabled={busy || display.current}
                onClick={() => void move(display.id)}
                type="button"
              >
                Move here
              </button>
            </li>
          ))}
        </ul>
      )}
      {notice && (
        <p className="inline-error" role="alert">
          {notice}
        </p>
      )}
      <button className="text-button" disabled={busy} onClick={() => void load()} type="button">
        Refresh the list
      </button>
    </section>
  );
}
