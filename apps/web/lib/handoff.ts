// Web -> desktop handoff helpers (D-55). The web app asks the API for a short-lived, single-use
// code for one of the user's prepared sessions and opens the installed desktop app with it:
//
//   interviewassistant://start?h=<code>
//
// The link carries ONLY that opaque code: no session data, API URL or token. The desktop app
// redeems it as its own signed-in user, so the code is useless to anyone else.
//
// A browser cannot tell whether a custom-protocol link opened an app, so "did it open?" is a
// heuristic: if the page lost focus or was hidden shortly after navigating, the app took over;
// otherwise the install/retry fallback is shown. The fallback never shows the code or the link.
//
// Imported by Node's test runner (tests/*.test.ts) as well as the app: plain, erasable
// TypeScript, no path aliases, relative imports with ".ts".

import { ApiError } from "./api.ts";

export const PROTOCOL_SCHEME = "interviewassistant";
export const CODE_PATTERN = /^[A-Za-z0-9_-]{43}$/;
/** How long to wait for the page to lose focus before assuming the desktop app did not open. */
export const LAUNCH_WAIT_MS = 2500;

export type HandoffIssue = { code: string; expires_in: number };

/** Builds the launch link, or null when the value is not a well-formed handoff code. */
export function buildLaunchUrl(code: string): string | null {
  return CODE_PATTERN.test(code) ? `${PROTOCOL_SCHEME}://start?h=${code}` : null;
}

/**
 * The installer download link from NEXT_PUBLIC_DESKTOP_DOWNLOAD_URL. Only an absolute https URL
 * (without embedded credentials) is used; anything else means "not configured".
 */
export function parseDownloadUrl(raw: string | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw.trim());
    if (url.protocol !== "https:" || url.username !== "" || url.password !== "") return null;
    return url.toString();
  } catch {
    return null;
  }
}

export type LaunchOutcome =
  | { kind: "opened" }
  /** The page stayed in front: the app is probably not installed, or the browser blocked it. */
  | { kind: "not-opened" }
  /** The API refused or failed; `message` is fixed text chosen here, never server text. */
  | { kind: "error"; message: string };

export function describeHandoffError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      const wait = error.retryAfterSeconds;
      return wait ? `Too many attempts. Please try again in ${wait} seconds.` : "Too many attempts. Please wait a moment.";
    }
    if (error.status === 404) return "This session was not found.";
    if (error.status === 409) return "This session cannot be started. Check that it is ready.";
    if (error.status === 401) return "Your sign-in has expired. Please sign in again.";
    if (error.status === 503) return "Starting the desktop app is temporarily unavailable.";
  }
  return "Could not start the desktop app. Please try again.";
}

export type LaunchDeps = {
  /** POST /api/v1/sessions/{id}/handoff (authenticated). */
  requestCode: () => Promise<HandoffIssue>;
  /** Navigates the browser to the protocol link. */
  navigate: (url: string) => void;
  /**
   * Starts watching for the page to lose focus / be hidden and resolves true if that happens
   * within `timeoutMs`. It must be started BEFORE `navigate` is called.
   */
  watchForLeave: (timeoutMs: number) => Promise<boolean>;
  waitMs?: number;
};

/** One "Start Interview" attempt: new code -> launch link -> did the page lose focus? */
export async function launchDesktop(deps: LaunchDeps): Promise<LaunchOutcome> {
  let issued: HandoffIssue;
  try {
    issued = await deps.requestCode();
  } catch (error) {
    return { kind: "error", message: describeHandoffError(error) };
  }
  const url = buildLaunchUrl(issued.code);
  if (url === null) return { kind: "error", message: describeHandoffError(null) };
  const left = deps.watchForLeave(deps.waitMs ?? LAUNCH_WAIT_MS);
  deps.navigate(url);
  return (await left) ? { kind: "opened" } : { kind: "not-opened" };
}

/** Browser implementation of `watchForLeave`: blur or visibilitychange(hidden) within the window. */
export function browserWatchForLeave(timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    const finish = (left: boolean) => {
      window.clearTimeout(timer);
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVisibility);
      resolve(left);
    };
    const onBlur = () => finish(true);
    const onVisibility = () => {
      if (document.visibilityState === "hidden") finish(true);
    };
    const timer = window.setTimeout(() => finish(false), timeoutMs);
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVisibility);
  });
}
