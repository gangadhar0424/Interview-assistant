import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CaptureEvent } from "../src/shared/contracts";
import {
  CaptureRefusal,
  CaptureService,
  COUNTDOWN_SECONDS,
  EXTRACTION_TTL_MS,
  HELD_IMAGE_TTL_MS,
  MAX_CAPTURES_PER_SESSION,
  MAX_PNG_BYTES,
  MIN_CAPTURE_INTERVAL_MS,
  type CapturePorts,
  type IndicatorState,
} from "../src/main/capture-service";
import { isBlankBitmap, scaledSize } from "../src/main/capture-image";

const SESSION = "11111111-2222-4333-8444-555555555555";
const OTHER_SESSION = "99999999-2222-4333-8444-555555555555";
const DISPLAY = "2528732444";

function png(fill = 7, size = 64): Buffer {
  return Buffer.alloc(size, fill);
}

type Harness = ReturnType<typeof makeHarness>;

function makeHarness(overrides: Partial<CapturePorts> = {}) {
  const events: CaptureEvent[] = [];
  const indicators: IndicatorState[] = [];
  const state = {
    enabled: true,
    signedIn: true,
    window: { visible: true, focused: true, minimized: false },
    readySessions: new Set([SESSION, OTHER_SESSION]),
    capturedBuffers: [] as Buffer[],
    uploads: [] as Array<{ png: Buffer; sessionId: string; copy: number[] }>,
    uploadImpl: async (_png: Buffer, _sessionId: string, _signal: AbortSignal) => ({
      text: "What is a mutex?",
      truncated: false,
    }),
    captureImpl: async (): Promise<Buffer | null> => png(),
  };
  const ports: CapturePorts = {
    isEnabled: () => state.enabled,
    isSignedIn: () => state.signedIn,
    windowState: () => state.window,
    listDisplays: () => [
      { id: DISPLAY, label: "Display 1 (primary) · 1920×1080", primary: true, width: 1920, height: 1080 },
    ],
    captureDisplay: vi.fn(async () => {
      const buffer = await state.captureImpl();
      if (buffer) state.capturedBuffers.push(buffer);
      return buffer;
    }),
    makePreview: vi.fn(() => ({ dataUrl: "data:image/png;base64,AAAA", width: 1920, height: 1080 })),
    sessionReady: vi.fn(async (sessionId: string) => state.readySessions.has(sessionId)),
    upload: vi.fn(async (buffer: Buffer, sessionId: string, signal: AbortSignal) => {
      state.uploads.push({ png: buffer, sessionId, copy: [...buffer] });
      return state.uploadImpl(buffer, sessionId, signal);
    }),
    emit: (event) => events.push(event),
    setIndicator: (value) => indicators.push(value),
    now: () => Date.now(),
    ...overrides,
  };
  const service = new CaptureService(ports);
  return { service, ports, state, events, indicators };
}

async function runCountdown(): Promise<void> {
  await vi.advanceTimersByTimeAsync(COUNTDOWN_SECONDS * 1000);
}

async function captured(h: Harness): Promise<string> {
  await h.service.start(DISPLAY, SESSION);
  await runCountdown();
  const event = [...h.events].reverse().find((e) => e.type === "captured");
  if (event?.type !== "captured") throw new Error("expected a captured event");
  return event.captureId;
}

const zeroed = (buffer: Buffer) => buffer.every((byte) => byte === 0);

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-11-10T10:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
});

describe("explicit, user-initiated capture only", () => {
  it("never captures on its own: constructing the service and waiting does nothing", async () => {
    const h = makeHarness();
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(h.ports.captureDisplay).not.toHaveBeenCalled();
    expect(h.events).toEqual([]);
    expect(h.service.busy()).toBe(false);
  });

  it("listing displays does not capture", () => {
    const h = makeHarness();
    expect(h.service.listDisplays()).toHaveLength(1);
    expect(h.ports.captureDisplay).not.toHaveBeenCalled();
  });

  it("captures only after the full countdown, with visible ticks 3, 2, 1", async () => {
    const h = makeHarness();
    await h.service.start(DISPLAY, SESSION);
    expect(h.events).toEqual([
      { type: "countdown", seconds: 3, displayLabel: "Display 1 (primary) · 1920×1080" },
    ]);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(h.ports.captureDisplay).not.toHaveBeenCalled();
    expect(h.events.map((e) => (e.type === "countdown" ? e.seconds : e.type))).toEqual([3, 2, 1]);
    await vi.advanceTimersByTimeAsync(999);
    expect(h.ports.captureDisplay).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(h.ports.captureDisplay).toHaveBeenCalledTimes(1);
    expect(h.ports.captureDisplay).toHaveBeenCalledWith(DISPLAY);
    expect(h.indicators).toEqual(["countdown", "capturing", "held"]);
  });
});

