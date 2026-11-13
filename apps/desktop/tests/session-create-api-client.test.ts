import { describe, expect, it, vi } from "vitest";
import { DesktopApiClient } from "../src/main/api-client";

const user = { id: "00000000-0000-4000-8000-000000000001", email: "p@example.com", created_at: "2026-01-01T00:00:00Z" };

class MemoryStore {
  token: string | null = null;
  load() { return this.token; }
  save(token: string) { this.token = token; }
  clear() { this.token = null; }
}

const tokens = (access: string, refresh = "r1") =>
  new Response(JSON.stringify({ access_token: access, expires_in: 900, user }), {
    status: 200,
    headers: { "Content-Type": "application/json", "Set-Cookie": `refresh_token=${refresh}; HttpOnly; Path=/api/v1/auth` },
  });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function signedIn(fetcher: ReturnType<typeof vi.fn<typeof fetch>>) {
  fetcher.mockResolvedValueOnce(tokens("access-1"));
  const client = new DesktopApiClient("http://127.0.0.1:8000", new MemoryStore(), fetcher);
  await client.login("p@example.com", "long-password-1");
  fetcher.mockClear();
  return client;
}

const item = (id: string, ingestion: unknown, indexing: unknown) => ({
  id, filename: `${id}.pdf`, content_type: "application/pdf", size_bytes: 10, created_at: "2026-01-01T00:00:00Z", ingestion, indexing,
});

describe("listFiles readiness", () => {
  it.each([
    ["both completed", { status: "completed" }, { status: "completed" }, "ready"],
    ["indexing still running", { status: "completed" }, { status: "processing" }, "processing"],
    ["extraction pending", { status: "pending" }, null, "processing"],
    ["no jobs yet", null, null, "processing"],
    ["extraction failed", { status: "failed" }, { status: "pending" }, "failed"],
    ["indexing failed", { status: "completed" }, { status: "failed" }, "failed"],
  ])("%s -> %s", async (_name, ingestion, indexing, expected) => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(json({ items: [item("a", ingestion, indexing)] }));
    const [first] = await client.listFiles("resumes");
    expect(first?.status).toBe(expected);
    expect(String(fetcher.mock.calls[0]?.[0])).toBe("http://127.0.0.1:8000/api/v1/resumes?limit=200");
    expect(fetcher.mock.calls[0]?.[1]?.headers).toEqual({ Authorization: "Bearer access-1" });
  });

  it("returns only identifying and status fields", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(json({ items: [{ ...item("a", { status: "completed" }, { status: "completed" }), storage_key: "secret", sha256: "x" }] }));
    const [first] = await client.listFiles("documents");
    expect(Object.keys(first ?? {}).sort()).toEqual(["created_at", "filename", "id", "size_bytes", "status"]);
  });
});

describe("getSessionOptions", () => {
  it("returns the five value lists and provider ids/labels, and ignores anything else", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(json({ providers: [{ id: "x" }], languages: ["en", 5, "hi"], question_types: ["auto"], formats: [], lengths: ["short"], tones: "bad" }));
    expect(await client.getSessionOptions()).toEqual({
      languages: ["en", "hi"], question_types: ["auto"], formats: [], lengths: ["short"], tones: [],
      providers: [{ id: "x", label: "x" }], voice_providers: [],
    });
  });

  it("reduces providers to id and label, dropping models, capabilities and any extra field", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(
      json({
        providers: [
          { id: "groq", api_key: "SECRET", base_url: "https://x", models: [{ id: "m", label: "Groq (m)", capabilities: ["streaming"], languages: ["en"] }] },
          { id: 5 }, { id: "" }, null, "bad",
        ],
        voice_providers: [{ id: "sarvam", models: [{ id: "s", label: "Sarvam (s)" }] }],
        languages: [], question_types: [], formats: [], lengths: [], tones: [],
      }),
    );
    const result = await client.getSessionOptions();
    expect(result.providers).toEqual([{ id: "groq", label: "Groq (m)" }]);
    expect(result.voice_providers).toEqual([{ id: "sarvam", label: "Sarvam (s)" }]);
    expect(JSON.stringify(result)).not.toMatch(/SECRET|base_url|capabilities/);
  });
});

describe("createSession", () => {
  const body = { type: "interview", company_name: "Acme", job_description: "Role", resume_id: "r", document_ids: [], preferences: {} };

  it("posts JSON with the bearer token and returns the summary only", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(json({ id: "s1", type: "interview", title: null, company_name: "Acme", status: "ready", created_at: "c", updated_at: "u", resume: { id: "r", filename: "cv.pdf" }, document_count: 1, job_description: "SECRET", preferences: { x: 1 } }, 201));
    const created = await client.createSession(body);
    expect(created).toEqual({ id: "s1", type: "interview", title: null, company_name: "Acme", status: "ready", created_at: "c", updated_at: "u", resume: { id: "r", filename: "cv.pdf" }, document_count: 1 });
    const [url, init] = fetcher.mock.calls[0] ?? [];
    expect(String(url)).toBe("http://127.0.0.1:8000/api/v1/sessions");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ Authorization: "Bearer access-1", "Content-Type": "application/json" });
    expect(JSON.parse(String(init?.body))).toEqual(body);
  });

  it("refreshes once on 401 and resends the same body", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher
      .mockResolvedValueOnce(json({ error: { code: "not_authenticated", message: "No." } }, 401))
      .mockResolvedValueOnce(tokens("access-2", "r2"))
      .mockResolvedValueOnce(json({ id: "s1", status: "ready", company_name: "Acme", type: "interview", title: null, created_at: "c", updated_at: "u", resume: { id: "r", filename: "f" }, document_count: 0 }, 201));
    await client.createSession(body);
    expect(fetcher.mock.calls[2]?.[1]?.headers).toMatchObject({ Authorization: "Bearer access-2" });
    expect(JSON.parse(String(fetcher.mock.calls[2]?.[1]?.body))).toEqual(body);
  });

  it("caps and filters validation hints and never copies anything else", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    const details = [
      ...Array.from({ length: 15 }, (_, i) => ({ field: `f${i}`, message: "m".repeat(500) })),
      { field: 1, message: "bad" },
      "junk",
    ];
    fetcher.mockResolvedValueOnce(json({ error: { code: "validation_error", message: "The request is invalid.", details } }, 422));
    const error = await client.createSession(body).catch((e) => e);
    expect(error.details.fieldErrors).toHaveLength(10);
    expect(error.details.fieldErrors[0].message).toHaveLength(200);
  });

  it("does not attach field hints to other errors", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(json({ error: { code: "material_not_ready", message: "fixed", details: [{ field: "x", message: "y" }] } }, 422));
    const error = await client.createSession(body).catch((e) => e);
    expect(error.details.fieldErrors).toBeUndefined();
  });

  it("rejects a malformed success response and refuses without a sign-in", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = await signedIn(fetcher);
    fetcher.mockResolvedValueOnce(json({ nope: true }, 201));
    await expect(client.createSession(body)).rejects.toMatchObject({ details: { code: "bad_response" } });
    const anonymous = new DesktopApiClient("http://127.0.0.1:8000", new MemoryStore(), vi.fn<typeof fetch>());
    await expect(anonymous.createSession(body)).rejects.toMatchObject({ details: { status: 401 } });
  });
});
