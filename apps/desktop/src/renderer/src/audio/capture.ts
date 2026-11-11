/**
 * The only renderer module that touches Chromium's media APIs (Phase 12, D-53). It runs only after
 * the user pressed Start AND the main process armed the capture (it grants the permission only
 * then; every other time the request is denied). It captures AUDIO ONLY (never video or pixels),
 * converts to 16 kHz mono PCM16 100 ms frames and hands them to the main process over one-way IPC.
 * Nothing is stored, recorded to a file, or logged here; the main process owns segmentation,
 * upload and wiping.
 */
import type { AudioSourceId } from "../../../shared/contracts";

const TARGET_RATE = 16_000;
const FRAME_SAMPLES = 1_600; // 100 ms

export type ActiveCapture = { stop(): void };

export async function startAudioCapture(
  source: AudioSourceId,
  sendChunk: (chunk: ArrayBuffer) => void,
): Promise<ActiveCapture> {
  const stream =
    source === "microphone"
      ? await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
          video: false,
        })
      : await navigator.mediaDevices.getDisplayMedia({ audio: true, video: false });
  // Defence in depth: a loopback grant must never keep a video track alive.
  for (const track of stream.getVideoTracks()) {
    track.stop();
    stream.removeTrack(track);
  }
  if (stream.getAudioTracks().length === 0) {
    stream.getTracks().forEach((track) => track.stop());
    throw new Error("No audio is available from that source.");
  }

  const context = new AudioContext({ sampleRate: TARGET_RATE });
  const input = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(4096, 1, 1);
  const mute = context.createGain();
  mute.gain.value = 0; // the processor must be connected to run; nothing is played back
  let pending = new Float32Array(0);
  let stopped = false;

  processor.onaudioprocess = (event) => {
    if (stopped) return;
    const next = event.inputBuffer.getChannelData(0);
    const merged = new Float32Array(pending.length + next.length);
    merged.set(pending);
    merged.set(next, pending.length);
    let offset = 0;
    while (merged.length - offset >= FRAME_SAMPLES) {
      const pcm = new Int16Array(FRAME_SAMPLES);
      for (let i = 0; i < FRAME_SAMPLES; i += 1) {
        const sample = Math.max(-1, Math.min(1, merged[offset + i] as number));
        pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      }
      sendChunk(pcm.buffer);
      offset += FRAME_SAMPLES;
    }
    pending = merged.slice(offset);
  };

  input.connect(processor);
  processor.connect(mute);
  mute.connect(context.destination);

  return {
    stop() {
      if (stopped) return;
      stopped = true;
      processor.onaudioprocess = null;
      pending = new Float32Array(0);
      try {
        input.disconnect();
        processor.disconnect();
        mute.disconnect();
      } catch {
        /* already disconnected */
      }
      stream.getTracks().forEach((track) => track.stop());
      void context.close().catch(() => undefined);
    },
  };
}
