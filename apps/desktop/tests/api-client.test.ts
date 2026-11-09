import { describe, expect, it, vi } from "vitest";
import { DesktopApiClient, DesktopApiError, normalizeBackendUrl } from "../src/main/api-client";
import type { DesktopUser } from "../src/shared/contracts";

class MemoryRefreshStore {
  token: string | null = null;
  load() {
    return this.token;
  }
  save(token: string) {
    this.token = token;
  }
  clear() {
    this.token = null;
  }
}

const user: DesktopUser = {
  id: "00000000-0000-4000-8000-000000000001",
  email: "person@example.com",
  created_at: "2026-01-01T00:00:00Z",
};

function tokenResponse(token: string, refresh = "refresh-token-1"): Response {
  return new Response(
    JSON.stringify({ access_token: token, expires_in: 900, user }),
    {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "Set-Cookie": `refresh_token=${refresh}; HttpOnly; Path=/api/v1/auth`,
      },
    },
  );
}

function eventStream(chunks: string[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

const terminalFrame =
  'event: answer.done\ndata: {"answer_id":"a1","question_id":"q1","provider":"fake","model":"test","metrics":{"retrieval_ms":2,"first_token_ms":4,"total_ms":8},"fallback_used":false,"input_tokens":10,"output_tokens":5}\n\n';

describe("backend URL policy", () => {
  it("allows local HTTP and production HTTPS origins", () => {
    expect(normalizeBackendUrl("http://localhost:8000/")).toBe("http://localhost:8000");
    expect(normalizeBackendUrl("https://api.example.com")).toBe("https://api.example.com");
  });

  it.each([
    "http://192.168.1.10:8000",
    "https://user:pass@example.com",
    "https://api.example.com/base",
    "file:///tmp/api",
  ])("rejects unsafe backend URL %s", (url) => {
    expect(() => normalizeBackendUrl(url)).toThrow(DesktopApiError);
  });
});

describe("DesktopApiClient", () => {
  it("keeps refresh credentials in the secure store and refreshes on app startup", async () => {
    const store = new MemoryRefreshStore();
    const fetcher = vi.fn<typeof fetch>();
    fetcher.mockResolvedValueOnce(tokenResponse("access-1"));
    const client = new DesktopApiClient("http://127.0.0.1:8000", store, fetcher);

    await client.login("person@example.com", "long-password");
    expect(store.token).toBe("refresh-token-1");

    const restarted = new DesktopApiClient("http://127.0.0.1:8000", store, fetcher);
    fetcher.mockResolvedValueOnce(tokenResponse("access-2", "refresh-token-2"));
    await expect(restarted.initialize()).resolves.toEqual(user);
    expect(store.token).toBe("refresh-token-2");
    expect(fetcher.mock.calls[1]?.[1]?.headers).toEqual({ Cookie: "refresh_token=refresh-token-1" });
  });

  it("returns an explicit safe network error when the backend cannot be reached", async () => {
    const client = new DesktopApiClient(
      "http://127.0.0.1:8000",
      new MemoryRefreshStore(),
      vi.fn<typeof fetch>().mockRejectedValue(new TypeError("socket detail")),
    );
    await expect(client.checkBackend()).rejects.toMatchObject({
      details: {
        kind: "network",
        code: "network_error",
        message: "Could not reach the backend. Check your connection and try again.",
      },
    });
  });

  it("retries one unauthorized Q&A request after rotating the refresh token", async () => {
    const fetcher = vi.fn<typeof fetch>();
    fetcher
      .mockResolvedValueOnce(tokenResponse("access-1"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { code: "not_authenticated", message: "No." } }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(tokenResponse("access-2", "refresh-token-2"))
      .mockResolvedValueOnce(eventStream([terminalFrame]));
    const client = new DesktopApiClient("http://localhost:8000", new MemoryRefreshStore(), fetcher);
    await client.login("person@example.com", "long-password");
    const received: string[] = [];

    await client.streamQuestion(
      "00000000-0000-4000-8000-000000000002",
      "Tell me about your experience",
      new AbortController().signal,
      (event) => received.push(event.type),
    );

    expect(received).toEqual(["answer.done"]);
    expect(fetcher).toHaveBeenCalledTimes(4);
    expect(fetcher.mock.calls[3]?.[1]?.headers).toMatchObject({
      Authorization: "Bearer access-2",
      Accept: "text/event-stream",
    });
  });

  it("preserves safe API error codes and retry hints", async () => {
    const fetcher = vi.fn<typeof fetch>();
    fetcher
      .mockResolvedValueOnce(tokenResponse("access-1"))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ error: { code: "rate_limited", message: "Please wait." } }),
          { status: 429, headers: { "Content-Type": "application/json", "Retry-After": "4" } },
        ),
      );
    const client = new DesktopApiClient("http://localhost:8000", new MemoryRefreshStore(), fetcher);
    await client.login("person@example.com", "long-password");

    await expect(client.listSessions()).rejects.toMatchObject({
      details: { status: 429, code: "rate_limited", retryAfterSeconds: 4 },
    });
  });

  it("parses incremental SSE chunks in order and rejects incomplete streams", async () => {
    const fetcher = vi.fn<typeof fetch>();
    fetcher
      .mockResolvedValueOnce(tokenResponse("access-1"))
      .mockResolvedValueOnce(
        eventStream([
          'event: answer.delta\ndata: {"answer_id":"a1","index":0,',
          '"text":"Hello"}\n\n',
          terminalFrame,
        ]),
      );
    const client = new DesktopApiClient("http://localhost:8000", new MemoryRefreshStore(), fetcher);
    await client.login("person@example.com", "long-password");
    const received: string[] = [];

    await client.streamQuestion("00000000-0000-4000-8000-000000000002", "Question", new AbortController().signal, (event) => {
      received.push(event.type);
    });

    expect(received).toEqual(["answer.delta", "answer.done"]);
  });

  it("propagates cancellation instead of disguising it as a network failure", async () => {
    const controller = new AbortController();
    const fetcher = vi.fn<typeof fetch>();
    fetcher
      .mockResolvedValueOnce(tokenResponse("access-1"))
      .mockImplementationOnce(async (_input, init) => {
        expect(init?.signal).toBe(controller.signal);
        throw new DOMException("Aborted", "AbortError");
      });
    const client = new DesktopApiClient("http://localhost:8000", new MemoryRefreshStore(), fetcher);
    await client.login("person@example.com", "long-password");
    controller.abort();

    await expect(
      client.streamQuestion(
        "00000000-0000-4000-8000-000000000002",
        "Question",
        controller.signal,
        () => undefined,
      ),
    ).rejects.toMatchObject({ name: "AbortError" });
  });

  it("accepts a terminal provider error event without inventing answer text", async () => {
    const fetcher = vi.fn<typeof fetch>();
    fetcher
      .mockResolvedValueOnce(tokenResponse("access-1"))
      .mockResolvedValueOnce(
        eventStream([
          'event: answer.error\ndata: {"answer_id":"a1","question_id":"q1","code":"llm_unavailable","retryable":true}\n\n',
        ]),
      );
    const client = new DesktopApiClient("http://localhost:8000", new MemoryRefreshStore(), fetcher);
    await client.login("person@example.com", "long-password");
    const received: string[] = [];

    await client.streamQuestion(
      "00000000-0000-4000-8000-000000000002",
      "Question",
      new AbortController().signal,
      (event) => received.push(event.type),
    );
    expect(received).toEqual(["answer.error"]);
  });
});