describe("conditions that must hold before capture", () => {
  it.each([
    ["feature disabled", (h: Harness) => (h.state.enabled = false), "screenshot_disabled"],
    ["signed out", (h: Harness) => (h.state.signedIn = false), "not_signed_in"],
    ["window hidden", (h: Harness) => (h.state.window.visible = false), "window_not_ready"],
    ["window not focused", (h: Harness) => (h.state.window.focused = false), "window_not_ready"],
    ["window minimised", (h: Harness) => (h.state.window.minimized = true), "window_not_ready"],
  ])("refuses to start when %s", async (_name, arrange, code) => {
    const h = makeHarness();
    arrange(h);
    await expect(h.service.start(DISPLAY, SESSION)).rejects.toMatchObject({ code });
    expect(h.service.busy()).toBe(false);
    expect(h.ports.captureDisplay).not.toHaveBeenCalled();
    expect(h.events).toEqual([]);
  });

  it("refuses when no valid session is selected, or it is not ready", async () => {
    const h = makeHarness();
    h.state.readySessions.delete(SESSION);
    await expect(h.service.start(DISPLAY, SESSION)).rejects.toMatchObject({ code: "session_not_ready" });
    await expect(h.service.start(DISPLAY, "00000000-0000-4000-8000-000000000000")).rejects.toMatchObject({
      code: "session_not_ready",
    });
    expect(h.ports.captureDisplay).not.toHaveBeenCalled();
  });

  it("refuses an unknown display", async () => {
    const h = makeHarness();
    await expect(h.service.start("999", SESSION)).rejects.toMatchObject({ code: "display_not_found" });
  });

  it("refuses to list displays while disabled or signed out", () => {
    const h = makeHarness();
    h.state.enabled = false;
    expect(() => h.service.listDisplays()).toThrow(CaptureRefusal);
    h.state.enabled = true;
    h.state.signedIn = false;
    expect(() => h.service.listDisplays()).toThrow(CaptureRefusal);
  });

  it("allows one capture at a time", async () => {
    const h = makeHarness();
    await h.service.start(DISPLAY, SESSION);
    await expect(h.service.start(DISPLAY, SESSION)).rejects.toMatchObject({ code: "capture_in_progress" });
    await runCountdown();
    await expect(h.service.start(DISPLAY, SESSION)).rejects.toMatchObject({ code: "capture_in_progress" });
  });

  it("enforces a minimum interval and a per-session cap", async () => {
    const h = makeHarness();
    await captured(h);
    h.service.cancel();
    await expect(h.service.start(DISPLAY, SESSION)).rejects.toMatchObject({ code: "too_soon" });
    for (let i = 1; i < MAX_CAPTURES_PER_SESSION; i += 1) {
      await vi.advanceTimersByTimeAsync(MIN_CAPTURE_INTERVAL_MS);
      await h.service.start(DISPLAY, SESSION);
      h.service.cancel();
    }
    await vi.advanceTimersByTimeAsync(MIN_CAPTURE_INTERVAL_MS);
    await expect(h.service.start(DISPLAY, SESSION)).rejects.toMatchObject({ code: "capture_limit" });
    await h.service.start(DISPLAY, OTHER_SESSION); // another session has its own allowance
  });

  it("re-checks the conditions after the network lookup and when the countdown ends", async () => {
    const h = makeHarness();
    h.ports.sessionReady = vi.fn(async () => {
      h.state.window.visible = false; // the window was hidden while we waited
      return true;
    });
    await expect(h.service.start(DISPLAY, SESSION)).rejects.toMatchObject({ code: "window_not_ready" });
    expect(h.service.busy()).toBe(false);
  });

  it.each([
    ["the window is hidden", (h: Harness) => (h.state.window.visible = false), "window"],
    ["the window is minimised", (h: Harness) => (h.state.window.minimized = true), "window"],
    ["the feature is turned off", (h: Harness) => (h.state.enabled = false), "disabled"],
    ["the user signs out", (h: Harness) => (h.state.signedIn = false), "signed_out"],
  ])("cancels without capturing if %s during the countdown", async (_name, arrange, reason) => {
    const h = makeHarness();
    await h.service.start(DISPLAY, SESSION);
    await vi.advanceTimersByTimeAsync(1000);
    arrange(h);
    await vi.advanceTimersByTimeAsync(5000);
    expect(h.ports.captureDisplay).not.toHaveBeenCalled();
    expect(h.events.at(-1)).toMatchObject({ type: "cancelled", reason });
    expect(h.service.busy()).toBe(false);
    expect(h.indicators.at(-1)).toBe("idle");
  });

  it("does not require focus at capture time (the user may be arranging another window)", async () => {
    const h = makeHarness();
    await h.service.start(DISPLAY, SESSION);
    h.state.window.focused = false;
    await runCountdown();
    expect(h.ports.captureDisplay).toHaveBeenCalledTimes(1);
  });
});

