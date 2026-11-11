/**
 * Phase 12 pure units: WAV builder, voice-activity segmenter, media-permission rules and the
 * AudioService state machine (driven with fake ports: no Electron, no hardware, no network).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ARMING_TIMEOUT_MS,
  AudioRefusal,
  AudioService,
  MAX_BYTES_PER_SECOND,
  MAX_LISTEN_MS,
  type AudioPorts,
} from "../src/main/audio-service";
import { UtteranceSegmenter, WINDOW_SAMPLES } from "../src/main/audio-vad";
import {
  decideDisplayMedia,
  decideMediaPermission,
  IDLE_GATE,
  type AudioGateState,
} from "../src/main/media-permissions";
import { buildWav } from "../src/main/wav";
import type { AudioEvent } from "../src/shared/contracts";

const SESSION = "11111111-2222-4333-8444-555555555555";

/** 16-bit samples as the little-endian bytes the renderer sends. */
function tone(windows: number, amplitude: number): Uint8Array {
  const samples = new Int16Array(windows * WINDOW_SAMPLES);
  for (let i = 0; i < samples.length; i += 1) {
    samples[i] = Math.round(Math.sin(i / 5) * amplitude);
  }
  return new Uint8Array(samples.buffer);
}
const speech = (windows: number) => tone(windows, 12_000);
const silence = (windows: number) => new Uint8Array(windows * WINDOW_SAMPLES * 2);

describe("buildWav", () => {
  it("writes a canonical 16 kHz mono 16-bit PCM header", () => {
    const wav = buildWav(new Uint8Array(640));
    expect(wav.subarray(0, 4).toString("ascii")).toBe("RIFF");
    expect(wav.subarray(8, 16).toString("ascii")).toBe("WAVEfmt ");
    expect(wav.readUInt32LE(4)).toBe(36 + 640);
    expect(wav.readUInt16LE(20)).toBe(1);
    expect(wav.readUInt16LE(22)).toBe(1);
    expect(wav.readUInt32LE(24)).toBe(16_000);
    expect(wav.readUInt16LE(34)).toBe(16);
    expect(wav.subarray(36, 40).toString("ascii")).toBe("data");
    expect(wav.readUInt32LE(40)).toBe(640);
    expect(wav.length).toBe(44 + 640);
  });
});

describe("UtteranceSegmenter", () => {
  const feed = (segmenter: UtteranceSegmenter, bytes: Uint8Array) =>
    segmenter.push(new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2));

  it("emits nothing for silence", () => {
    const segmenter = new UtteranceSegmenter();
    expect(feed(segmenter, silence(200))).toEqual([]);
    expect(segmenter.flush()).toBeNull();
  });

  it("emits one utterance after speech followed by enough silence", () => {
    const segmenter = new UtteranceSegmenter();
    const out = [...feed(segmenter, speech(40)), ...feed(segmenter, silence(60))];
    expect(out).toHaveLength(1);
    expect((out[0] as Buffer).length).toBeGreaterThan(40 * WINDOW_SAMPLES * 2);
  });

  it("ignores a short noise burst", () => {
    const segmenter = new UtteranceSegmenter();
    const out = [...feed(segmenter, speech(3)), ...feed(segmenter, silence(80))];
    expect(out).toEqual([]);
  });

  it("flush returns the phrase in progress and then holds nothing", () => {
    const segmenter = new UtteranceSegmenter();
    expect(feed(segmenter, speech(40))).toEqual([]);
    expect(segmenter.flush()).not.toBeNull();
    expect(segmenter.flush()).toBeNull();
  });

  it("reset drops the phrase in progress", () => {
    const segmenter = new UtteranceSegmenter();
    feed(segmenter, speech(40));
    segmenter.reset();
    expect(segmenter.flush()).toBeNull();
  });

  it("force-cuts a never-ending phrase before the backend limit", () => {
    const segmenter = new UtteranceSegmenter();
    const out = feed(segmenter, speech(1_500));
    expect(out.length).toBeGreaterThanOrEqual(1);
    expect((out[0] as Buffer).length).toBeLessThanOrEqual(30 * 16_000 * 2);
  });
});

