import { useEffect, useId } from "react";
import {
  ACCEPTABLE_USE_INTRO,
  ACCEPTABLE_USE_NOTE,
  ACCEPTABLE_USE_STATEMENTS,
  ACCEPTABLE_USE_TITLE,
} from "../../../shared/acknowledgement";
import { useDesktopStore } from "../store";

/**
 * The acceptable-use notice (M-1, D-68). Shown inside the audio and screenshot panels until the
 * signed-in user has accepted the current wording. The button only asks the main process to open its
 * own native confirmation: acceptance is recorded there, never from this component, and main refuses
 * to start audio or screenshots without it whatever this component shows.
 */
export function AcceptableUseNotice() {
  const busy = useDesktopStore((state) => state.acknowledgementBusy);
  const notice = useDesktopStore((state) => state.acknowledgementNotice);
  const accept = useDesktopStore((state) => state.acceptAcknowledgement);
  const headingId = useId();

  return (
    <div aria-labelledby={headingId} className="ack-notice" role="region">
      <p className="ack-title" id={headingId}>
        {ACCEPTABLE_USE_TITLE}
      </p>
      <p className="settings-help">{ACCEPTABLE_USE_INTRO}</p>
      <ol className="ack-list">
        {ACCEPTABLE_USE_STATEMENTS.map((statement) => (
          <li key={statement}>{statement}</li>
        ))}
      </ol>
      <p className="settings-help">{ACCEPTABLE_USE_NOTE}</p>
      {notice && (
        <p className="inline-error" role="alert">
          {notice}
        </p>
      )}
      <button
        className="button button-primary full-button"
        disabled={busy}
        onClick={() => void accept()}
        type="button"
      >
        {busy ? "Waiting for your confirmation…" : "Review and accept…"}
      </button>
    </div>
  );
}

/**
 * Loads the acknowledgement state when a panel appears. `true` once accepted, `false` when it is
 * known to be missing, `null` while unknown (controls stay disabled until main has answered).
 */
export function useAcknowledged(): boolean | null {
  const acknowledgement = useDesktopStore((state) => state.acknowledgement);
  const load = useDesktopStore((state) => state.loadAcknowledgement);
  useEffect(() => {
    void load();
  }, [load]);
  return acknowledgement === null ? null : acknowledgement.accepted;
}
