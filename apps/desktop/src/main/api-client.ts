import type {
  AnswerItem,
  DesktopError,
  DesktopSettings,
  DesktopUser,
  QuestionItem,
  QuestionPage,
  QaEvent,
  SessionList,
  SessionSummary,
} from "../shared/contracts";
import { parseSseEvent, SseParser } from "./sse";

type TokenResponse = { access_token: string; expires_in: number; user: DesktopUser };
type RefreshTokenStore = {
  load(): string | null;
  save(token: string): void;
  clear(): void;
};
type Fetcher = typeof fetch;

export class DesktopApiError extends Error {
  constructor(
    readonly details: DesktopError,
  ) {
    super(details.message);
    this.name = "DesktopApiError";
  }
}

type ErrorEnvelope = { error?: { code?: string; message?: string } };

function isLoopback(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";
}

export function normalizeBackendUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    throw new DesktopApiError({
      kind: "invalid_input",
      code: "invalid_backend_url",
      message: "Enter a valid backend URL.",
    });
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== "/" ||
    (url.protocol === "http:" && !isLoopback(url.hostname))
  ) {
    throw new DesktopApiError({
      kind: "invalid_input",
      code: "invalid_backend_url",
      message: "Use HTTPS, or HTTP on localhost for development.",
    });
  }
  return url.origin;
}

function errorFromResponse(response: Response, payload: unknown): DesktopApiError {
  const error = (payload as ErrorEnvelope | null)?.error;
  const retry = Number(response.headers.get("Retry-After"));
  return new DesktopApiError({
    kind: "http",
    status: response.status,
    code: error?.code ?? "unknown_error",
    message: error?.message ?? "The server could not complete the request.",
    ...(Number.isFinite(retry) && retry > 0 ? { retryAfterSeconds: Math.ceil(retry) } : {}),
  });
}

async function responseError(response: Response): Promise<DesktopApiError> {
  const payload: unknown = await response.json().catch(() => null);
  return errorFromResponse(response, payload);
}

function getRefreshCookie(response: Response): string | null {
  const lines =
    typeof response.headers.getSetCookie === "function"
      ? response.headers.getSetCookie()
      : [response.headers.get("set-cookie") ?? ""];
  for (const line of lines) {
    const match = /^refresh_token=([^;]*)/.exec(line);
    if (match) return match[1] || null;
  }
  return null;
}

function apiUrl(baseUrl: string, path: string): string {
  if (!path.startsWith("/api/v1/") && path !== "/health") {
    throw new DesktopApiError({
      kind: "invalid_input",
      code: "invalid_api_path",
      message: "The requested API path is not available.",
    });
  }
  return `${normalizeBackendUrl(baseUrl)}${path}`;
}

export class DesktopApiClient {
  private accessToken: string | null = null;
  private user: DesktopUser | null = null;
  private refreshToken: string | null = null;
  private origin: string;

  constructor(
    baseUrl: string,
    private readonly refreshTokens: RefreshTokenStore,
    private readonly fetcher: Fetcher = fetch,
  ) {
    this.origin = normalizeBackendUrl(baseUrl);
  }

  updateBaseUrl(baseUrl: string): void {
    const next = normalizeBackendUrl(baseUrl);
    if (next !== this.origin) {
      this.clearSession();
      this.origin = next;
    }
  }

