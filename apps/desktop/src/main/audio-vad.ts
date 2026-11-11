/**
 * Voice-activity segmentation of a 16 kHz mono PCM16 stream into utterances (Phase 12, D-53).
 *
 * Pure and deterministic (no Electron, no I/O, no clock): the same samples always give the same
 * utterances, so replay tests are exact. It uses a fixed energy threshold over 20 ms windows with a
 * short pre-roll and a hangover; it is a segmentation aid, not a speech recogniser. Audio lives only
 * in the buffers held here and is zero-filled when an utterance is handed over or dropped.
 */

export const WINDOW_SAMPLES = 320; // 20 ms at 16 kHz
export const SPEECH_RMS = 0.02; // about -34 dBFS
export const START_WINDOWS = 2; // 40 ms of speech starts an utterance
export const PRE_ROLL_WINDOWS = 10; // 200 ms kept before the start
export const END_SILENCE_WINDOWS = 35; // 700 ms of silence ends it
export const TRAILING_WINDOWS = 10; // 200 ms of the silence is kept
export const MIN_SPEECH_WINDOWS = 15; // shorter than 300 ms is noise
export const MAX_UTTERANCE_WINDOWS = 1_400; // 28 s: forced cut before the backend's 30 s limit

function rms(windowSamples: Int16Array): number {
  let sum = 0;
  for (const sample of windowSamples) sum += (sample / 32768) ** 2;
  return Math.sqrt(sum / windowSamples.length);
}

export class UtteranceSegmenter {
  private carry = new Int16Array(0); // samples not yet forming a full window
  private preRoll: Int16Array[] = [];
  private active: Int16Array[] = [];
  private speechWindows = 0;
  private voicedRun = 0;
  private silenceRun = 0;
  private inSpeech = false;

  /** Feed PCM16 samples; returns every utterance that completed (each as little-endian bytes). */
  push(samples: Int16Array): Buffer[] {
    const merged = new Int16Array(this.carry.length + samples.length);
    merged.set(this.carry, 0);
    merged.set(samples, this.carry.length);
    this.carry.fill(0);
    const out: Buffer[] = [];
    let offset = 0;
    while (offset + WINDOW_SAMPLES <= merged.length) {
      const done = this.window(merged.slice(offset, offset + WINDOW_SAMPLES));
      if (done) out.push(done);
      offset += WINDOW_SAMPLES;
    }
    this.carry = merged.slice(offset);
    merged.fill(0);
    return out;
  }

  /** The utterance in progress, if it is long enough to be speech (used on an explicit stop). */
  flush(): Buffer | null {
    const utterance = this.inSpeech ? this.finish() : null;
    this.reset();
    return utterance;
  }

  /** Drop everything held, zero-filling it. */
  reset(): void {
    this.carry.fill(0);
    this.carry = new Int16Array(0);
    for (const frame of this.preRoll) frame.fill(0);
    for (const frame of this.active) frame.fill(0);
    this.preRoll = [];
    this.active = [];
    this.speechWindows = 0;
    this.voicedRun = 0;
    this.silenceRun = 0;
    this.inSpeech = false;
  }

  private window(frame: Int16Array): Buffer | null {
    const voiced = rms(frame) >= SPEECH_RMS;
    if (!this.inSpeech) {
      this.preRoll.push(frame);
      if (this.preRoll.length > PRE_ROLL_WINDOWS + START_WINDOWS) {
        this.preRoll.shift()?.fill(0);
      }
      this.voicedRun = voiced ? this.voicedRun + 1 : 0;
      if (this.voicedRun >= START_WINDOWS) {
        this.inSpeech = true;
        this.active = this.preRoll;
        this.preRoll = [];
        this.speechWindows = this.voicedRun;
        this.silenceRun = 0;
      }
      return null;
    }
    this.active.push(frame);
    if (voiced) {
      this.speechWindows += 1;
      this.silenceRun = 0;
    } else {
      this.silenceRun += 1;
    }
    if (this.silenceRun >= END_SILENCE_WINDOWS || this.active.length >= MAX_UTTERANCE_WINDOWS) {
      const utterance = this.finish();
      this.inSpeech = false;
      this.voicedRun = 0;
      return utterance;
    }
    return null;
  }

  /** Hand over the active utterance (or drop it as noise) and wipe the working buffers. */
  private finish(): Buffer | null {
    let frames = this.active;
    this.active = [];
    const speech = this.speechWindows;
    this.speechWindows = 0;
    this.silenceRun = 0;
    if (speech < MIN_SPEECH_WINDOWS) {
      for (const frame of frames) frame.fill(0);
      return null;
    }
    // Trim the long trailing silence, keeping a short tail so words do not end abruptly.
    let trailing = 0;
    while (trailing < frames.length && rms(frames[frames.length - 1 - trailing] as Int16Array) < SPEECH_RMS) {
      trailing += 1;
    }
    if (trailing > TRAILING_WINDOWS) frames = frames.slice(0, frames.length - (trailing - TRAILING_WINDOWS));
    const total = frames.reduce((n, frame) => n + frame.length, 0);
    const bytes = Buffer.alloc(total * 2);
    let at = 0;
    for (const frame of frames) {
      for (const sample of frame) {
        bytes.writeInt16LE(sample, at);
        at += 2;
      }
      frame.fill(0);
    }
    return bytes;
  }
}
