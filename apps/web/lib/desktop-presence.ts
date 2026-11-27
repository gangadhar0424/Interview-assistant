// Desktop presence check (D-60). A browser cannot ask Windows whether an app handles a custom
// protocol, so the running desktop app answers one content-free request on loopback and the page
// uses that only to choose between "open the app" and "show the install hint".
//
// This is the only unauthenticated, non-API request in the handoff flow: fixed loopback URL, no
// credentials, no cookies, and the response is opaque (no-cors) so nothing is ever read from it.

/** Loopback port the running desktop app answers on; keep in sync with the desktop app. */
export const PRESENCE_PORT = 47821;
export const PRESENCE_URL = `http://127.0.0.1:${PRESENCE_PORT}/ia-presence`;
/** How long to wait for the beacon before treating the app as not running. */
export const PRESENCE_TIMEOUT_MS = 800;

/** True only when something on loopback answered. Rejection (refused/timeout) means "not running". */
export async function browserDetectDesktop(timeoutMs: number = PRESENCE_TIMEOUT_MS): Promise<boolean> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    await fetch(PRESENCE_URL, {
      mode: "no-cors",
      credentials: "omit",
      cache: "no-store",
      signal: controller.signal,
    });
    return true;
  } catch {
    return false;
  } finally {
    window.clearTimeout(timer);
  }
}

// ---- Handoff binding (SEC-08) -------------------------------------------------------------------
// The running desktop app hands the page a CHALLENGE: the SHA-256 of a secret verifier that stays in
// the desktop app's memory. The page binds the one-time code it asks the API for to that challenge,
// so a program that captures the launch link later has a code it cannot redeem. The challenge is not
// a secret (it is a hash); the page never sees, stores or sends a verifier.

export const CHALLENGE_URL = `http://127.0.0.1:${PRESENCE_PORT}/ia-presence/challenge`;
export const CHALLENGE_PATTERN = /^[A-Za-z0-9_-]{43}$/;
export const CHALLENGE_TIMEOUT_MS = 1500;

/** Parses the beacon's answer; null for anything that is not exactly a well-formed challenge. */
export function parseChallengeResponse(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const value = (payload as { challenge?: unknown }).challenge;
  return typeof value === "string" && CHALLENGE_PATTERN.test(value) ? value : null;
}

/**
 * Asks the running desktop app for a challenge. null means "none available" (an older desktop app,
 * a refused or slow answer): the caller then uses the unbound flow, which the API accepts only while
 * it does not require binding. Never throws.
 */
export async function browserFetchChallenge(timeoutMs: number = CHALLENGE_TIMEOUT_MS): Promise<string | null> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(CHALLENGE_URL, {
      mode: "cors",
      credentials: "omit",
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) return null;
    return parseChallengeResponse(await response.json());
  } catch {
    return null;
  } finally {
    window.clearTimeout(timer);
  }
}
