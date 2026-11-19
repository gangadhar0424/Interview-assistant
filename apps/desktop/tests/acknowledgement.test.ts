/**
 * M-1 acceptable-use acknowledgement: the persisted state, the main-process gates inside the audio
 * and capture services, IPC input validation, the shared wording, and the renderer's first-use flow.
 * (The channel-level tests that drive the real IPC layer live in audio-ipc and capture-ipc.)
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ACCEPTABLE_USE_STATEMENTS,
  ACCEPTABLE_USE_VERSION,
  ACKNOWLEDGEMENT_REQUIRED_CODE,
} from "../src/shared/acknowledgement";
import { AudioRefusal, AudioService, type AudioPorts } from "../src/main/audio-service";
import { CaptureRefusal, CaptureService, COUNTDOWN_SECONDS, type CapturePorts } from "../src/main/capture-service";
import { validateAcknowledgementVersion } from "../src/main/ipc-validation";
import { SettingsStore } from "../src/main/settings-store";
import type { DesktopResult } from "../src/shared/contracts";
import type { AcknowledgementState } from "../src/shared/acknowledgement";
import { useDesktopStore } from "../src/renderer/src/store";

const USER = "00000000-0000-4000-8000-000000000001";
const OTHER = "00000000-0000-4000-8000-000000000002";
const SESSION = "11111111-2222-4333-8444-555555555555";
const AT = "2026-11-18T09:00:00.000Z";

let dir: string;
let file: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "ack-"));
  file = join(dir, "settings.json");
});
afterEach(() => rmSync(dir, { recursive: true, force: true }));

describe("the notice wording", () => {
  it("covers the three required responsibilities and is versioned", () => {
    expect(ACCEPTABLE_USE_VERSION).toBeGreaterThanOrEqual(1);
    const text = ACCEPTABLE_USE_STATEMENTS.join(" ").toLowerCase();
    expect(text).toContain("rules of any interview, meeting or assessment");
    expect(text).toContain("permission");
    expect(text).toContain("prohibit");
  });
});

describe("persisted acknowledgement state", () => {
  it("is not accepted on first use, for anyone", () => {
    const store = new SettingsStore(file);
    expect(store.acknowledgement(USER)).toEqual({ accepted: false, version: ACCEPTABLE_USE_VERSION, acceptedAt: null });
    expect(store.isAcknowledged(null)).toBe(false);
  });

  it("is accepted for the user who accepted, with the time, and for nobody else", () => {
    const store = new SettingsStore(file);
    expect(store.acknowledge(USER, AT)).toEqual({ accepted: true, version: ACCEPTABLE_USE_VERSION, acceptedAt: AT });
    expect(store.isAcknowledged(USER)).toBe(true);
    expect(store.isAcknowledged(OTHER)).toBe(false);
    expect(store.isAcknowledged(null)).toBe(false);
  });

  it("survives an application restart and later settings changes", () => {
    const first = new SettingsStore(file);
    first.acknowledge(USER, AT);
    first.update({ alwaysOnTop: false, opacity: 0.8 });
    const second = new SettingsStore(file);
    expect(second.load().opacity).toBe(0.8);
    expect(second.isAcknowledged(USER)).toBe(true);
    second.update({ expanded: true });
    const third = new SettingsStore(file);
    third.load();
    expect(third.isAcknowledged(USER)).toBe(true);
  });

  it("is not accepted when the wording version has changed since", () => {
    const store = new SettingsStore(file);
    store.acknowledge(USER, AT);
    const stored = JSON.parse(readFileSync(file, "utf8"));
    stored.acceptableUse.version = ACCEPTABLE_USE_VERSION - 1;
    writeFileSync(file, JSON.stringify(stored));
    const reloaded = new SettingsStore(file);
    reloaded.load();
    expect(reloaded.isAcknowledged(USER)).toBe(false);
  });

  it("treats a damaged or forged entry as not accepted and still loads the settings", () => {
    const base = { backendBaseUrl: "http://127.0.0.1:8000", alwaysOnTop: true, opacity: 0.9, expanded: false };
    for (const bad of [
      null,
      "yes",
      { version: "1", userId: USER, acceptedAt: AT },
      { version: ACCEPTABLE_USE_VERSION, userId: "not-a-uuid", acceptedAt: AT },
      { version: ACCEPTABLE_USE_VERSION, userId: USER, acceptedAt: "never" },
      { version: ACCEPTABLE_USE_VERSION, userId: USER },
    ]) {
      writeFileSync(file, JSON.stringify({ ...base, acceptableUse: bad }));
      const store = new SettingsStore(file);
      expect(store.load().opacity).toBe(0.9);
      expect(store.isAcknowledged(USER)).toBe(false);
    }
  });

  it("stores only the version, the account id and the time", () => {
    new SettingsStore(file).acknowledge(USER, AT);
    expect(Object.keys(JSON.parse(readFileSync(file, "utf8")).acceptableUse).sort()).toEqual([
      "acceptedAt",
      "userId",
      "version",
    ]);
  });

  it("refuses to store an acknowledgement with an invalid account id or time", () => {
    const store = new SettingsStore(file);
    expect(() => store.acknowledge("not-a-uuid", AT)).toThrow();
    expect(() => store.acknowledge(USER, "yesterday")).toThrow();
    expect(store.isAcknowledged(USER)).toBe(false);
  });
});

describe("IPC input validation", () => {
  it("accepts exactly the current version and nothing else", () => {
    expect(validateAcknowledgementVersion(ACCEPTABLE_USE_VERSION)).toBe(ACCEPTABLE_USE_VERSION);
    for (const bad of [0, ACCEPTABLE_USE_VERSION + 1, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, "1", null, undefined, {}, [1], true]) {
      expect(() => validateAcknowledgementVersion(bad), String(bad)).toThrow();
    }
  });
});

describe("main-process gate inside AudioService", () => {
  let acknowledged = false;
  const indicators: string[] = [];
  const make = () =>
    new AudioService({
      isEnabled: () => true,
      isSignedIn: () => true,
      isAcknowledged: () => acknowledged,
      windowState: () => ({ visible: true, focused: true, minimized: false }),
      sessionReady: async () => true,
      voiceProviderChosen: async () => true,
      transcribe: async () => ({ text: "x", truncated: false, isQuestion: false, confidence: 0 }),
      describeError: () => "failed",
      fatalReason: () => null,
      emit: () => undefined,
      setIndicator: (state) => indicators.push(state),
      now: () => Date.now(),
    } satisfies AudioPorts);

  beforeEach(() => {
    acknowledged = false;
    indicators.length = 0;
  });

  it("refuses before acknowledgement: nothing arms, nothing is indicated", async () => {
    const service = make();
    await expect(service.start("microphone", SESSION)).rejects.toMatchObject({
      code: ACKNOWLEDGEMENT_REQUIRED_CODE,
    });
    await expect(service.start("system", SESSION)).rejects.toBeInstanceOf(AudioRefusal);
    expect(service.gate()).toEqual({ state: "idle", source: null });
    expect(indicators).toEqual([]);
  });

  it("starts after acknowledgement, with the usual arming state and indicator", async () => {
    const service = make();
    acknowledged = true;
    await service.start("microphone", SESSION);
    expect(service.gate().state).toBe("arming");
    expect(indicators).toEqual(["arming"]);
    service.dispose();
  });

  it("checks again after the awaited network steps, so a notice revoked meanwhile still blocks", async () => {
    acknowledged = true;
    const service = new AudioService({
      isEnabled: () => true,
      isSignedIn: () => true,
      isAcknowledged: () => acknowledged,
      windowState: () => ({ visible: true, focused: true, minimized: false }),
      sessionReady: async () => {
        acknowledged = false; // revoked while the session check was in flight
        return true;
      },
      voiceProviderChosen: async () => true,
      transcribe: async () => ({ text: "x", truncated: false, isQuestion: false, confidence: 0 }),
      describeError: () => "failed",
      fatalReason: () => null,
      emit: () => undefined,
      setIndicator: (state) => indicators.push(state),
      now: () => Date.now(),
    });
    await expect(service.start("microphone", SESSION)).rejects.toMatchObject({ code: ACKNOWLEDGEMENT_REQUIRED_CODE });
    expect(indicators).toEqual([]);
  });
});

describe("main-process gate inside CaptureService", () => {
  let acknowledged = false;
  const captured: string[] = [];
  const make = () => {
    const ports: CapturePorts = {
      isEnabled: () => true,
      isSignedIn: () => true,
      isAcknowledged: () => acknowledged,
      windowState: () => ({ visible: true, focused: true, minimized: false }),
      listDisplays: () => [{ id: "1", label: "Display 1", primary: true, width: 100, height: 100 }],
      captureDisplay: async () => {
        captured.push("captured");
        return Buffer.alloc(64, 7);
      },
      makePreview: () => ({ dataUrl: "data:image/png;base64,AAAA", width: 100, height: 100 }),
      sessionReady: async () => true,
      upload: async () => ({ text: "x", truncated: false }),
      emit: () => undefined,
      setIndicator: () => undefined,
      now: () => Date.now(),
    };
    return new CaptureService(ports);
  };

  beforeEach(() => {
    vi.useFakeTimers();
    acknowledged = false;
    captured.length = 0;
  });
  afterEach(() => vi.useRealTimers());

  it("refuses the picker and start before acknowledgement, and never captures", async () => {
    const service = make();
    expect(() => service.listDisplays()).toThrow(CaptureRefusal);
    await expect(service.start("1", SESSION)).rejects.toMatchObject({ code: ACKNOWLEDGEMENT_REQUIRED_CODE });
    await vi.advanceTimersByTimeAsync(COUNTDOWN_SECONDS * 1000 + 500);
    expect(captured).toEqual([]);
    expect(service.busy()).toBe(false);
  });

  it("captures only after acknowledgement AND the visible countdown", async () => {
    const service = make();
    acknowledged = true;
    await service.start("1", SESSION);
    expect(captured).toEqual([]); // the countdown still has to run
    await vi.advanceTimersByTimeAsync(COUNTDOWN_SECONDS * 1000 + 200);
    expect(captured).toEqual(["captured"]);
    service.dispose();
  });
});

describe("renderer first-use flow", () => {
  const ack = (accepted: boolean): AcknowledgementState => ({
    accepted,
    version: ACCEPTABLE_USE_VERSION,
    acceptedAt: accepted ? AT : null,
  });
  const ok = <T,>(data: T): DesktopResult<T> => ({ ok: true, data });
  let desktop: {
    app: {
      getAcknowledgement: ReturnType<typeof vi.fn>;
      acceptAcknowledgement: ReturnType<typeof vi.fn>;
    };
  };

  beforeEach(() => {
    desktop = {
      app: {
        getAcknowledgement: vi.fn(async () => ok(ack(false))),
        acceptAcknowledgement: vi.fn(async () => ok(ack(true))),
      },
    };
    vi.stubGlobal("window", { desktop });
    useDesktopStore.setState({ acknowledgement: null, acknowledgementBusy: false, acknowledgementNotice: null });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("starts unknown, loads as not accepted, and becomes accepted only through main", async () => {
    expect(useDesktopStore.getState().acknowledgement).toBeNull();
    await useDesktopStore.getState().loadAcknowledgement();
    expect(useDesktopStore.getState().acknowledgement?.accepted).toBe(false);
    await useDesktopStore.getState().acceptAcknowledgement();
    expect(desktop.app.acceptAcknowledgement).toHaveBeenCalledWith(ACCEPTABLE_USE_VERSION);
    expect(useDesktopStore.getState().acknowledgement?.accepted).toBe(true);
    expect(useDesktopStore.getState().acknowledgementBusy).toBe(false);
  });

  it("stays not accepted and shows a safe message when the user cancels the native dialog", async () => {
    await useDesktopStore.getState().loadAcknowledgement();
    desktop.app.acceptAcknowledgement.mockResolvedValueOnce({
      ok: false,
      error: { kind: "unknown", message: "The acceptable-use notice was not accepted." },
    });
    await useDesktopStore.getState().acceptAcknowledgement();
    const state = useDesktopStore.getState();
    expect(state.acknowledgement?.accepted).toBe(false);
    expect(state.acknowledgementNotice).toContain("not accepted");
    expect(state.acknowledgementBusy).toBe(false);
  });

  it("treats an unreadable state as not accepted (main still enforces the real answer)", async () => {
    desktop.app.getAcknowledgement.mockRejectedValueOnce(new Error("boom"));
    await useDesktopStore.getState().loadAcknowledgement();
    expect(useDesktopStore.getState().acknowledgement).toBeNull();
  });

  it("ignores a second accept press while one confirmation is open", async () => {
    let release: (value: DesktopResult<AcknowledgementState>) => void = () => undefined;
    desktop.app.acceptAcknowledgement.mockImplementationOnce(
      () => new Promise((resolve) => (release = resolve)),
    );
    const first = useDesktopStore.getState().acceptAcknowledgement();
    await useDesktopStore.getState().acceptAcknowledgement();
    expect(desktop.app.acceptAcknowledgement).toHaveBeenCalledTimes(1);
    release(ok(ack(true)));
    await first;
    expect(useDesktopStore.getState().acknowledgement?.accepted).toBe(true);
  });
});
