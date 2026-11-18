/** D-59 renderer state: one-time consent on the first Listen press, and an immediate answer. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AudioEvent, DesktopResult, DesktopSettings } from "../src/shared/contracts";

vi.mock("../src/renderer/src/audio/capture", () => ({
  startAudioCapture: vi.fn(async () => ({ stop: vi.fn() })),
}));

import { useDesktopStore } from "../src/renderer/src/store";

const SESSION = "11111111-2222-4333-8444-555555555555";
const ok = <T,>(data: T): DesktopResult<T> => ({ ok: true, data });

const settings = (audioInputEnabled: boolean): DesktopSettings => ({
  backendBaseUrl: "http://127.0.0.1:8000",
  alwaysOnTop: true,
  opacity: 0.96,
  expanded: false,
  screenshotInputEnabled: false,
  audioInputEnabled,
});

function bridge() {
  return {
    app: {
      updateSettings: vi.fn(async (patch: Partial<DesktopSettings>) => ok(settings(patch.audioInputEnabled === true))),
    },
    auth: { logout: vi.fn(async () => ok(undefined)) },
    sessions: {
      createOptions: vi.fn(async () =>
        ok({ resumes: [], documents: [], options: { voice_providers: [{ id: "gemini", label: "Gemini" }] } }),
      ),
      setVoice: vi.fn(async () => ok(undefined)),
      questions: vi.fn(async () => ok([])),
      list: vi.fn(async () => ok([])),
      startQuestion: vi.fn(async () => ok(undefined)),
      cancelQuestion: vi.fn(async () => ok(undefined)),
    },
    audio: { start: vi.fn(async () => ok(undefined)), stop: vi.fn(async () => ok(undefined)), sendChunk: vi.fn() },
  };
}

let desktop: ReturnType<typeof bridge>;
const state = () => useDesktopStore.getState();

const utterance = (over: Partial<Extract<AudioEvent, { type: "utterance" }>> = {}): AudioEvent => ({
  type: "utterance",
  id: "line-1",
  text: "What is a mutex?",
  truncated: false,
  isQuestion: true,
  confidence: 0.9,
  ...over,
});

function setSession(status: string, enabled: boolean) {
  useDesktopStore.setState({
    selectedSessionId: SESSION,
    sessions: [{ id: SESSION, status }] as never,
    settings: settings(enabled),
    liveAnswer: null,
    error: null,
    audio: { state: "idle", source: null, lines: [], notice: null, voiceChoices: null, voiceSaving: false },
  });
}

beforeEach(() => {
  desktop = bridge();
  vi.stubGlobal("window", { desktop });
  vi.stubGlobal("crypto", { randomUUID: () => "44444444-2222-4333-8444-555555555555" });
  setSession("ready", false);
});

describe("first Listen press (one-time consent)", () => {
  it("asks for consent once, remembers it, then starts listening", async () => {
    await state().startAudio("microphone");
    expect(desktop.app.updateSettings).toHaveBeenCalledTimes(1);
    expect(desktop.app.updateSettings).toHaveBeenCalledWith({ audioInputEnabled: true });
    expect(desktop.audio.start).toHaveBeenCalledWith("microphone", SESSION);
    expect(state().settings?.audioInputEnabled).toBe(true);

    await state().stopAudio();
    await state().startAudio("microphone"); // second press: no second prompt
    expect(desktop.app.updateSettings).toHaveBeenCalledTimes(1);
    expect(desktop.audio.start).toHaveBeenCalledTimes(2);
  });

  it("does not ask again when audio was already turned on", async () => {
    setSession("ready", true);
    await state().startAudio("microphone");
    expect(desktop.app.updateSettings).not.toHaveBeenCalled();
    expect(desktop.audio.start).toHaveBeenCalledTimes(1);
  });

  it("does not listen when the user declines the native consent", async () => {
    desktop.app.updateSettings.mockResolvedValueOnce({
      ok: false,
      error: { kind: "unknown", code: "declined", message: "Audio input was not turned on." },
    });
    await state().startAudio("microphone");
    expect(desktop.audio.start).not.toHaveBeenCalled();
    expect(state().audio.state).toBe("idle");
    expect(state().audio.notice).toMatch(/not turned on/i);
    expect(state().error).toBeNull();
  });
});

describe("a heard question is answered immediately", () => {
  it("asks the detected question at once, tagged with its transcript line", async () => {
    state().handleAudioEvent(utterance());
    await vi.waitFor(() => expect(desktop.sessions.startQuestion).toHaveBeenCalledTimes(1));
    expect(desktop.sessions.startQuestion).toHaveBeenCalledWith(
      "44444444-2222-4333-8444-555555555555",
      SESSION,
      "What is a mutex?",
      "line-1",
    );
    expect(state().audio.lines[0]).toMatchObject({ id: "line-1", asked: true });
  });

  it.each([
    ["is not a question", { isQuestion: false }],
    ["was cut off (truncated)", { truncated: true }],
  ])("waits for the Ask button when the line %s", (_name, over) => {
    state().handleAudioEvent(utterance(over));
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
    expect(state().audio.lines[0]).toMatchObject({ asked: false });
  });

  it("waits for the Ask button while another answer is still streaming", () => {
    useDesktopStore.setState({ liveAnswer: { phase: "streaming" } as never });
    state().handleAudioEvent(utterance());
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
    expect(state().audio.lines[0]).toMatchObject({ asked: false });
  });

  it("never asks for a session that is not ready", () => {
    setSession("draft", true);
    state().handleAudioEvent(utterance());
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
  });
});

describe("a session without a voice provider (D-57)", () => {
  const refusal = {
    ok: false as const,
    error: {
      kind: "invalid_input" as const,
      code: "stt_provider_required",
      message: "Choose a voice listening provider for this session before using audio input.",
    },
  };

  it("never shows a recording state; it offers the provider choices instead", async () => {
    setSession("ready", true);
    desktop.audio.start.mockResolvedValueOnce(refusal);
    await state().startAudio("microphone");
    expect(state().audio.state).toBe("idle"); // no RECORDING banner
    expect(state().audio.notice).toMatch(/voice listening provider/i);
    expect(state().audio.voiceChoices).toEqual([{ id: "gemini", label: "Gemini" }]);
  });

  it("saving a provider clears the picker and asks the user to press Listen again", async () => {
    setSession("ready", true);
    desktop.audio.start.mockResolvedValueOnce(refusal);
    await state().startAudio("microphone");
    await state().saveVoiceProvider("gemini");
    expect(desktop.sessions.setVoice).toHaveBeenCalledWith(SESSION, "gemini");
    expect(state().audio.voiceChoices).toBeNull();
    expect(state().audio.voiceSaving).toBe(false);
    expect(state().audio.notice).toMatch(/press listen/i);
    expect(desktop.audio.start).toHaveBeenCalledTimes(1); // saving never starts listening by itself
  });

  it("a failed save keeps the picker and shows the reason", async () => {
    setSession("ready", true);
    desktop.audio.start.mockResolvedValueOnce(refusal);
    await state().startAudio("microphone");
    desktop.sessions.setVoice.mockResolvedValueOnce({
      ok: false,
      error: { kind: "http", code: "invalid_provider_selection", message: "That voice provider is not available." },
    });
    await state().saveVoiceProvider("nope");
    expect(state().audio.voiceChoices).not.toBeNull();
    expect(state().audio.notice).toMatch(/not available/i);
  });

  it("a stop caused by the missing provider (mid-session) also offers the picker", async () => {
    setSession("ready", true);
    state().handleAudioEvent({ type: "stopped", reason: "voice_provider_required", message: "Choose a voice listening provider for this session before using audio input." });
    await vi.waitFor(() => expect(state().audio.voiceChoices).not.toBeNull());
    expect(state().audio.state).toBe("idle");
  });
});