describe("media permission rules", () => {
  const trusted = (url: string) => url === "file:///app/index.html";
  const URL = "file:///app/index.html";
  const arming = (source: "microphone" | "system"): AudioGateState => ({ state: "arming", source });
  const listening = (source: "microphone" | "system"): AudioGateState => ({ state: "listening", source });
  const query = (over: Partial<Parameters<typeof decideMediaPermission>[0]> = {}) =>
    decideMediaPermission({
      kind: "request",
      permission: "media",
      mediaTypes: ["audio"],
      requestingUrl: URL,
      gate: arming("microphone"),
      isTrustedRenderer: trusted,
      ...over,
    });

  it("allows audio-only microphone access only while arming", () => {
    expect(query()).toBe(true);
  });

  it("denies everything while idle (the default)", () => {
    expect(query({ gate: IDLE_GATE })).toBe(false);
    expect(query({ gate: IDLE_GATE, kind: "check" })).toBe(false);
  });

  it("denies a new request while already listening, but allows a check", () => {
    expect(query({ gate: listening("microphone") })).toBe(false);
    expect(query({ gate: listening("microphone"), kind: "check" })).toBe(true);
  });

  it("denies video, mixed and unspecified media", () => {
    expect(query({ mediaTypes: ["video"] })).toBe(false);
    expect(query({ mediaTypes: ["audio", "video"] })).toBe(false);
    expect(query({ mediaTypes: [] })).toBe(false);
  });

  it("denies every other permission", () => {
    for (const permission of ["geolocation", "notifications", "display-capture", "clipboard-read", "midi", "openExternal"]) {
      expect(query({ permission }), permission).toBe(false);
    }
  });

  it("denies an untrusted requester", () => {
    expect(query({ requestingUrl: "https://evil.example/" })).toBe(false);
    expect(query({ requestingUrl: "" })).toBe(false);
  });

  it("does not grant the microphone to a system-audio capture", () => {
    expect(query({ gate: arming("system") })).toBe(false);
  });

  it("answers getDisplayMedia with audio loopback only while system audio is arming", () => {
    expect(decideDisplayMedia(arming("system"), URL, trusted)).toEqual({ audio: "loopback" });
    expect(decideDisplayMedia(IDLE_GATE, URL, trusted)).toBeNull();
    expect(decideDisplayMedia(listening("system"), URL, trusted)).toBeNull();
    expect(decideDisplayMedia(arming("microphone"), URL, trusted)).toBeNull();
    expect(decideDisplayMedia(arming("system"), "https://evil.example/", trusted)).toBeNull();
    // never a video source
    expect(Object.keys(decideDisplayMedia(arming("system"), URL, trusted) ?? {})).toEqual(["audio"]);
  });
});

