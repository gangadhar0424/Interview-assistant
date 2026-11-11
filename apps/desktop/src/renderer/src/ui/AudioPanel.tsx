import { useEffect } from "react";
import { useDesktopStore } from "../store";

/**
 * Live audio input UI (Phase 12, D-53). Shown only when the user turned the feature on in Settings.
 * Listening starts only on an explicit Start press and shows an always-visible recording banner.
 * Transcript lines are kept in memory only; a question is answered only when the user presses Ask.
 */
export function AudioPanel({ ready }: { ready: boolean }) {
  const audio = useDesktopStore((state) => state.audio);
  const startAudio = useDesktopStore((state) => state.startAudio);
  const stopAudio = useDesktopStore((state) => state.stopAudio);
  const askFromAudio = useDesktopStore((state) => state.askFromAudio);
  const dismissAudioLine = useDesktopStore((state) => state.dismissAudioLine);
  const clearAudioLines = useDesktopStore((state) => state.clearAudioLines);
  const streaming = useDesktopStore(
    (state) =>
      !!state.liveAnswer &&
      ["sending", "retrieving", "generating", "streaming"].includes(state.liveAnswer.phase),
  );

  // Leaving the Assistant page stops listening and drops the on-screen transcript.
  useEffect(
    () => () => {
      void useDesktopStore.getState().stopAudio();
      useDesktopStore.getState().clearAudioLines();
    },
    [],
  );

  const active = audio.state !== "idle";
  const sourceLabel = audio.source === "system" ? "system audio" : "your microphone";

  return (
    <section aria-label="Audio input" className="audio-panel">
      <div className="capture-heading">
        <span className="eyebrow">AUDIO INPUT</span>
        <span className="capture-hint">
          Listening starts only when you press Start. Nothing is saved; nothing is answered without
          your confirmation.
        </span>
      </div>

      {active && (
        <p className="audio-recording" role="status">
          <span aria-hidden="true" className="audio-dot" />
          {audio.state === "arming" ? "Starting…" : `RECORDING — listening to ${sourceLabel}`}
        </p>
      )}
      {audio.notice && (
        <p className="inline-error capture-notice" role="alert">
          {audio.notice}
        </p>
      )}

      {active ? (
        <button
          className="button button-secondary full-button"
          onClick={() => void stopAudio()}
          type="button"
        >
          Stop listening
        </button>
      ) : (
        <div className="audio-start-row">
          <button
            className="button button-secondary full-button"
            disabled={!ready}
            onClick={() => void startAudio("microphone")}
            type="button"
          >
            Listen: microphone
          </button>
          <button
            className="button button-secondary full-button"
            disabled={!ready}
            onClick={() => void startAudio("system")}
            type="button"
          >
            Listen: system audio
          </button>
        </div>
      )}

      {audio.lines.length > 0 && (
        <>
          <ul className="audio-lines" aria-label="Live transcript">
            {audio.lines.map((line) => (
              <li className={line.isQuestion ? "audio-line audio-question" : "audio-line"} key={line.id}>
                <p>
                  {line.isQuestion && <span className="audio-badge">Question?</span>} {line.text}
                  {line.truncated ? " …" : ""}
                </p>
                <div className="audio-actions">
                  <button
                    className="button button-primary"
                    disabled={!ready || streaming || line.asked}
                    onClick={() => void askFromAudio(line.id)}
                    type="button"
                  >
                    {line.asked ? "Asked" : "Ask"}
                  </button>
                  <button
                    className="button button-secondary"
                    onClick={() => dismissAudioLine(line.id)}
                    type="button"
                  >
                    Dismiss
                  </button>
                </div>
              </li>
            ))}
          </ul>
          <button className="button button-secondary full-button" onClick={clearAudioLines} type="button">
            Clear transcript
          </button>
        </>
      )}
    </section>
  );
}
