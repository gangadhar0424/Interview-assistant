export type FieldError = { field: string; message: string };

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: FieldError[];
  /** Seconds from a `Retry-After` header (429 responses), when the server sent one. */
  readonly retryAfterSeconds: number | null;

  constructor(
    status: number,
    code: string,
    message: string,
    details: FieldError[] = [],
    retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

type ErrorEnvelope = {
  error?: { code?: string; message?: string; details?: FieldError[] };
};

export type RequestOptions = {
  method?: string;
  /** JSON body. */
  body?: unknown;
  /** Multipart body (the browser sets the Content-Type and boundary). */
  formData?: FormData;
  /** Return the response as a Blob (file downloads) instead of parsing JSON. */
  asBlob?: boolean;
};

export async function apiRequest<T>(
  path: string,
  options: RequestOptions & { token?: string | null } = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  let body: BodyInit | undefined;
  if (options.formData !== undefined) {
    body = options.formData;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  if (options.token) headers["Authorization"] = `Bearer ${options.token}`;

  let response: Response;
  try {
    response = await fetch(path, {
      method: options.method ?? (body !== undefined ? "POST" : "GET"),
      headers,
      body,
      credentials: "same-origin", // refresh cookie travels only to our own origin
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "network_error", "Could not reach the server. Please try again.");
  }

  if (response.ok && options.asBlob) return (await response.blob()) as T;
  if (response.status === 204) return undefined as T;

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) throw toApiError(response, payload);
  return payload as T;
}

function toApiError(response: Response, payload: unknown): ApiError {
  const err = (payload as ErrorEnvelope | null)?.error;
  const retryAfter = Number(response.headers.get("Retry-After"));
  return new ApiError(
    response.status,
    err?.code ?? "unknown_error",
    err?.message ?? "Something went wrong. Please try again.",
    err?.details ?? [],
    Number.isFinite(retryAfter) && retryAfter > 0 ? Math.ceil(retryAfter) : null,
  );
}

export type StreamOptions = {
  /** JSON body. */
  body: unknown;
  token?: string | null;
  /** Aborting closes the connection; the server then records the answer as cancelled. */
  signal?: AbortSignal;
};

// POST that expects a Server-Sent Events response (Phase 8, docs/API.md). Refusals that happen
// before the stream starts (401/404/406/409/422/429) are ordinary JSON errors and are thrown as
// ApiError; on success the caller gets the live Response and reads its body. An abort is rethrown
// as-is (it is the caller's own doing, not a network failure).
export async function apiStream(path: string, options: StreamOptions): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  if (options.token) headers["Authorization"] = `Bearer ${options.token}`;

  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers,
      body: JSON.stringify(options.body),
      credentials: "same-origin",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (err) {
    if (options.signal?.aborted) throw err;
    throw new ApiError(0, "network_error", "Could not reach the server. Please try again.");
  }

  if (!response.ok) throw toApiError(response, await response.json().catch(() => null));
  const type = response.headers.get("Content-Type") ?? "";
  if (!type.toLowerCase().startsWith("text/event-stream") || response.body === null) {
    throw new ApiError(0, "bad_response", "The server sent an unexpected response.");
  }
  return response;
}
