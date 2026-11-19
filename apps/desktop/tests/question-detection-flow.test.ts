/**
 * M-7: what the desktop does with the detector's verdict, in every supported language. The detector is
 * on the backend and only ADVISES; this file pins what the renderer does with `isQuestion`:
 *  - a flagged, complete utterance is answered at once (D-59: the user pressed Listen, which is the
 *    consent), tagged with its transcript line, with the exact heard text;
 *  - an unflagged or cut-off utterance waits for the Ask button;
 *  - nothing is asked until an utterance arrives, and utterances only exist after an explicit Start.
 * These are mocked-bridge tests of renderer logic; the backend verdicts are tested in
 * services/api/tests/test_question_detection_multilingual.py.
 */
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

let desktop: ReturnType<typeof bridge>;
const state = () => useDesktopStore.getState();

function bridge() {
  return {
    app: { updateSettings: vi.fn(async () => ok(settings(true))) },
    sessions: {
      startQuestion: vi.fn(async () => ok(undefined)),
      cancelQuestion: vi.fn(async () => ok(undefined)),
      questions: vi.fn(async () => ok([])),
      list: vi.fn(async () => ok([])),
    },
    audio: { start: vi.fn(async () => ok(undefined)), stop: vi.fn(async () => ok(undefined)), sendChunk: vi.fn() },
  };
}

const utterance = (id: string, text: string, over: Partial<Extract<AudioEvent, { type: "utterance" }>> = {}): AudioEvent => ({
  type: "utterance",
  id,
  text,
  truncated: false,
  isQuestion: true,
  confidence: 0.9,
  ...over,
});

beforeEach(() => {
  desktop = bridge();
  vi.stubGlobal("window", { desktop });
  vi.stubGlobal("crypto", { randomUUID: () => "44444444-2222-4333-8444-555555555555" });
  useDesktopStore.setState({
    selectedSessionId: SESSION,
    sessions: [{ id: SESSION, status: "ready" }] as never,
    settings: settings(true),
    liveAnswer: null,
    error: null,
    audio: { state: "listening", source: "microphone", lines: [], notice: null, voiceChoices: null, voiceSaving: false },
  });
});

const QUESTIONS: Array<[string, string]> = [
  ["English", "tell me about a time you had a conflict with a coworker"],
  ["Hindi (Devanagari)", "आप अपने बारे में कुछ बताइए"],
  ["Telugu (script)", "మీ గురించి కొంచెం చెప్పండి"],
  ["Hindi and English mixed", "आप Python में decorators कैसे use करते हैं"],
  ["Telugu and English mixed", "మీరు microservices architecture ఎలా design చేస్తారు"],
  ["romanised Hindi", "aap apne baare mein bataiye"],
  ["romanised Telugu", "mee gurinchi cheppandi"],
];

describe("a flagged question is answered at once, with the exact heard text, in every language", () => {
  it.each(QUESTIONS)("%s", async (_language, text) => {
    state().handleAudioEvent(utterance("line-1", text));
    await vi.waitFor(() => expect(desktop.sessions.startQuestion).toHaveBeenCalledTimes(1));
    expect(desktop.sessions.startQuestion).toHaveBeenCalledWith(expect.any(String), SESSION, text, "line-1");
    expect(state().audio.lines[0]).toMatchObject({ id: "line-1", text, isQuestion: true, asked: true });
  });
});

describe("an unflagged or incomplete line waits for the Ask button, in every language", () => {
  const STATEMENTS: Array<[string, string]> = [
    ["English statement", "i have been working as a backend engineer for six years"],
    ["Hindi statement", "मैं पिछले छह साल से बैकएंड इंजीनियर के रूप में काम कर रहा हूँ"],
    ["Telugu statement", "నేను గత ఆరు సంవత్సరాలుగా బ్యాకెండ్ ఇంజనీర్‌గా పని చేస్తున్నాను"],
    ["Hindi procedural request", "कृपया अपनी स्क्रीन शेयर कीजिए ताकि हम कोड देख सकें"],
    ["Telugu small talk", "శుభోదయం ఈ రోజు మీరు ఎలా ఉన్నారు"],
  ];
  it.each(STATEMENTS)("%s is shown but not asked", (_name, text) => {
    state().handleAudioEvent(utterance("line-2", text, { isQuestion: false, confidence: 0 }));
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
    expect(state().audio.lines[0]).toMatchObject({ text, isQuestion: false, asked: false });
  });

  it("a question cut off by the length limit is not asked automatically", () => {
    state().handleAudioEvent(utterance("line-3", "మీరు ఎక్కడ చదువుకున్నారు", { truncated: true }));
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
  });

  it("the Ask button asks a line the detector did not flag, with the exact text", async () => {
    const text = "మీ గత ప్రాజెక్ట్ గురించి కాస్త చెప్పగలరా";
    state().handleAudioEvent(utterance("line-4", text, { isQuestion: false }));
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
    await state().askFromAudio("line-4");
    expect(desktop.sessions.startQuestion).toHaveBeenCalledWith(expect.any(String), SESSION, text, "line-4");
  });
});

describe("the verdict never starts listening and is never asked without a ready session or a free answer slot", () => {
  it("does not ask when the session is not ready", () => {
    useDesktopStore.setState({ sessions: [{ id: SESSION, status: "draft" }] as never });
    state().handleAudioEvent(utterance("line-5", "आप अपने बारे में कुछ बताइए"));
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
  });

  it("does not ask while another answer is streaming", () => {
    useDesktopStore.setState({ liveAnswer: { phase: "streaming" } as never });
    state().handleAudioEvent(utterance("line-6", "మీ గురించి కొంచెం చెప్పండి"));
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
    expect(state().audio.lines[0]).toMatchObject({ asked: false });
  });

  it("an utterance event never calls audio start, stop or the consent dialog", () => {
    state().handleAudioEvent(utterance("line-7", "आप अपने बारे में कुछ बताइए"));
    expect(desktop.audio.start).not.toHaveBeenCalled();
    expect(desktop.audio.stop).not.toHaveBeenCalled();
    expect(desktop.app.updateSettings).not.toHaveBeenCalled();
  });

  it("with nothing heard, nothing is asked", async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
    expect(state().audio.lines).toEqual([]);
  });
});