describe("cancellation at every stage", () => {
  it("cancels during the countdown: nothing is ever captured", async () => {
    const h = makeHarness();
    await h.service.start(DISPLAY, SESSION);
    await vi.advanceTimersByTimeAsync(1500);
    h.service.cancel();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.ports.captureDisplay).not.toHaveBeenCalled();
    expect(h.events.at(-1)).toMatchObject({ type: "cancelled", reason: "user" });
    expect(h.service.busy()).toBe(false);
    expect(h.indicators.at(-1)).toBe("idle");
  });

  it("cancels while the screen is being read: the image is dropped and never held", async () => {
    const h = makeHarness();
    let release: (b: Buffer) => void = () => undefined;
    h.state.captureImpl = () => new Promise<Buffer>((resolve) => (release = resolve));
    await h.service.start(DISPLAY, SESSION);
    await runCountdown();
    h.service.cancel(); // the capture is in flight
    const late = png(9);
    release(late);
    await vi.advanceTimersByTimeAsync(0);
    expect(h.service.hasHeldImage()).toBe(false);
    expect(zeroed(late)).toBe(true);
    expect(h.events.some((e) => e.type === "captured")).toBe(false);
  });

  it("discards a held image on user cancel and wipes it", async () => {
    const h = makeHarness();
    await captured(h);
    const buffer = h.state.capturedBuffers[0] as Buffer;
    expect(zeroed(buffer)).toBe(false);
    h.service.cancel();
    expect(zeroed(buffer)).toBe(true);
    expect(h.service.hasHeldImage()).toBe(false);
    expect(h.events.at(-1)).toMatchObject({ type: "cancelled", reason: "user" });
  });

  it("cancels while the image is being sent: the upload is aborted and the image wiped", async () => {
    const h = makeHarness();
    let signal: AbortSignal | undefined;
    h.state.uploadImpl = (_p, _s, s) =>
      new Promise((_resolve, reject) => {
        signal = s;
        s.addEventListener("abort", () => reject(new Error("aborted")));
      });
    const id = await captured(h);
    const buffer = h.state.capturedBuffers[0] as Buffer;
    const pending = h.service.extract(id);
    const assertion = expect(pending).rejects.toBeDefined();
    await vi.advanceTimersByTimeAsync(0);
    h.service.cancel();
    await assertion;
    expect(signal?.aborted).toBe(true);
    expect(zeroed(buffer)).toBe(true);
    expect(h.service.busy()).toBe(false);
  });

  it.each(["window", "disabled", "signed_out", "session", "navigation"] as const)(
    "discards a held image on %s",
    async (reason) => {
      const h = makeHarness();
      await captured(h);
      const buffer = h.state.capturedBuffers[0] as Buffer;
      h.service.discard(reason);
      expect(zeroed(buffer)).toBe(true);
      expect(h.service.hasHeldImage()).toBe(false);
      expect(h.events.at(-1)).toMatchObject({ type: "cancelled", reason });
    },
  );

  it("discards an unsent image after the hold time", async () => {
    const h = makeHarness();
    await captured(h);
    const buffer = h.state.capturedBuffers[0] as Buffer;
    await vi.advanceTimersByTimeAsync(HELD_IMAGE_TTL_MS - 1);
    expect(h.service.hasHeldImage()).toBe(true);
    await vi.advanceTimersByTimeAsync(1);
    expect(h.service.hasHeldImage()).toBe(false);
    expect(zeroed(buffer)).toBe(true);
    expect(h.events.at(-1)).toMatchObject({ type: "cancelled", reason: "expired" });
  });

  it("dispose wipes everything without notifying the renderer", async () => {
    const h = makeHarness();
    await captured(h);
    const buffer = h.state.capturedBuffers[0] as Buffer;
    const before = h.events.length;
    h.service.dispose();
    expect(zeroed(buffer)).toBe(true);
    expect(h.events.length).toBe(before);
    expect(h.service.busy()).toBe(false);
  });

  it("is safe to cancel when nothing is happening", () => {
    const h = makeHarness();
    h.service.cancel();
    h.service.discard("window");
    h.service.dispose();
    expect(h.events).toEqual([]);
  });
});

