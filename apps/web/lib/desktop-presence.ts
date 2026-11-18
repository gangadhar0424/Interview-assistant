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
