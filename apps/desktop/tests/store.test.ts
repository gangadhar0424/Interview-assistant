import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StreamMessage } from "../src/shared/contracts";
import { useDesktopStore } from "../src/renderer/src/store";

function fakeBridge() {
  return {
    app: {},
    auth: {},
    sessions: {
      questions: async () => ({ ok: true, data: [] }),
      createOptions: async () => ({
        ok: true,
        data: {
          resumes: [],
          documents: [],
          options: { providers: [{ id: "groq", label: "Groq" }], voice_providers: [] },
        },
      }),
      setReasoning: async () => ({ ok: true, data: undefined }),
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
        errorCode: null,
      },
      selectedSessionId: "session",
      history: [],
      answerProvider: { choices: null, saving: false, notice: null },
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

  it("a provider failure shows a clear message and offers other answer providers", async () => {
    useDesktopStore.getState().handleStreamMessage({
      requestId: "request",
      event: { type: "answer.error", answerId: "a", questionId: "q", code: "llm_quota_exhausted", retryable: false },
    });
    const live = useDesktopStore.getState().liveAnswer;
    expect(live?.phase).toBe("failed");
    expect(live?.errorCode).toBe("llm_quota_exhausted");
    expect(live?.errorMessage).toMatch(/no credit or quota/i); // not the misleading "busy" text
    await vi.waitFor(() =>
      expect(useDesktopStore.getState().answerProvider.choices).toEqual([{ id: "groq", label: "Groq" }]),
    );
  });

  it("a timeout or refused question does not offer a provider switch", async () => {
    useDesktopStore.getState().handleStreamMessage({
      requestId: "request",
      event: { type: "answer.error", answerId: "a", questionId: "q", code: "llm_timeout", retryable: true },
    });
    await Promise.resolve();
    expect(useDesktopStore.getState().answerProvider.choices).toBeNull();
  });

  it("saving an answer provider clears the offer and asks the user to ask again", async () => {
    const setReasoning = vi.fn(async () => ({ ok: true as const, data: undefined }));
    vi.stubGlobal("window", { desktop: { ...fakeBridge(), sessions: { ...fakeBridge().sessions, setReasoning } } });
    useDesktopStore.setState({ answerProvider: { choices: [{ id: "groq", label: "Groq" }], saving: false, notice: null } });
    await useDesktopStore.getState().saveAnswerProvider("groq");
    expect(setReasoning).toHaveBeenCalledWith("session", "groq");
    const state = useDesktopStore.getState().answerProvider;
    expect(state.choices).toBeNull();
    expect(state.saving).toBe(false);
    expect(state.notice).toMatch(/ask your question again/i);
  });
});
