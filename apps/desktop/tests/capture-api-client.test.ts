import { describe, expect, it, vi } from "vitest";
import { DesktopApiClient } from "../src/main/api-client";

const SESSION = "00000000-0000-4000-8000-000000000002";
const user = { id: "00000000-0000-4000-8000-000000000001", email: "p@example.com", created_at: "2026-01-01T00:00:00Z" };

class MemoryStore {
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

const tokens = (access: string, refresh = "r1") =>
  new Response(JSON.stringify({ access_token: access, expires_in: 900, user }), {
    status: 200,
    headers: { "Content-Type": "application/json", "Set-Cookie": `refresh_token=${refresh}; HttpOnly; Path=/api/v1/auth` },
  });

const error = (status: number, code: string, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify({ error: { code, message: `safe ${code}` } }), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });

const ok = (text = "What is a mutex?", truncated = false) =>
  new Response(JSON.stringify({ extracted_text: text, truncated, provider: "fake", model: "fake-vision", latency_ms: 3 }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

async function signedIn(fetcher: ReturnType<typeof vi.fn<typeof fetch>>) {
  fetcher.mockResolvedValueOnce(tokens("access-1"));
  const client = new DesktopApiClient("http://127.0.0.1:8000", new MemoryStore(), fetcher);
  await client.login("p@example.com", "long-password-1");
  fetcher.mockClear();
  return client;
}

describe("extractScreenshot (Phase 11A endpoint)", () => {
  it("posts the PNG as a raw image/png body with the bearer token, never as multipart", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(ok("Explain closures", true));
    const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);
    const result = await client.extractScreenshot(SESSION, png, new AbortController().signal);
    expect(result).toEqual({ text: "Explain closures", truncated: true });
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, init] = fetcher.mock.calls[0] ?? [];
    expect(String(url)).toBe(`http://127.0.0.1:8000/api/v1/sessions/${SESSION}/screenshots`);
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ Authorization: "Bearer access-1", "Content-Type": "image/png" });
    expect(init?.body).toBe(png);
    expect(init?.body).not.toBeInstanceOf(FormData);
    expect(init?.cache).toBe("no-store");
  });

  it("refreshes once on 401 and resends the same image", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher
      .mockResolvedValueOnce(error(401, "not_authenticated"))
      .mockResolvedValueOnce(tokens("access-2", "r2"))
      .mockResolvedValueOnce(ok());
    const png = Buffer.from([1, 2, 3, 4]);
    await client.extractScreenshot(SESSION, png, new AbortController().signal);
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(fetcher.mock.calls[2]?.[1]?.headers).toMatchObject({ Authorization: "Bearer access-2" });
    expect(fetcher.mock.calls[2]?.[1]?.body).toBe(png);
  });

  it.each([
    [413, "screenshot_too_large"],
    [415, "unsupported_image_type"],
    [422, "invalid_image"],
    [404, "session_not_found"],
    [409, "session_not_ready"],
    [503, "vision_unavailable"],
    [504, "llm_timeout"],
  ])("preserves the backend's safe error for %s %s", async (status, code) => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(error(status, code));
    await expect(client.extractScreenshot(SESSION, Buffer.from([1]), new AbortController().signal)).rejects.toMatchObject({
      details: { kind: "http", status, code, message: `safe ${code}` },
    });
  });

  it("keeps the retry hint of a rate limit", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(error(429, "rate_limited", { "Retry-After": "7" }));
    await expect(client.extractScreenshot(SESSION, Buffer.from([1]), new AbortController().signal)).rejects.toMatchObject({
      details: { status: 429, code: "rate_limited", retryAfterSeconds: 7 },
    });
  });

  it("returns a generic network error that reveals nothing", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockRejectedValueOnce(new TypeError("connect ECONNREFUSED 10.1.2.3:8000"));
    await expect(client.extractScreenshot(SESSION, Buffer.from([1]), new AbortController().signal)).rejects.toMatchObject({
      details: { kind: "network", code: "network_error", message: "Could not reach the backend. Check your connection and try again." },
    });
  });

  it("rejects a malformed success body", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ extracted_text: 5 }), { status: 200 }));
    await expect(client.extractScreenshot(SESSION, Buffer.from([1]), new AbortController().signal)).rejects.toMatchObject({
      details: { code: "bad_response" },
    });
  });

  it("is aborted by the signal", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockImplementationOnce(
      (_url, init) =>
        new Promise((_resolve, reject) =>
          init?.signal?.addEventListener("abort", () => reject(Object.assign(new Error("a"), { name: "AbortError" }))),
        ),
    );
    const controller = new AbortController();
    const pending = client.extractScreenshot(SESSION, Buffer.from([1]), controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("refuses without a signed-in user and never calls the network", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = new DesktopApiClient("http://127.0.0.1:8000", new MemoryStore(), fetcher);
    await expect(client.extractScreenshot(SESSION, Buffer.from([1]), new AbortController().signal)).rejects.toMatchObject({
      details: { status: 401 },
    });
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("question requests", () => {
  const done =
    'event: answer.done\ndata: {"answer_id":"a1","question_id":"q1","provider":"fake","model":"m","metrics":{"retrieval_ms":1,"first_token_ms":2,"total_ms":3},"fallback_used":false,"input_tokens":1,"output_tokens":1}\n\n';
  const stream = () =>
    new Response(new TextEncoder().encode(done), { status: 200, headers: { "Content-Type": "text/event-stream" } });

  it("typed questions keep exactly the original body", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(stream());
    await client.streamQuestion(SESSION, "Typed", new AbortController().signal, () => undefined);
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({ text: "Typed" });
  });

  it("only a screenshot flow adds input_source, also after a token refresh", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher
      .mockResolvedValueOnce(error(401, "not_authenticated"))
      .mockResolvedValueOnce(tokens("access-2", "r2"))
      .mockResolvedValueOnce(stream());
    await client.streamQuestion(SESSION, "From screen", new AbortController().signal, () => undefined, "screenshot");
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({ text: "From screen", input_source: "screenshot" });
    expect(JSON.parse(String(fetcher.mock.calls[2]?.[1]?.body))).toEqual({ text: "From screen", input_source: "screenshot" });
  });
});