describe("preview before send, explicit confirmation, discard afterwards", () => {
  it("holds the image after capture and sends nothing until extract is called", async () => {
    const h = makeHarness();
    const id = await captured(h);
    const event = h.events.at(-1);
    expect(event).toMatchObject({
      type: "captured",
      captureId: id,
      previewDataUrl: "data:image/png;base64,AAAA",
      width: 1920,
      height: 1080,
      bytes: 64,
    });
    expect(h.ports.makePreview).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(HELD_IMAGE_TTL_MS - 1000);
    expect(h.ports.upload).not.toHaveBeenCalled(); // no automatic send, ever
    expect(h.service.hasHeldImage()).toBe(true);
    expect(h.indicators.at(-1)).toBe("held");
  });

  it("the renderer-visible event never carries the image bytes", async () => {
    const h = makeHarness();
    await captured(h);
    const serialized = JSON.stringify(h.events);
    expect(serialized).not.toContain(JSON.stringify([...(h.state.capturedBuffers[0] as Buffer)]));
    expect(Object.keys(h.events.at(-1) as object).sort()).toEqual(
      ["bytes", "captureId", "height", "previewDataUrl", "type", "width"].sort(),
    );
  });

  it("extract sends the held image once, returns editable text, and wipes the image", async () => {
    const h = makeHarness();
    const id = await captured(h);
    const buffer = h.state.capturedBuffers[0] as Buffer;
    const result = await h.service.extract(id);
    expect(result).toMatchObject({ text: "What is a mutex?", truncated: false });
    expect(result.extractionId).toMatch(/^[0-9a-f-]{36}$/);
    expect(h.state.uploads).toHaveLength(1);
    expect(h.state.uploads[0]).toMatchObject({ sessionId: SESSION });
    expect(h.state.uploads[0]?.copy.every((byte) => byte === 7)).toBe(true); // real pixels were sent
    expect(zeroed(buffer)).toBe(true); // ...and are gone afterwards
    expect(h.service.hasHeldImage()).toBe(false);
    expect(h.indicators.at(-1)).toBe("idle");
  });

  it("wipes the image when the upload fails, and the user gets the safe error", async () => {
    const h = makeHarness();
    h.state.uploadImpl = async () => {
      throw new CaptureRefusal("vision_unavailable", "Screenshot input is not available on this server.");
    };
    const id = await captured(h);
    const buffer = h.state.capturedBuffers[0] as Buffer;
    await expect(h.service.extract(id)).rejects.toMatchObject({ code: "vision_unavailable" });
    expect(zeroed(buffer)).toBe(true);
    expect(h.service.hasHeldImage()).toBe(false);
    await expect(h.service.extract(id)).rejects.toMatchObject({ code: "capture_not_found" });
  });

  it("rejects unknown, stale and repeated capture ids", async () => {
    const h = makeHarness();
    await expect(h.service.extract("00000000-0000-4000-8000-000000000000")).rejects.toMatchObject({
      code: "capture_not_found",
    });
    const id = await captured(h);
    await expect(h.service.extract("00000000-0000-4000-8000-000000000000")).rejects.toMatchObject({
      code: "capture_not_found",
    });
    await h.service.extract(id);
    await expect(h.service.extract(id)).rejects.toMatchObject({ code: "capture_not_found" });
  });

  it("does not allow two uploads of the same image at once", async () => {
    const h = makeHarness();
    h.state.uploadImpl = () => new Promise(() => undefined);
    const id = await captured(h);
    void h.service.extract(id).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(0);
    await expect(h.service.extract(id)).rejects.toMatchObject({ code: "capture_not_found" });
    h.service.dispose();
  });

  it("refuses to send if the user signed out or disabled the feature after the preview", async () => {
    const h = makeHarness();
    const id = await captured(h);
    h.state.signedIn = false;
    await expect(h.service.extract(id)).rejects.toMatchObject({ code: "not_signed_in" });
    expect(h.ports.upload).not.toHaveBeenCalled();
  });
});

