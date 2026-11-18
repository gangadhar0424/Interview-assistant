import { useEffect, useState } from "react";
import { useDesktopStore } from "../store";

/**
 * Live audio input UI (Phase 12, D-53; simplified by D-59). Always shown on the Assistant page. The
 * first Listen press asks for consent once (native dialog, remembered). Listening starts only on an
 * explicit Listen press and shows an always-visible recording banner. Transcript lines are kept in
 * memory only; a detected question is answered at once, anything else waits for the Ask button.
 */
export function AudioPanel({ ready }: { ready: boolean }) {
  const audio = useDesktopStore((state) => state.audio);
  const startAudio = useDesktopStore((state) => state.startAudio);
  const stopAudio = useDesktopStore((state) => state.stopAudio);
  const askFromAudio = useDesktopStore((state) => state.askFromAudio);
  const dismissAudioLine = useDesktopStore((state) => state.dismissAudioLine);
  const clearAudioLines = useDesktopStore((state) => state.clearAudioLines);
  const saveVoiceProvider = useDesktopStore((state) => state.saveVoiceProvider);
  const [pickedVoice, setPickedVoice] = useState("");
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
  const choices = audio.voiceChoices;
  const needsVoice = choices !== null;
  const selectedVoice = choices?.some((c) => c.id === pickedVoice) ? pickedVoice : (choices?.[0]?.id ?? "");
  const sourceLabel = audio.source === "system" ? "system audio" : "your microphone";

  return (
    <section aria-label="Audio input" className="audio-panel">
      <div className="capture-heading">
        <span className="eyebrow">AUDIO INPUT</span>
        <span className="capture-hint">
          Press Listen and ask your question: it is answered as soon as it is heard. Listening
          happens only while this is on; nothing is saved.
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

      {choices && (
        <div className="voice-setup" role="group" aria-label="Voice listening provider">
          <p className="capture-hint">
            This session has no voice listening provider yet. Choose one: your speech is sent to it
            only while you are listening.
          </p>
          {choices.length === 0 ? (
            <p className="inline-error" role="alert">
              No voice listening provider is enabled on the server.
            </p>
          ) : (
            <div className="voice-setup-row">
              <select
                aria-label="Voice listening provider"
                className="field-control"
                disabled={audio.voiceSaving}
                onChange={(event) => setPickedVoice(event.target.value)}
                value={selectedVoice}
              >
                {choices.map((choice) => (
                  <option key={choice.id} value={choice.id}>
                    {choice.label}
                  </option>
                ))}
              </select>
              <button
                className="button button-primary"
                disabled={!selectedVoice || audio.voiceSaving}
                onClick={() => void saveVoiceProvider(selectedVoice)}
                type="button"
              >
                {audio.voiceSaving ? "Saving…" : "Save"}
              </button>
            </div>
          )}
        </div>
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
            disabled={!ready || needsVoice}
            onClick={() => void startAudio("microphone")}
            type="button"
          >
            Listen: microphone
          </button>
          <button
            className="button button-secondary full-button"
            disabled={!ready || needsVoice}
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
