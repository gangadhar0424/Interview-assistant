/**
 * Test helper (D-65). The desktop has no password sign-in: an account reaches it only through the
 * web "Start Interview" handoff. IPC test harnesses therefore sign in by delivering a handoff link
 * to the deep-link inbox and serving `POST /sessions/handoff/redeem` from their fake backend.
 */
import { vi } from "vitest";

export const HANDOFF_CODE = "Ab3-_".repeat(8) + "xyz";
export const HANDOFF_LINK = `interviewassistant://start?h=${HANDOFF_CODE}`;
export const HANDOFF_REDEEM_PATH = "/api/v1/sessions/handoff/redeem";

/** Lets the main-process handoff finish (works with real and with fake timers). */
export async function settleHandoff(): Promise<void> {
  for (let i = 0; i < 20; i += 1) {
    if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(0);
    else await new Promise((resolve) => setTimeout(resolve, 2));
  }
}
