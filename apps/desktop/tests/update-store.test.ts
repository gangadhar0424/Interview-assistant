/** M-11 renderer state: the store only mirrors what main decides and never starts capture. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DesktopResult } from "../src/shared/contracts";
import type { UpdateState } from "../src/shared/updates";
import { useDesktopStore } from "../src/renderer/src/store";

const ok = <T,>(data: T): DesktopResult<T> => ({ ok: true, data });
const available: UpdateState = { status: "available", version: "0.2.0", sizeBytes: 100 };

function bridge() {
  return {
    updates: {
      getState: vi.fn(async () => ok<UpdateState>({ status: "idle", lastCheckedAt: null })),
      check: vi.fn(async () => ok<UpdateState>({ status: "none", checkedAt: "2026-11-22T00:00:00.000Z" })),
      download: vi.fn(async () => ok<UpdateState>({ status: "downloaded", version: "0.2.0" })),
      install: vi.fn(async () => ok<UpdateState>({ status: "installing", version: "0.2.0" })),
    },
    audio: { start: vi.fn(), stop: vi.fn(), sendChunk: vi.fn() },
    capture: { start: vi.fn(), cancel: vi.fn(), extract: vi.fn(), listDisplays: vi.fn() },
  };
}
let desktop: ReturnType<typeof bridge>;
const state = () => useDesktopStore.getState();

beforeEach(() => {
  desktop = bridge();
  vi.stubGlobal("window", { desktop });
  useDesktopStore.setState({ update: { status: "disabled", reason: "development" } });
});

describe("update state in the renderer", () => {
  it("starts disabled so nothing is shown until main says otherwise", () => {
    expect(state().update).toEqual({ status: "disabled", reason: "development" });
  });

  it("mirrors pushed events exactly", () => {
    state().handleUpdateEvent(available);
    expect(state().update).toEqual(available);
    state().handleUpdateEvent({ status: "downloading", version: "0.2.0", percent: 40 });
    expect(state().update).toEqual({ status: "downloading", version: "0.2.0", percent: 40 });
  });

  it("loads the current state at startup", async () => {
    desktop.updates.getState.mockResolvedValueOnce(ok(available));
    await state().loadUpdateState();
    expect(state().update).toEqual(available);
  });

  it("keeps the previous state when loading fails or the bridge rejects", async () => {
    state().handleUpdateEvent(available);
    desktop.updates.getState.mockResolvedValueOnce({ ok: false, error: { kind: "unknown", message: "x" } });
    await state().loadUpdateState();
    expect(state().update).toEqual(available);
    desktop.updates.getState.mockRejectedValueOnce(new Error("ipc gone"));
    await expect(state().loadUpdateState()).resolves.toBeUndefined();
    expect(state().update).toEqual(available);
  });

  it("adopts the state main returns from each user action and nothing else", async () => {
    await state().checkForUpdate();
    expect(state().update.status).toBe("none");
    await state().downloadUpdate();
    expect(state().update).toEqual({ status: "downloaded", version: "0.2.0" });
    await state().installUpdate();
    expect(state().update).toEqual({ status: "installing", version: "0.2.0" });
    expect(desktop.updates.check).toHaveBeenCalledTimes(1);
    expect(desktop.updates.download).toHaveBeenCalledTimes(1);
    expect(desktop.updates.install).toHaveBeenCalledTimes(1);
  });

  it("survives a failing action (for example the app quitting to install)", async () => {
    desktop.updates.install.mockRejectedValueOnce(new Error("channel closed"));
    await expect(state().installUpdate()).resolves.toBeUndefined();
    desktop.updates.download.mockResolvedValueOnce({ ok: false, error: { kind: "unknown", message: "x" } });
    state().handleUpdateEvent(available);
    await state().downloadUpdate();
    expect(state().update).toEqual(available);
  });

  it("no update action ever starts audio or screen capture", async () => {
    await state().loadUpdateState();
    await state().checkForUpdate();
    await state().downloadUpdate();
    await state().installUpdate();
    expect(desktop.audio.start).not.toHaveBeenCalled();
    expect(desktop.capture.start).not.toHaveBeenCalled();
    expect(desktop.capture.extract).not.toHaveBeenCalled();
  });
});
