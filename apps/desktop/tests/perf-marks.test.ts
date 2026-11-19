/**
 * M-2: performance marks. Pure units for the recorder, plus the audio and capture services driven
 * with a real recorder on a fake clock, to prove timing is captured on success, failure and
 * cancellation and that no content can reach it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AudioService, type AudioPorts } from "../src/main/audio-service";
import { WINDOW_SAMPLES } from "../src/main/audio-vad";
import {
  buildReport,
  MAX_ACTIVE_TRACES,
  MAX_COMPLETED_TRACES,
  percentile,
  PerfRecorder,
  ResourceSampler,
  stats,
  type PerfStage,
} from "../src/main/perf-marks";
import {
  CaptureService,
  COUNTDOWN_SECONDS,
  type CapturePorts,
  type IndicatorState,
} from "../src/main/capture-service";

const A = "11111111-2222-4333-8444-555555555555";
const B = "22222222-2222-4333-8444-555555555555";
const C = "33333333-2222-4333-8444-555555555555";
const SESSION = "44444444-2222-4333-8444-555555555555";
const SECRET = "SECRET-TRANSCRIPT-TEXT-91f3";

let clock = 0;
const recorder = () => new PerfRecorder(() => clock);
const at = (ms: number) => {
  clock = ms;
};

beforeEach(() => at(1000));

describe("PerfRecorder: marks and durations", () => {
  it("creates marks with a monotonic clock and computes a duration between stages", () => {
    const r = recorder();
    expect(r.begin("audio", A)).toBe(true);
    r.mark(A, "segment_created");
    at(1250);
    r.mark(A, "request_start");
    at(1900);
    r.mark(A, "response");
    expect(r.duration(A, "segment_created", "request_start")).toBe(250);
    expect(r.duration(A, "request_start", "response")).toBe(650);
    expect(r.duration(A, "segment_created", "response")).toBe(900);
  });

  it("keeps the first mark of a stage (a repeat does not move it)", () => {
    const r = recorder();
    r.begin("answer", A);
    r.mark(A, "first_token");
    at(5000);
    r.mark(A, "first_token");
    r.mark(A, "submitted");
    expect(r.duration(A, "first_token", "submitted")).toBe(4000);
  });

  it("returns null, never a guess, when a timestamp is missing or the order is wrong", () => {
    const r = recorder();
    r.begin("answer", A);
    r.mark(A, "submitted");
    expect(r.duration(A, "submitted", "first_token")).toBeNull();
    expect(r.duration(B, "submitted", "done")).toBeNull();
    at(500); // the clock went backwards: refuse a negative duration
    r.mark(A, "done");
    expect(r.duration(A, "submitted", "done")).toBeNull();
  });

  it("leaves a trace with a missing stage out of that metric only", () => {
    const r = recorder();
    r.begin("answer", A);
    r.mark(A, "submitted");
    at(1100);
    r.mark(A, "first_event");
    r.mark(A, "done"); // no first_token (e.g. an exact-text answer with no delta before done)
    r.end(A, "ok");
    const summary = r.summary();
    expect(summary.metrics["answer.submit_to_first_event_ms"]?.count).toBe(1);
    expect(summary.metrics["answer.submit_to_first_token_ms"]).toBeUndefined();
  });
});

describe("PerfRecorder: duplicate ids", () => {
  it("refuses a duplicate active id and counts it without disturbing the first trace", () => {
    const r = recorder();
    expect(r.begin("audio", A)).toBe(true);
    r.mark(A, "segment_created");
    expect(r.begin("audio", A)).toBe(false);
    expect(r.counters.duplicateIds).toBe(1);
    at(1100);
    r.mark(A, "request_start");
    expect(r.duration(A, "segment_created", "request_start")).toBe(100);
  });

  it("refuses an id that already completed", () => {
    const r = recorder();
    r.begin("audio", A);
    r.end(A, "ok");
    expect(r.begin("audio", A)).toBe(false);
    expect(r.counters.duplicateIds).toBe(1);
  });

  it("evicts the oldest active trace at the cap instead of growing", () => {
    const r = recorder();
    for (let i = 0; i < MAX_ACTIVE_TRACES + 5; i += 1) {
      r.begin("audio", `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`);
    }
    expect(r.activeCount()).toBe(MAX_ACTIVE_TRACES);
    expect(r.counters.evictedActive).toBe(5);
  });

  it("keeps a bounded number of completed traces", () => {
    const r = recorder();
    for (let i = 0; i < MAX_COMPLETED_TRACES + 10; i += 1) {
      const id = `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
      r.begin("audio", id);
      r.end(id, "ok");
    }
    expect(r.completedCount()).toBe(MAX_COMPLETED_TRACES);
  });
});

describe("PerfRecorder: failed and cancelled operations", () => {
  it("counts failed and cancelled traces but keeps their timings out of the latency stats", () => {
    const r = recorder();
    const ids = [A, B, C];
    const outcomes = ["ok", "error", "cancelled"] as const;
    ids.forEach((id, index) => {
      r.begin("audio", id);
      r.mark(id, "segment_created");
      at(1000 + (index + 1) * 100);
      r.mark(id, "request_start");
      r.mark(id, "response");
      r.end(id, outcomes[index] as "ok" | "error" | "cancelled");
    });
    const summary = r.summary();
    expect(summary.outcomes.audio).toEqual({ ok: 1, error: 1, cancelled: 1 });
    expect(summary.metrics["audio.segment_queue_wait_ms"]?.count).toBe(1);
  });

  it("ending an unknown or already finished trace is a harmless no-op", () => {
    const r = recorder();
    r.end(A, "ok");
    r.begin("audio", B);
    r.end(B, "cancelled");
    r.end(B, "ok");
    expect(r.summary().outcomes.audio).toEqual({ ok: 0, error: 0, cancelled: 1 });
  });
});

describe("PerfRecorder: derived and chained timings", () => {
  it("reports server-reported time and a derived network/client overhead separately", () => {
    const r = recorder();
    r.begin("audio", A);
    r.mark(A, "segment_created");
    r.mark(A, "request_start");
    at(1800);
    r.mark(A, "response");
    r.value(A, "server_ms", 600);
    r.end(A, "ok");
    const samples = r.samples();
    expect(samples["audio.transcribe_request_ms"]).toEqual([800]);
    expect(samples["audio.server_reported_ms"]).toEqual([600]);
    expect(samples["audio.network_and_client_overhead_ms"]).toEqual([200]);
  });

  it("never reports a negative overhead if the server time exceeds the round trip", () => {
    const r = recorder();
    r.begin("screenshot", A);
    r.mark(A, "upload_start");
    at(1100);
    r.mark(A, "extracted");
    r.value(A, "server_ms", 5000);
    r.end(A, "ok");
    expect(r.samples()["screenshot.network_and_client_overhead_ms"]).toEqual([0]);
  });

  it("chains an answer to the capture it came from through an alias", () => {
    const r = recorder();
    r.begin("screenshot", A);
    r.mark(A, "capture_requested");
    at(1400);
    r.mark(A, "captured");
    r.alias(B, A);
    r.end(A, "ok");
    at(2000);
    r.begin("answer", C, B);
    r.mark(C, "submitted");
    at(3500);
    r.mark(C, "done");
    r.end(C, "ok");
    expect(r.samples()["answer.input_to_done_ms"]).toEqual([2500]);
  });
});

describe("PerfRecorder: nothing but timing can be recorded", () => {
  it("rejects stage and value names outside the closed set and non-UUID ids", () => {
    const r = recorder();
    r.begin("audio", A);
    r.mark(A, SECRET as unknown as PerfStage);
    r.value(A, SECRET as never, 5);
    expect(r.begin("audio", SECRET)).toBe(false);
    expect(r.begin(SECRET as never, B)).toBe(false);
    expect(r.counters.droppedMarks).toBe(4);
  });

  it("rejects non-numeric, negative and non-finite values", () => {
    const r = recorder();
    r.begin("audio", A);
    for (const bad of [SECRET, -1, Number.NaN, Number.POSITIVE_INFINITY, {}, null]) {
      r.value(A, "server_ms", bad as never);
    }
    r.end(A, "ok");
    expect(r.samples()["audio.server_reported_ms"]).toBeUndefined();
    expect(r.counters.droppedMarks).toBe(6);
  });

  it("produces a report that contains no content, only timings and machine specs", () => {
    const r = recorder();
    r.begin("answer", A);
    r.mark(A, "submitted");
    r.mark(A, SECRET as unknown as PerfStage);
    at(1200);
    r.mark(A, "done");
    r.end(A, "ok");
    const sampler = new ResourceSampler(() => [
      { type: SECRET.repeat(5), cpu: { percentCPUUsage: 1 }, memory: { workingSetSize: 100 } },
      { type: "Browser", cpu: { percentCPUUsage: 2 }, memory: { workingSetSize: 2048 } },
    ]);
    sampler.sample();
    const body = buildReport(
      r,
      sampler,
      {
        appVersion: "0.1.0",
        electron: "x",
        node: "y",
        platform: "win32",
        arch: "x64",
        osRelease: "10",
        cpuModel: "cpu",
        cpuCount: 8,
        totalMemoryMb: 16000,
        processUptimeAtModuleLoadS: 0.3,
      },
      "2026-11-18T00:00:00Z",
    );
    expect(body).not.toContain(SECRET);
    expect(JSON.parse(body).summary.metrics["answer.submit_to_done_ms"].p50).toBe(200);
    expect(JSON.parse(body).resources.Browser.memory_mb.max).toBe(2);
  });
});

describe("statistics", () => {
  it("computes nearest-rank percentiles, min and max", () => {
    const values = Array.from({ length: 100 }, (_, i) => i + 1);
    expect(stats(values)).toEqual({ count: 100, min: 1, p50: 50, p95: 95, max: 100 });
    expect(percentile([7], 95)).toBe(7);
    expect(stats([])).toBeNull();
  });
});

describe("ResourceSampler", () => {
  it("ignores malformed entries, keeps numbers only, and needs no timer", () => {
    let n = 0;
    const sampler = new ResourceSampler(() => {
      n += 1;
      return [
        { type: "Browser", cpu: { percentCPUUsage: n }, memory: { workingSetSize: 1024 * n } },
        { type: "Tab", cpu: { percentCPUUsage: Number.NaN }, memory: { workingSetSize: 5 } },
      ];
    });
    sampler.sample();
    sampler.sample();
    sampler.sample();
    const summary = sampler.summary();
    expect(summary.Tab).toBeUndefined();
    expect(summary.Browser?.samples).toBe(3);
    expect(summary.Browser?.memory_mb.max).toBe(3);
  });

  it("is sampled when a trace ends, through the recorder hook", () => {
    const r = recorder();
    let samples = 0;
    r.onTraceEnd = () => {
      samples += 1;
    };
    r.begin("audio", A);
    r.end(A, "ok");
    r.end(A, "ok"); // a repeat end is a no-op and does not sample again
    expect(samples).toBe(1);
  });
});

// ------------------------------------------------------------------ service integration

function speech(windows: number): Uint8Array {
  const samples = new Int16Array(windows * WINDOW_SAMPLES);
  for (let i = 0; i < samples.length; i += 1) samples[i] = Math.round(Math.sin(i / 5) * 12_000);
  return new Uint8Array(samples.buffer);
}
const quiet = (windows: number) => new Uint8Array(windows * WINDOW_SAMPLES * 2);

describe("AudioService timing", () => {
  let transcribe: AudioPorts["transcribe"];
  let perfClock: PerfRecorder;

  const make = () =>
    new AudioService({
      isEnabled: () => true,
      isSignedIn: () => true,
      isAcknowledged: () => true,
      windowState: () => ({ visible: true, focused: true, minimized: false }),
      sessionReady: async () => true,
      voiceProviderChosen: async () => true,
      transcribe: (wav, id, signal, onServerMs) => transcribe(wav, id, signal, onServerMs),
      describeError: () => "Transcription failed.",
      fatalReason: () => null,
      emit: () => undefined,
      setIndicator: () => undefined,
      now: () => Date.now(),
      perf: perfClock,
    });

  beforeEach(() => {
    vi.useFakeTimers();
    perfClock = new PerfRecorder(() => Date.now());
    transcribe = async (_wav, _id, _signal, onServerMs) => {
      await new Promise((resolve) => setTimeout(resolve, 300));
      onServerMs?.(200);
      return { text: SECRET, truncated: false, isQuestion: true, confidence: 0.9 };
    };
  });
  afterEach(() => vi.useRealTimers());

  const feed = async (service: AudioService, bytes: Uint8Array) => {
    for (let offset = 0; offset < bytes.length; offset += 3200) {
      service.pushChunk(bytes.subarray(offset, offset + 3200));
      await vi.advanceTimersByTimeAsync(100);
    }
  };

  it("records arming, queue wait, request time and end-to-end time for one phrase", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    await feed(service, speech(40));
    await feed(service, quiet(60));
    await vi.advanceTimersByTimeAsync(1000);
    const samples = perfClock.samples();
    expect(samples["audio.arm_to_listening_ms"]).toHaveLength(1);
    expect(samples["audio.transcribe_request_ms"]?.[0]).toBe(300);
    expect(samples["audio.server_reported_ms"]).toEqual([200]);
    expect(samples["audio.network_and_client_overhead_ms"]).toEqual([100]);
    expect(samples["audio.segment_to_utterance_ms"]?.[0]).toBe(300);
    expect(JSON.stringify(perfClock.summary())).not.toContain(SECRET);
    service.dispose();
  });

  it("marks a failed transcription as an error and an aborted one as cancelled", async () => {
    transcribe = async () => {
      throw new Error("boom");
    };
    const service = make();
    await service.start("microphone", SESSION);
    await feed(service, speech(40));
    await feed(service, quiet(60));
    await vi.advanceTimersByTimeAsync(500);
    expect(perfClock.summary().outcomes.audio?.error).toBe(1);

    // Never answers, but rejects when aborted, as a real fetch does.
    transcribe = (_wav, _id, signal) =>
      new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new Error("aborted")));
      });
    await feed(service, speech(40));
    await feed(service, quiet(60));
    service.stop("window"); // abandons the upload in flight
    await vi.advanceTimersByTimeAsync(10);
    const outcomes = perfClock.summary().outcomes;
    expect(outcomes.audio?.cancelled).toBeGreaterThanOrEqual(1);
    expect(outcomes.audio_arm?.ok).toBe(1);
    expect(perfClock.activeCount()).toBe(0);
    service.dispose();
  });

  it("ends the arming trace as cancelled when listening never starts", async () => {
    const service = make();
    await service.start("microphone", SESSION);
    service.stop("user");
    expect(perfClock.summary().outcomes.audio_arm?.cancelled).toBe(1);
    expect(perfClock.activeCount()).toBe(0);
  });
});

describe("CaptureService timing", () => {
  let perfClock: PerfRecorder;
  let uploadImpl: CapturePorts["upload"];

  const make = () => {
    const indicators: IndicatorState[] = [];
    const service = new CaptureService({
      isEnabled: () => true,
      isSignedIn: () => true,
      isAcknowledged: () => true,
      windowState: () => ({ visible: true, focused: true, minimized: false }),
      listDisplays: () => [{ id: "1", label: "Display 1", primary: true, width: 100, height: 100 }],
      captureDisplay: async () => {
        await new Promise((resolve) => setTimeout(resolve, 120));
        return Buffer.alloc(64, 7);
      },
      makePreview: () => ({ dataUrl: "data:image/png;base64,AAAA", width: 100, height: 100 }),
      sessionReady: async () => true,
      upload: (png, id, signal, onServerMs) => uploadImpl(png, id, signal, onServerMs),
      emit: () => undefined,
      setIndicator: (state) => indicators.push(state),
      now: () => Date.now(),
      perf: perfClock,
    });
    return service;
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-18T10:00:00Z"));
    perfClock = new PerfRecorder(() => Date.now());
    uploadImpl = async (_png, _id, _signal, onServerMs) => {
      await new Promise((resolve) => setTimeout(resolve, 400));
      onServerMs?.(300);
      return { text: SECRET, truncated: false };
    };
  });
  afterEach(() => vi.useRealTimers());

  const capture = async (service: CaptureService): Promise<string> => {
    await service.start("1", SESSION);
    await vi.advanceTimersByTimeAsync(COUNTDOWN_SECONDS * 1000 + 200);
    const held = (service as unknown as { held: { captureId: string } | null }).held;
    if (!held) throw new Error("expected a held capture");
    return held.captureId;
  };

  it("measures acquisition (not the visible countdown) and the extraction round trip", async () => {
    const service = make();
    const id = await capture(service);
    const pending = service.extract(id);
    await vi.advanceTimersByTimeAsync(500);
    await pending;
    const samples = perfClock.samples();
    expect(samples["screenshot.acquisition_ms"]).toEqual([120]);
    expect(samples["screenshot.extract_request_ms"]).toEqual([400]);
    expect(samples["screenshot.server_reported_ms"]).toEqual([300]);
    expect(samples["screenshot.network_and_client_overhead_ms"]).toEqual([100]);
    expect(JSON.stringify(perfClock.summary())).not.toContain(SECRET);
  });

  it("records a failed extraction as an error", async () => {
    uploadImpl = async () => {
      throw new Error("provider down");
    };
    const service = make();
    const id = await capture(service);
    await expect(service.extract(id)).rejects.toThrow();
    expect(perfClock.summary().outcomes.screenshot?.error).toBe(1);
    expect(perfClock.activeCount()).toBe(0);
  });

  it("records a discarded capture as cancelled", async () => {
    const service = make();
    await capture(service);
    service.cancel();
    expect(perfClock.summary().outcomes.screenshot?.cancelled).toBe(1);
    expect(perfClock.activeCount()).toBe(0);
  });
});
