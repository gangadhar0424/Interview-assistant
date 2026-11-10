/** Phase 11B renderer state: the screenshot flow as the user experiences it, with a fake bridge. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CaptureEvent, DesktopResult } from "../src/shared/contracts";
import { useDesktopStore } from "../src/renderer/src/store";

const SESSION = "11111111-2222-4333-8444-555555555555";
const OTHER = "99999999-2222-4333-8444-555555555555";

const ok = <T,>(data: T): DesktopResult<T> => ({ ok: true, data });
const fail = (message: string, code = "x"): DesktopResult<never> => ({
  ok: false,
  error: { kind: "invalid_input", code, message },
});

function bridge() {
  return {
    app: {},
    auth: { logout: vi.fn(async () => ok(undefined)) },
    sessions: {
      questions: vi.fn(async () => ok([])),
      list: vi.fn(async () => ok([])),
      startQuestion: vi.fn(async () => ok(undefined)),
      cancelQuestion: vi.fn(async () => ok(undefined)),
    },
    capture: {
      listDisplays: vi.fn(async () =>
        ok([{ id: "1", label: "Display 1 (primary) · 1920×1080", primary: true, width: 1920, height: 1080 }]),
      ),
      start: vi.fn(async () => ok(undefined)),
      cancel: vi.fn(async () => ok(undefined)),
      extract: vi.fn(async () => ok({ extractionId: "55555555-2222-4333-8444-555555555555", text: "What is a mutex?", truncated: false })),
    },
    onStreamMessage: () => () => undefined,
    onCaptureEvent: () => () => undefined,
    onSessionExpired: () => () => undefined,
  };
}

let desktop: ReturnType<typeof bridge>;
const state = () => useDesktopStore.getState();
const send = (event: CaptureEvent) => state().handleCaptureEvent(event);

const heldEvent: CaptureEvent = {
  type: "captured",
  captureId: "33333333-2222-4333-8444-555555555555",
  previewDataUrl: "data:image/png;base64,THUMB",
  width: 1920,
  height: 1080,
  bytes: 2048,
};

beforeEach(() => {
  desktop = bridge();
  vi.stubGlobal("window", { desktop });
  vi.stubGlobal("crypto", { randomUUID: () => "44444444-2222-4333-8444-555555555555" });
  useDesktopStore.setState({
    selectedSessionId: SESSION,
    sessions: [],
    history: [],
    liveAnswer: null,
    capture: { phase: "idle" },
    captureNotice: null,
    error: null,
  });
});

async function toHeld() {
  await state().openCapture();
  await state().startCapture("1");
  send({ type: "countdown", seconds: 3, displayLabel: "Display 1" });
  send(heldEvent);
}

describe("capture flow states", () => {
  it("walks idle → choosing → countdown → held, with the preview and no automatic send", async () => {
    await state().openCapture();
    expect(state().capture).toMatchObject({ phase: "choosing" });
    expect(desktop.capture.start).not.toHaveBeenCalled();

    await state().startCapture("1");
    expect(desktop.capture.start).toHaveBeenCalledWith("1", SESSION);
    send({ type: "countdown", seconds: 3, displayLabel: "Display 1" });
    expect(state().capture).toEqual({ phase: "countdown", seconds: 3, displayLabel: "Display 1" });
    send({ type: "countdown", seconds: 2, displayLabel: "Display 1" });
    expect(state().capture).toMatchObject({ phase: "countdown", seconds: 2 });

    send(heldEvent);
    expect(state().capture).toMatchObject({ phase: "held", previewDataUrl: "data:image/png;base64,THUMB" });
    expect(desktop.capture.extract).not.toHaveBeenCalled(); // explicit confirmation required
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
  });

  it("refuses to open the picker without a selected session", async () => {
    useDesktopStore.setState({ selectedSessionId: null });
    await state().openCapture();
    expect(state().capture.phase).toBe("idle");
    expect(state().captureNotice).toMatch(/Select a session/);
    expect(desktop.capture.listDisplays).not.toHaveBeenCalled();
    await state().startCapture("1");
    expect(desktop.capture.start).not.toHaveBeenCalled();
  });

  it("shows the main process's refusal as a plain notice and stays idle", async () => {
    desktop.capture.start.mockResolvedValueOnce(fail("The assistant window must be visible and focused to capture a screenshot."));
    await state().openCapture();
    await state().startCapture("1");
    expect(state().capture.phase).toBe("idle");
    expect(state().captureNotice).toBe("The assistant window must be visible and focused to capture a screenshot.");
  });

  it("ignores a late 'captured' event when no countdown is running", () => {
    send(heldEvent);
    expect(state().capture.phase).toBe("idle");
  });

  it("a cancelled event returns to idle and explains why, except for the user's own cancel", () => {
    send({ type: "countdown", seconds: 2, displayLabel: "Display 1" });
    send({ type: "cancelled", reason: "window", message: "The assistant window was hidden, so the screenshot was cancelled." });
    expect(state().capture.phase).toBe("idle");
    expect(state().captureNotice).toMatch(/hidden/);
    send({ type: "cancelled", reason: "user", message: "Screenshot cancelled." });
    expect(state().captureNotice).toBeNull();
  });
});

describe("cancellation at every stage reaches the main process and clears local state", () => {
  it.each([
    ["choosing", async () => state().openCapture()],
    ["countdown", async () => {
      await state().openCapture();
      await state().startCapture("1");
      send({ type: "countdown", seconds: 3, displayLabel: "Display 1" });
    }],
    ["held", toHeld],
  ])("cancel while %s", async (_name, arrange) => {
    await arrange();
    await state().cancelCapture();
    expect(state().capture).toEqual({ phase: "idle" });
    expect(desktop.capture.cancel).toHaveBeenCalledTimes(1);
    expect(desktop.capture.extract).not.toHaveBeenCalled();
  });

  it("cancel while the image is being sent ignores a late result", async () => {
    let finish: (value: DesktopResult<{ extractionId: string; text: string; truncated: boolean }>) => void = () => undefined;
    desktop.capture.extract.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)));
    await toHeld();
    const sending = state().sendCapture();
    expect(state().capture).toEqual({ phase: "extracting" });
    await state().cancelCapture();
    finish(ok({ extractionId: "55555555-2222-4333-8444-555555555555", text: "late", truncated: false }));
    await sending;
    expect(state().capture).toEqual({ phase: "idle" });
  });

  it("discarding the review text returns to idle and nothing is asked", async () => {
    await toHeld();
    await state().sendCapture();
    await state().cancelCapture();
    expect(state().capture).toEqual({ phase: "idle" });
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
  });

  it("changing session cancels a pending screenshot first", async () => {
    await toHeld();
    await state().selectSession(OTHER);
    expect(desktop.capture.cancel).toHaveBeenCalled();
    expect(state().capture).toEqual({ phase: "idle" });
    expect(state().selectedSessionId).toBe(OTHER);
  });

  it("signing out and session expiry clear the screenshot state", async () => {
    await toHeld();
    await state().logout();
    expect(state().capture).toEqual({ phase: "idle" });
    expect(desktop.capture.cancel).toHaveBeenCalled();
    await toHeld();
    state().expireSession();
    expect(state().capture).toEqual({ phase: "idle" });
  });
});

describe("preview, explicit send, edit and explicit Ask", () => {
  it("drops the preview as soon as the image is sent and shows editable text", async () => {
    await toHeld();
    await state().sendCapture();
    expect(desktop.capture.extract).toHaveBeenCalledWith(heldEvent.captureId);
    expect(state().capture).toEqual({
      phase: "review",
      extractionId: "55555555-2222-4333-8444-555555555555",
      text: "What is a mutex?",
      truncated: false,
    });
    expect(JSON.stringify(state().capture)).not.toContain("THUMB"); // no image data remains
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled(); // nothing is asked yet
  });

  it("a failed extraction returns to idle with the safe message and keeps no image", async () => {
    desktop.capture.extract.mockResolvedValueOnce(fail("Screenshot input is not available on this server.", "vision_unavailable"));
    await toHeld();
    await state().sendCapture();
    expect(state().capture).toEqual({ phase: "idle" });
    expect(state().captureNotice).toBe("Screenshot input is not available on this server.");
    expect(JSON.stringify(state())).not.toContain("THUMB");
  });

  it("Ask uses the edited text and the extraction id, through the existing question flow", async () => {
    await toHeld();
    await state().sendCapture();
    state().setReviewText("  What is a mutex, briefly?  ");
    await state().askFromScreenshot();
    expect(desktop.sessions.startQuestion).toHaveBeenCalledTimes(1);
    expect(desktop.sessions.startQuestion).toHaveBeenCalledWith(
      "44444444-2222-4333-8444-555555555555",
      SESSION,
      "What is a mutex, briefly?",
      "55555555-2222-4333-8444-555555555555",
    );
    expect(state().capture).toEqual({ phase: "idle" });
    expect(state().liveAnswer).toMatchObject({ question: "What is a mutex, briefly?" });
  });

  it("will not ask empty text", async () => {
    await toHeld();
    await state().sendCapture();
    state().setReviewText("   ");
    await state().askFromScreenshot();
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
    expect(state().capture.phase).toBe("review");
    expect(state().captureNotice).toMatch(/no text/);
  });

  it("an empty extraction lets the user type, and Ask is still explicit", async () => {
    desktop.capture.extract.mockResolvedValueOnce(ok({ extractionId: "55555555-2222-4333-8444-555555555555", text: "", truncated: false }));
    await toHeld();
    await state().sendCapture();
    expect(state().capture).toMatchObject({ phase: "review", text: "" });
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
  });
});

describe("the typed question flow is unchanged", () => {
  it("passes no extraction id and uses exactly the original three arguments", async () => {
    await state().ask("Tell me about yourself");
    expect(desktop.sessions.startQuestion).toHaveBeenCalledTimes(1);
    expect(desktop.sessions.startQuestion.mock.calls[0]).toEqual([
      "44444444-2222-4333-8444-555555555555",
      SESSION,
      "Tell me about yourself",
    ]);
  });

  it("still requires a selected session", async () => {
    useDesktopStore.setState({ selectedSessionId: null });
    await state().ask("Hello");
    expect(desktop.sessions.startQuestion).not.toHaveBeenCalled();
    expect(state().error).toBe("Select a session before asking a question.");
  });
});