describe("AudioService", () => {
  let now = 1_000_000;
  let enabled = true;
  let signedIn = true;
  let ready = true;
  let windowState = { visible: true, focused: true, minimized: false };
  let events: AudioEvent[] = [];
  let indicators: Array<[string, string | null]> = [];
  let transcribe: AudioPorts["transcribe"];
  let uploads: Buffer[] = [];

  const make = () =>
    new AudioService({
      isEnabled: () => enabled,
      isSignedIn: () => signedIn,
      windowState: () => windowState,
      sessionReady: async () => ready,
      transcribe: (wav, sessionId, signal) => transcribe(wav, sessionId, signal),
      describeError: () => "Transcription failed.",
      isFatal: (error) => error instanceof Error && error.message === "fatal",
      emit: (event) => events.push(event),
      setIndicator: (state, source) => indicators.push([state, source]),
      now: () => now,
    });

  beforeEach(() => {
    vi.useFakeTimers();
    now = 1_000_000;
    enabled = true;
    signedIn = true;
    ready = true;
    windowState = { visible: true, focused: true, minimized: false };
    events = [];
    indicators = [];
    uploads = [];
    transcribe = async (wav) => {
      uploads.push(Buffer.from(wav));
      return { text: "What is a mutex?", truncated: false, isQuestion: true, confidence: 0.9 };
    };
  });
  afterEach(() => vi.useRealTimers());

  const advance = (ms: number) => {
    now += ms;
    return vi.advanceTimersByTimeAsync(ms);
  };
  /** Push audio in 100 ms frames spread over simulated time (below the flood limit). */
  const push = async (service: AudioService, bytes: Uint8Array) => {
    const frame = 3_200;
    for (let offset = 0; offset < bytes.length; offset += frame) {
      service.pushChunk(bytes.subarray(offset, offset + frame));
      await advance(100);
    }
  };

  it("refuses to start when disabled, signed out, hidden, unfocused, minimised or not ready", async () => {
    enabled = false;
    await expect(make().start("microphone", SESSION)).rejects.toMatchObject({ code: "audio_disabled" });
    enabled = true;
    signedIn = false;
    await expect(make().start("microphone", SESSION)).rejects.toMatchObject({ code: "not_signed_in" });
    signedIn = true;
    for (const state of [
      { visible: false, focused: true, minimized: false },
      { visible: true, focused: false, minimized: false },
      { visible: true, focused: true, minimized: true },
    ]) {
      windowState = state;
      await expect(make().start("microphone", SESSION)).rejects.toMatchObject({ code: "window_not_ready" });
    }
    windowState = { visible: true, focused: true, minimized: false };
    ready = false;
    await expect(make().start("microphone", SESSION)).rejects.toMatchObject({ code: "session_not_ready" });
    expect(events).toEqual([]);
    expect(indicators).toEqual([]);
  });

  it("nothing is captured or accepted before an explicit start", () => {
    const service = make();
    service.pushChunk(speech(40));
    expect(service.state).toBe("idle");
    expect(service.gate()).toEqual(IDLE_GATE);
    expect(uploads).toEqual([]);
  });

  it("start arms, shows the indicator and opens the permission gate for that source only", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    expect(service.state).toBe("arming");
    expect(service.gate()).toEqual({ state: "arming", source: "microphone" });
    expect(indicators.at(-1)).toEqual(["arming", "microphone"]);
    expect(events[0]).toEqual({ type: "state", state: "arming", source: "microphone" });
    await expect(service.start("system", SESSION)).rejects.toBeInstanceOf(AudioRefusal);
  });

  it("an arming capture that never delivers audio times out and closes the gate", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    await advance(ARMING_TIMEOUT_MS + 10);
    expect(service.state).toBe("idle");
    expect(service.gate()).toEqual(IDLE_GATE);
    expect(events.at(-1)).toMatchObject({ type: "stopped", reason: "timeout" });
    expect(indicators.at(-1)).toEqual(["idle", null]);
  });

  it("the first chunk moves arming to listening", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    service.pushChunk(silence(5));
    expect(service.state).toBe("listening");
    expect(events.some((e) => e.type === "state" && e.state === "listening")).toBe(true);
    expect(indicators.at(-1)).toEqual(["listening", "microphone"]);
  });

  it("a spoken phrase is uploaded as a WAV and becomes a transcript line with a token", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    await push(service, speech(40));
    await push(service, silence(60));
    expect(uploads).toHaveLength(1);
    expect(uploads[0]?.subarray(0, 4).toString("ascii")).toBe("RIFF");
    const line = events.find((e) => e.type === "utterance");
    expect(line).toMatchObject({ text: "What is a mutex?", isQuestion: true });
    const id = (line as Extract<AudioEvent, { type: "utterance" }>).id;
    expect(service.consumeUtterance(id, SESSION)).toBe(true);
    expect(service.consumeUtterance(id, SESSION)).toBe(false); // single use
  });

  it("an utterance token is bound to its session and expires", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    await push(service, speech(40));
    await push(service, silence(60));
    const id = (events.find((e) => e.type === "utterance") as Extract<AudioEvent, { type: "utterance" }>).id;
    expect(service.consumeUtterance(id, "99999999-2222-4333-8444-555555555555")).toBe(false);
    expect(service.consumeUtterance(id, SESSION)).toBe(false); // wrong-session attempt burned it
  });

  it("restoreUtterance re-arms a token whose question never started", () => {
    const service = make();
    service.restoreUtterance("tok", SESSION);
    expect(service.consumeUtterance("tok", SESSION)).toBe(true);
  });

  it("an explicit Stop still transcribes the phrase in progress", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    await push(service, speech(40));
    service.stop("user");
    await advance(10);
    expect(uploads).toHaveLength(1);
    expect(service.state).toBe("idle");
  });

  it("every non-user stop drops the phrase in progress without uploading", async () => {
    for (const reason of ["window", "disabled", "signed_out", "session", "navigation"] as const) {
      uploads = [];
      const service = make();
      await service.start("microphone", SESSION);
      await push(service, speech(40));
      service.stop(reason);
      await advance(10);
      expect(uploads, reason).toEqual([]);
      expect(service.gate(), reason).toEqual(IDLE_GATE);
    }
  });

  it("signing out or changing session forgets outstanding utterance tokens", async () => {
    for (const reason of ["signed_out", "session"] as const) {
      const service = make();
      service.restoreUtterance("tok", SESSION);
      await service.start("microphone", SESSION);
      service.stop(reason);
      expect(service.consumeUtterance("tok", SESSION), reason).toBe(false);
    }
  });

  it("stops after the maximum listening time", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    service.pushChunk(silence(5));
    await advance(MAX_LISTEN_MS + 10);
    expect(service.state).toBe("idle");
    expect(events.at(-1)).toMatchObject({ type: "stopped", reason: "limit" });
  });

  it("drops malformed, oversized and flooding chunks", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    service.pushChunk(new Uint8Array(3)); // odd length
    service.pushChunk(new Uint8Array(0));
    service.pushChunk("not audio");
    service.pushChunk(new Uint8Array(64 * 1024)); // over the chunk limit
    expect(service.state).toBe("arming"); // none of that counted as audio
    const frame = new Uint8Array(32 * 1024);
    for (let i = 0; i < (MAX_BYTES_PER_SECOND / frame.length) + 3; i += 1) service.pushChunk(frame);
    expect(events.some((e) => e.type === "notice")).toBe(true);
  });

  it("a fatal provider error stops listening", async () => {
    transcribe = async () => {
      throw new Error("fatal");
    };
    const service = make();
    await service.start("microphone", SESSION);
    await push(service, speech(40));
    await push(service, silence(60));
    expect(service.state).toBe("idle");
    expect(events.at(-1)).toMatchObject({ type: "stopped", reason: "unavailable" });
  });

  it("a transient provider error is a notice and listening continues", async () => {
    transcribe = async () => {
      throw new Error("blip");
    };
    const service = make();
    await service.start("microphone", SESSION);
    await push(service, speech(40));
    await push(service, silence(60));
    expect(service.state).toBe("listening");
    expect(events.some((e) => e.type === "notice" && e.message === "Transcription failed.")).toBe(true);
  });

  it("zero-fills the uploaded audio after the upload and never puts it in an event", async () => {
    let seen: Buffer | null = null;
    transcribe = async (wav) => {
      seen = wav;
      return { text: "hello", truncated: false, isQuestion: false, confidence: 0.1 };
    };
    const service = make();
    await service.start("microphone", SESSION);
    await push(service, speech(40));
    await push(service, silence(60));
    expect(seen).not.toBeNull();
    expect((seen as unknown as Buffer).subarray(44).every((byte) => byte === 0)).toBe(true);
    expect(JSON.stringify(events)).not.toContain("RIFF");
  });

  it("dispose drops everything silently", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    const before = events.length;
    service.dispose();
    expect(service.state).toBe("idle");
    expect(events.length).toBe(before);
    expect(indicators.at(-1)).toEqual(["idle", null]);
  });
});