describe("capture failures", () => {
  it.each([
    ["the screen cannot be read", async () => null],
    ["the capture throws", async (): Promise<Buffer | null> => Promise.reject(new Error("device"))],
    ["the image is empty", async () => Buffer.alloc(0)],
    ["the image is over the size cap", async () => Buffer.alloc(MAX_PNG_BYTES + 1, 1)],
  ])("reports a safe error and holds nothing when %s", async (_name, impl) => {
    const h = makeHarness();
    h.state.captureImpl = impl as () => Promise<Buffer | null>;
    await h.service.start(DISPLAY, SESSION);
    await runCountdown();
    expect(h.events.at(-1)).toMatchObject({ type: "cancelled", reason: "error" });
    expect(h.service.hasHeldImage()).toBe(false);
    expect(h.ports.upload).not.toHaveBeenCalled();
    expect(JSON.stringify(h.events)).not.toContain("device");
  });

  it("wipes an oversized image instead of keeping it", async () => {
    const h = makeHarness();
    const big = Buffer.alloc(MAX_PNG_BYTES + 1, 5);
    h.state.captureImpl = async () => big;
    await h.service.start(DISPLAY, SESSION);
    await runCountdown();
    expect(zeroed(big)).toBe(true);
  });

  it("drops an image captured just as the window was hidden", async () => {
    const h = makeHarness();
    h.state.captureImpl = async () => {
      h.state.window.visible = false;
      return png(3);
    };
    await h.service.start(DISPLAY, SESSION);
    await runCountdown();
    expect(h.service.hasHeldImage()).toBe(false);
    expect(zeroed(h.state.capturedBuffers[0] as Buffer)).toBe(true);
    expect(h.events.at(-1)).toMatchObject({ type: "cancelled", reason: "window" });
  });

  it("wipes the image if the preview cannot be built", async () => {
    const h = makeHarness();
    h.ports.makePreview = vi.fn(() => {
      throw new Error("no preview");
    });
    await h.service.start(DISPLAY, SESSION);
    await runCountdown();
    expect(zeroed(h.state.capturedBuffers[0] as Buffer)).toBe(true);
    expect(h.service.hasHeldImage()).toBe(false);
  });
});

describe("single-use extraction tokens (input_source = screenshot)", () => {
  it("is valid once, for the same session only", async () => {
    const h = makeHarness();
    const { extractionId } = await h.service.extract(await captured(h));
    expect(h.service.consumeExtraction(extractionId, OTHER_SESSION)).toBe(false);
    expect(h.service.consumeExtraction(extractionId, SESSION)).toBe(false); // wrong-session try burned it
    const second = await (async () => {
      await vi.advanceTimersByTimeAsync(MIN_CAPTURE_INTERVAL_MS);
      return h.service.extract(await captured(h));
    })();
    expect(h.service.consumeExtraction(second.extractionId, SESSION)).toBe(true);
    expect(h.service.consumeExtraction(second.extractionId, SESSION)).toBe(false);
  });

  it("rejects forged ids and expires", async () => {
    const h = makeHarness();
    expect(h.service.consumeExtraction("00000000-0000-4000-8000-000000000000", SESSION)).toBe(false);
    const { extractionId } = await h.service.extract(await captured(h));
    await vi.advanceTimersByTimeAsync(EXTRACTION_TTL_MS + 1);
    expect(h.service.consumeExtraction(extractionId, SESSION)).toBe(false);
  });

  it("can be restored when the question never started, and is cleared on sign-out", async () => {
    const h = makeHarness();
    const { extractionId } = await h.service.extract(await captured(h));
    expect(h.service.consumeExtraction(extractionId, SESSION)).toBe(true);
    h.service.restoreExtraction(extractionId, SESSION);
    expect(h.service.consumeExtraction(extractionId, SESSION)).toBe(true);
    h.service.restoreExtraction(extractionId, SESSION);
    h.service.discard("signed_out");
    expect(h.service.consumeExtraction(extractionId, SESSION)).toBe(false);
  });
});

describe("image helpers", () => {
  it("scales down only, preserving the aspect ratio", () => {
    expect(scaledSize(3840, 2160, 2560)).toEqual({ width: 2560, height: 1440 });
    expect(scaledSize(1920, 1080, 2560)).toEqual({ width: 1920, height: 1080 });
    expect(scaledSize(1080, 1920, 640)).toEqual({ width: 360, height: 640 });
    expect(scaledSize(0, 0, 640)).toEqual({ width: 1, height: 1 });
  });

  it("detects blank (all black or empty) captures", () => {
    expect(isBlankBitmap(new Uint8Array(4096 * 4))).toBe(true);
    expect(isBlankBitmap(new Uint8Array(2))).toBe(true);
    const lit = new Uint8Array(4096 * 4);
    lit[4 * 100] = 200;
    expect(isBlankBitmap(lit)).toBe(false);
  });
});