  async checkBackend(): Promise<void> {
    const response = await this.request("/health");
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok || (body as { status?: string } | null)?.status !== "ok") {
      throw new DesktopApiError({
        kind: "network",
        code: "backend_unavailable",
        status: response.status,
        message: "The backend is unavailable. Check the URL and try again.",
      });
    }
  }

  async initialize(): Promise<DesktopUser | null> {
    this.refreshToken = this.refreshTokens.load();
    if (!this.refreshToken) return null;
    try {
      const session = await this.refreshSession();
      return session.user;
    } catch (error) {
      if (error instanceof DesktopApiError && error.details.status === 401) {
        this.clearSession();
        return null;
      }
      throw error;
    }
  }

  async login(email: string, password: string): Promise<DesktopUser> {
    const { response, payload } = await this.requestJson<TokenResponse>(
      "/api/v1/auth/login",
      { email, password },
    );
    if (!response.ok) throw errorFromResponse(response, payload);
    return this.acceptTokenResponse(response, payload).user;
  }

  async signup(email: string, password: string): Promise<DesktopUser> {
    const { response, payload } = await this.requestJson<unknown>(
      "/api/v1/auth/signup",
      { email, password },
    );
    if (!response.ok) throw errorFromResponse(response, payload);
    return this.login(email, password);
  }

  async logout(): Promise<void> {
    try {
      if (this.refreshToken) {
        const response = await this.request("/api/v1/auth/logout", {
          method: "POST",
          headers: { Cookie: `refresh_token=${this.refreshToken}` },
          body: "{}",
        });
        if (!response.ok) throw await responseError(response);
      }
    } finally {
      this.clearSession();
    }
  }

  getUser(): DesktopUser | null {
    return this.user;
  }

  async listSessions(): Promise<SessionSummary[]> {
    const items: SessionSummary[] = [];
    let cursor: string | null = null;
    const seen = new Set<string>();
    for (let pageIndex = 0; pageIndex < 10; pageIndex += 1) {
      const query = new URLSearchParams({ limit: "100" });
      if (cursor) query.set("cursor", cursor);
      const page = await this.authenticatedJson<SessionList>(
        `/api/v1/sessions?${query.toString()}`,
      );
      items.push(...page.items);
      if (!page.next_cursor || seen.has(page.next_cursor)) return items;
      seen.add(page.next_cursor);
      cursor = page.next_cursor;
    }
    return items;
  }

  async listQuestions(sessionId: string): Promise<QuestionItem[]> {
    const items: QuestionItem[] = [];
    let cursor: string | null = null;
    const seen = new Set<string>();
    for (let pageIndex = 0; pageIndex < 10; pageIndex += 1) {
      const query = new URLSearchParams({ limit: "100" });
      if (cursor) query.set("cursor", cursor);
      const page = await this.authenticatedJson<QuestionPage>(
        `/api/v1/sessions/${encodeURIComponent(sessionId)}/questions?${query.toString()}`,
      );
      items.push(...page.items);
      if (!page.next_cursor || seen.has(page.next_cursor)) return items;
      seen.add(page.next_cursor);
      cursor = page.next_cursor;
    }
    return items;
  }

  async streamQuestion(
    sessionId: string,
    text: string,
    signal: AbortSignal,
    onEvent: (event: QaEvent) => void,
    inputSource?: "screenshot",
  ): Promise<void> {
    const path = `/api/v1/sessions/${encodeURIComponent(sessionId)}/questions`;
    let response = await this.authenticatedStream(path, text, signal, inputSource);
    if (response.status === 401) {
      await this.refreshSession();
      response = await this.rawQuestionStream(path, text, signal, inputSource);
    }
    if (!response.ok) throw await responseError(response);
    const contentType = response.headers.get("Content-Type") ?? "";
    if (!contentType.toLowerCase().startsWith("text/event-stream") || !response.body) {
      throw new DesktopApiError({
        kind: "unknown",
        code: "bad_response",
        message: "The server sent an unexpected response.",
      });
    }
    await consumeSse(response, onEvent);
  }

  /**
   * Phase 11A endpoint: a raw `image/png` body (never multipart), authenticated in the main process.
   * The PNG is never logged or stored here; the caller wipes it afterwards.
   */
  async extractScreenshot(
    sessionId: string,
    png: Uint8Array,
    signal: AbortSignal,
  ): Promise<{ text: string; truncated: boolean }> {
    const path = `/api/v1/sessions/${encodeURIComponent(sessionId)}/screenshots`;
    if (!this.accessToken) {
      throw new DesktopApiError({
        kind: "http",
        status: 401,
        code: "not_authenticated",
        message: "Sign in again to continue.",
      });
    }
    const send = () =>
      this.request(path, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.accessToken ?? ""}`,
          "Content-Type": "image/png",
        },
        body: png as unknown as BodyInit,
        signal,
      });
    let response = await send();
    if (response.status === 401) {
      await this.refreshSession();
      response = await send();
    }
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) throw errorFromResponse(response, payload);
    const body = payload as { extracted_text?: unknown; truncated?: unknown } | null;
    if (typeof body?.extracted_text !== "string" || typeof body.truncated !== "boolean") {
      throw new DesktopApiError({
        kind: "unknown",
        code: "bad_response",
        message: "The server sent an unexpected response.",
      });
    }
    return { text: body.extracted_text, truncated: body.truncated };
  }

  private async authenticatedStream(
    path: string,
    text: string,
    signal: AbortSignal,
    inputSource?: "screenshot",
  ) {
    if (!this.accessToken) {
      throw new DesktopApiError({
        kind: "http",
        status: 401,
        code: "not_authenticated",
        message: "Sign in again to continue.",
      });
    }
    return this.rawQuestionStream(path, text, signal, inputSource);
  }

  private async rawQuestionStream(
    path: string,
    text: string,
    signal: AbortSignal,
    inputSource?: "screenshot",
  ) {
    return this.request(path, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.accessToken ?? ""}`,
        Accept: "text/event-stream",
        "Content-Type": "application/json",
      },
      // Typed questions keep the exact body they always had; only a completed screenshot flow
      // adds `input_source`.
      body: JSON.stringify(inputSource ? { text, input_source: inputSource } : { text }),
      signal,
    });
  }

  private async authenticatedJson<T>(path: string): Promise<T> {
    if (!this.accessToken) {
      throw new DesktopApiError({
        kind: "http",
        status: 401,
        code: "not_authenticated",
        message: "Sign in again to continue.",
      });
    }
    let response = await this.request(path, {
      headers: { Authorization: `Bearer ${this.accessToken}` },
    });
    if (response.status === 401) {
      await this.refreshSession();
      response = await this.request(path, {
        headers: { Authorization: `Bearer ${this.accessToken ?? ""}` },
      });
    }
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) throw errorFromResponse(response, payload);
    return payload as T;
  }

  private async refreshSession(): Promise<TokenResponse> {
    this.refreshToken ??= this.refreshTokens.load();
    if (!this.refreshToken) {
      throw new DesktopApiError({
        kind: "http",
        status: 401,
        code: "session_expired",
        message: "Your session expired. Please sign in again.",
      });
    }
    const response = await this.request("/api/v1/auth/refresh", {
      method: "POST",
      headers: { Cookie: `refresh_token=${this.refreshToken}` },
      body: "{}",
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const error = errorFromResponse(response, payload);
      if (response.status === 401) this.clearSession();
      throw error;
    }
    return this.acceptTokenResponse(response, payload);
  }

  private acceptTokenResponse(response: Response, payload: unknown): TokenResponse {
    const tokenResponse = payload as TokenResponse;
    const rotated = getRefreshCookie(response);
    if (!tokenResponse?.access_token || !tokenResponse.user || !rotated) {
      throw new DesktopApiError({
        kind: "unknown",
        code: "invalid_auth_response",
        message: "The backend returned an unexpected sign-in response.",
      });
    }
    this.refreshTokens.save(rotated);
    this.refreshToken = rotated;
    this.accessToken = tokenResponse.access_token;
    this.user = tokenResponse.user;
    return tokenResponse;
  }

  private clearSession(): void {
    this.accessToken = null;
    this.user = null;
    this.refreshToken = null;
    this.refreshTokens.clear();
  }

  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    try {
      return await this.fetcher(apiUrl(this.origin, path), {
        ...init,
        cache: "no-store",
      });
    } catch (error) {
      if (error instanceof DesktopApiError || (error instanceof Error && error.name === "AbortError")) {
        throw error;
      }
      throw new DesktopApiError({
        kind: "network",
        code: "network_error",
        message: "Could not reach the backend. Check your connection and try again.",
      });
    }
  }

  private async requestJson<T>(
    path: string,
    body: unknown,
  ): Promise<{ response: Response; payload: T }> {
    const response = await this.request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return {
      response,
      payload: (await response.json().catch(() => null)) as T,
    };
  }
}

export function asDesktopError(error: unknown): DesktopError {
  if (error instanceof DesktopApiError) return error.details;
  if (error instanceof Error && error.name === "AbortError") {
    return { kind: "unknown", code: "cancelled", message: "Generation cancelled." };
  }
  return {
    kind: "unknown",
    code: "unexpected_error",
    message: "Something went wrong. Please try again.",
  };
}

export type { DesktopSettings, AnswerItem };

async function consumeSse(
  response: Response,
  onEvent: (event: QaEvent) => void,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Missing stream reader");
  const decoder = new TextDecoder();
  const parser = new SseParser();
  let terminal = false;
  try {
    while (!terminal) {
      const { done, value } = await reader.read();
      const chunk = done ? decoder.decode() : decoder.decode(value, { stream: true });
      for (const frame of parser.push(chunk)) {
        const event = parseSseEvent(frame);
        if (!event) continue;
        onEvent(event);
        if (event.type === "answer.done" || event.type === "answer.error") {
          terminal = true;
          break;
        }
      }
      if (done) break;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
  if (!terminal) {
    throw new DesktopApiError({
      kind: "unknown",
      code: "stream_interrupted",
      message: "The answer stream ended unexpectedly.",
    });
  }
}
