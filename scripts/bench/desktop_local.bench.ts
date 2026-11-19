/**
 * Desktop LOCAL processing benchmark (no network, no provider, no Electron). Synthetic audio only.
 *
 * Measures our own main-process code: the voice-activity segmenter, WAV encoding, the perf recorder's
 * own overhead, and the AudioService path from "segment created" to "utterance emitted" with a
 * zero-latency fake transcriber (so the number is pure local overhead).
 *
 * Run from apps/desktop:
 *   npx vitest run --config vitest.bench.config.ts
 * Set BENCH_OUT to a path to also write the JSON result.
 */
import { writeFileSync } from "node:fs";
import { AudioService } from "../../apps/desktop/src/main/audio-service";
import { UtteranceSegmenter, WINDOW_SAMPLES } from "../../apps/desktop/src/main/audio-vad";
import { PerfRecorder, stats } from "../../apps/desktop/src/main/perf-marks";
import { buildWav } from "../../apps/desktop/src/main/wav";

const SESSION = "44444444-2222-4333-8444-555555555555";
const SAMPLES = Number(process.env.BENCH_SAMPLES ?? 200);

/** Deterministic speech-like PCM16: tone bursts separated by silence. */
function pcm(seconds: number): Int16Array {
  const total = Math.floor(seconds * 16_000);
  const out = new Int16Array(total);
  let seed = 7;
  const rand = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 2 ** 32;
  };
  let i = 0;
  while (i < total) {
    const burst = Math.min(total - i, Math.floor((0.6 + rand()) * 16_000));
    const gap = Math.floor((0.8 + rand() * 0.4) * 16_000);
    const freq = 120 + rand() * 200;
    for (let n = 0; n < burst; n += 1) {
      const env = Math.min(1, n / 800, (burst - n) / 800);
      out[i + n] = Math.round(9000 * env * Math.sin((2 * Math.PI * freq * n) / 16_000));
    }
    i += burst + gap;
  }
  return out;
}

const ms = (start: number) => performance.now() - start;
type Row = ReturnType<typeof stats>;
const results: Record<string, Row> = {};

describe("desktop local processing benchmark", () => {
  it("measures segmenter, WAV, recorder overhead and the local audio path", async () => {
    // 1. Segmenter: one 100 ms frame at a time (what the renderer sends), over 30 s of audio.
    const audio = pcm(30);
    const frame = 1_600;
    const perFrame: number[] = [];
    const segmenter = new UtteranceSegmenter();
    let utterances = 0;
    const wholeStart = performance.now();
    for (let offset = 0; offset + frame <= audio.length; offset += frame) {
      const t = performance.now();
      utterances += segmenter.push(audio.subarray(offset, offset + frame)).length;
      perFrame.push(ms(t));
    }
    const wholeMs = ms(wholeStart);
    results["segmenter.push per 100 ms frame (ms)"] = stats(perFrame);
    results["segmenter 30 s of audio, total (ms)"] = stats([wholeMs]);
    expect(utterances).toBeGreaterThan(0);

    // 2. WAV encoding of a 10 s utterance.
    const tenSeconds = Buffer.from(pcm(10).buffer);
    const wavSamples: number[] = [];
    for (let i = 0; i < SAMPLES; i += 1) {
      const t = performance.now();
      buildWav(tenSeconds);
      wavSamples.push(ms(t));
    }
    results["buildWav 10 s (ms)"] = stats(wavSamples);

    // 3. The recorder's own overhead: one full trace (begin, 4 marks, value, end) per sample.
    const recorder = new PerfRecorder();
    const overhead: number[] = [];
    for (let i = 0; i < SAMPLES * 10; i += 1) {
      const id = crypto.randomUUID();
      const t = performance.now();
      recorder.begin("audio", id);
      recorder.mark(id, "segment_created");
      recorder.mark(id, "request_start");
      recorder.mark(id, "response");
      recorder.mark(id, "utterance_emitted");
      recorder.value(id, "server_ms", 12);
      recorder.end(id, "ok");
      overhead.push(ms(t));
    }
    results["PerfRecorder full trace overhead (ms)"] = stats(overhead);

    // 4. AudioService: segment created -> utterance emitted, zero-latency fake transcriber.
    const perf = new PerfRecorder();
    const service = new AudioService({
      isEnabled: () => true,
      isSignedIn: () => true,
      windowState: () => ({ visible: true, focused: true, minimized: false }),
      sessionReady: async () => true,
      voiceProviderChosen: async () => true,
      transcribe: async () => ({ text: "x", truncated: false, isQuestion: false, confidence: 0 }),
      describeError: () => "failed",
      fatalReason: () => null,
      emit: () => undefined,
      setIndicator: () => undefined,
      now: () => Date.now(),
      perf,
    });
    await service.start("microphone", SESSION);
    const utteranceAudio = pcm(2.5);
    const silence = new Int16Array(16_000 * 1.2);
    const bytes = (samples: Int16Array) => new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength);
    for (let i = 0; i < 25; i += 1) {
      for (const part of [utteranceAudio, silence]) {
        const raw = bytes(part);
        for (let offset = 0; offset < raw.length; offset += 3_200) {
          // About 3x real time: under the service's 4x per-second input limit, so nothing is dropped.
          service.pushChunk(raw.subarray(offset, Math.min(raw.length, offset + 3_200)));
          if (offset % 32_000 === 0) await new Promise((resolve) => setTimeout(resolve, 300));
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    service.dispose();
    const local = perf.samples();
    results["AudioService segment -> utterance, zero-latency fake (ms)"] =
      stats(local["audio.segment_to_utterance_ms"] ?? []);
    results["AudioService segment queue wait, zero-latency fake (ms)"] =
      stats(local["audio.segment_queue_wait_ms"] ?? []);

    // Report.
    const lines = Object.entries(results).map(([name, row]) =>
      row
        ? `${name.padEnd(62)} n=${String(row.count).padStart(5)} min=${row.min.toFixed(3)} p50=${row.p50.toFixed(3)} p95=${row.p95.toFixed(3)} max=${row.max.toFixed(3)}`
        : `${name.padEnd(62)} UNMEASURED`,
    );
    console.log(`\nDesktop local processing (ms). Provider and network time are NOT included.\n${lines.join("\n")}`);
    if (process.env.BENCH_OUT) {
      writeFileSync(
        process.env.BENCH_OUT,
        JSON.stringify({ kind: "desktop_local", node: process.version, samples: SAMPLES, unit: "ms", results }, null, 2),
      );
    }
  }, 120_000);
});
