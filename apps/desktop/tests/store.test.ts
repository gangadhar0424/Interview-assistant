import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StreamMessage } from "../src/shared/contracts";
import { useDesktopStore } from "../src/renderer/src/store";

function fakeBridge() {
  return {
    app: {},
    auth: {},
    sessions: {
      questions: async () => ({ ok: true, data: [] }),
    },
    onStreamMessage: () => () => undefined,
    onSessionExpired: () => () => undefined,
  };
}

describe("live answer state", () => {
  beforeEach(() => {
    vi.stubGlobal("window", { desktop: fakeBridge() });
    useDesktopStore.setState({
      liveAnswer: {
        requestId: "request",
        question: "Question?",
        phase: "sending",
        text: "",
        questionId: null,
        answerId: null,
        provider: null,
        model: null,
        metrics: null,
        deltaCount: 0,
        errorMessage: null,
      },
      selectedSessionId: "session",
      history: [],
    });
  });

  it("keeps token ordering, ignores duplicates and absorbs late events after completion", () => {
    const onMessage = useDesktopStore.getState().handleStreamMessage;
    const created: StreamMessage = {
      requestId: "request",
      event: { type: "question.created", questionId: "question", answerId: "answer" },
    };
    onMessage(created);
    onMessage({
      requestId: "request",
      event: { type: "answer.delta", answerId: "answer", index: 0, text: "Hello" },
    });
    onMessage({
      requestId: "request",
      event: { type: "answer.delta", answerId: "answer", index: 0, text: "duplicate" },
    });
    onMessage({
      requestId: "request",
      event: {
        type: "answer.done",
        answerId: "answer",
        questionId: "question",
        provider: "fake",
        model: "test",
        metrics: { retrieval_ms: 1, first_token_ms: 2, total_ms: 3 },
        fallbackUsed: false,
        inputTokens: 4,
        outputTokens: 5,
      },
    });
    onMessage({
      requestId: "request",
      event: { type: "answer.delta", answerId: "answer", index: 1, text: " late" },
    });
    const answer = useDesktopStore.getState().liveAnswer;
    expect(answer?.text).toBe("Hello");
    expect(answer?.phase).toBe("done");
  });

  it("turns an out-of-order delta into an explicit stream failure", () => {
    const onMessage = useDesktopStore.getState().handleStreamMessage;
    onMessage({
      requestId: "request",
      event: { type: "question.created", questionId: "question", answerId: "answer" },
    });
    onMessage({
      requestId: "request",
      event: { type: "answer.delta", answerId: "answer", index: 2, text: "out of order" },
    });
    expect(useDesktopStore.getState().liveAnswer?.phase).toBe("failed");
    expect(useDesktopStore.getState().liveAnswer?.errorMessage).toContain("out of order");
  });

  it("ignores events from other requests", () => {
    useDesktopStore.getState().handleStreamMessage({
      requestId: "other",
      event: { type: "answer.delta", answerId: "answer", index: 0, text: "wrong" },
    });
    expect(useDesktopStore.getState().liveAnswer?.text).toBe("");
  });

  it("reports history IPC failures without leaking an unhandled rejection", async () => {
    vi.stubGlobal("window", {
      desktop: {
        ...fakeBridge(),
        sessions: { questions: async () => { throw new Error("backend detail"); } },
      },
    });
    await expect(useDesktopStore.getState().loadHistory()).resolves.toBeUndefined();
    expect(useDesktopStore.getState().error).toBe("backend detail");
  });

  it("does not report settings as saved when the IPC update fails", async () => {
    const updated = await useDesktopStore.getState().updateSettings({ opacity: 0.8 });
    expect(updated).toBe(false);
    expect(useDesktopStore.getState().error).toBe("window.desktop.app.updateSettings is not a function");
  });
});
