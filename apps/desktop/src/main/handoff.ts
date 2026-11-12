// Web -> desktop handoff controller (D-55). Main process only; the renderer receives `HandoffState`
// (a phase, the prepared session's id and name, or a failure kind) and never the code.
//
// Flow: a handoff link arrives (cold start or second instance) -> the code is held in MEMORY ONLY
// for at most its server lifetime -> once this app has its own signed-in user it is redeemed
// once -> the prepared session is selected and the assistant shows READY.
//
// What this never does: start microphone, system-audio or screenshot capture; change the session's
// status; keep the code on disk, in logs or in the renderer; or switch away from a running capture
// without an explicit native confirmation.

import {
  IDLE_HANDOFF,
  type HandoffFailure,
  type HandoffSession,
  type HandoffState,
} from "../shared/contracts";
import { parseHandoffLink } from "./deep-link";

/** Matches the server code lifetime (WS ticket-style single use, 60 s). */
export const HANDOFF_CODE_TTL_MS = 60_000;

export type RedeemedSession = HandoffSession & { status: string };

export type HandoffPorts = {
  isSignedIn(): boolean;
  /** `POST /sessions/handoff/redeem` as the signed-in user. */
  redeem(code: string): Promise<RedeemedSession>;
  /** True while a screenshot countdown/capture/held image or live audio exists. */
  isLive(): boolean;
  /** Stops any capture (the existing "session changed" safety behaviour). */
  stopLive(): void;
  /** Native, main-process confirmation; resolves false when the user declines. */
  confirmSwitch(): Promise<boolean>;
  emit(state: HandoffState): void;
  /** Brings the window forward so the user sees the result. */
  showWindow(): void;
  /** A 401 the refresh could not fix: the same path as any other expired session. */
  onAuthExpired(): void;
  now(): number;
  /** Schedules `fn`; returns a function that cancels it. */
  schedule(fn: () => void, ms: number): () => void;
};

type Pending = { code: string; receivedAt: number };

/** Maps a thrown error to a fixed failure kind without exposing any server text. */
export function classifyRedeemError(error: unknown): HandoffFailure | "auth" {
  const details = (error as { details?: { status?: number; kind?: string } } | null)?.details;
  const status = details?.status;
  if (status === 401) return "auth";
  if (status === 404) return "invalid";
  if (status === 429) return "rate_limited";
  return "unavailable";
}

export class HandoffController {
  private state: HandoffState = IDLE_HANDOFF;
  private pending: Pending | null = null;
  private cancelExpiry: (() => void) | null = null;
  /** Bumped by every new link and by sign-out, so a stale in-flight redemption is ignored. */
  private epoch = 0;

  constructor(private readonly ports: HandoffPorts) {}

  getState(): HandoffState {
    return this.state;
  }

  /** Entry point for a raw link from the OS. Anything that is not a well-formed link is refused. */
  receiveLink(raw: string): void {
    const code = parseHandoffLink(raw);
    this.ports.showWindow();
    this.epoch += 1;
    this.clearPending();
    if (code === null) {
      this.setState({ phase: "failed", session: null, failure: "invalid" });
      return;
    }
    this.pending = { code, receivedAt: this.ports.now() };
    // The code never outlives its server lifetime in memory, whatever else happens.
    this.cancelExpiry = this.ports.schedule(() => this.expirePending(), HANDOFF_CODE_TTL_MS);
    if (!this.ports.isSignedIn()) {
      this.setState({ phase: "waiting_signin", session: null, failure: null });
      return;
    }
    void this.process(this.epoch);
  }

  /** Called after a successful sign-in/sign-up or a restored session. */
  onSignedIn(): void {
    if (this.pending) void this.process(this.epoch);
  }

  /** Sign-out or an expired session: nothing handed off survives it. */
  onSignedOut(): void {
    this.epoch += 1;
    this.clearPending();
    if (this.state.phase !== "idle") this.setState(IDLE_HANDOFF);
  }

  dismiss(): void {
    if (this.state.phase === "ready" || this.state.phase === "failed") this.setState(IDLE_HANDOFF);
  }

  private async process(epoch: number): Promise<void> {
    const taken = this.pending;
    if (!taken) return;
    if (this.ports.now() - taken.receivedAt > HANDOFF_CODE_TTL_MS) {
      this.clearPending();
      this.setState({ phase: "failed", session: null, failure: "expired" });
      return;
    }
    if (this.ports.isLive()) {
      // Never switch away from a running capture silently.
      const confirmed = await this.ports.confirmSwitch();
      if (epoch !== this.epoch) return;
      if (!confirmed) {
        this.clearPending();
        this.setState({ phase: "failed", session: null, failure: "declined" });
        return;
      }
      if (this.ports.now() - taken.receivedAt > HANDOFF_CODE_TTL_MS) {
        this.clearPending();
        this.setState({ phase: "failed", session: null, failure: "expired" });
        return;
      }
    }
    this.clearPending(); // the code is only ever held for one redemption attempt
    this.setState({ phase: "redeeming", session: null, failure: null });
    try {
      const redeemed = await this.ports.redeem(taken.code);
      if (epoch !== this.epoch) return;
      if (redeemed.status !== "ready") {
        this.setState({ phase: "failed", session: null, failure: "invalid" });
        return;
      }
      this.ports.stopLive(); // selecting another session ends any capture, as before
      this.setState({
        phase: "ready",
        session: { id: redeemed.id, title: redeemed.title, company_name: redeemed.company_name },
        failure: null,
      });
    } catch (error) {
      if (epoch !== this.epoch) return;
      const kind = classifyRedeemError(error);
      if (kind === "auth") {
        this.ports.onAuthExpired();
        this.setState({ phase: "failed", session: null, failure: "signed_out" });
        return;
      }
      this.setState({ phase: "failed", session: null, failure: kind });
    }
  }

  private expirePending(): void {
    if (!this.pending) return;
    this.pending = null;
    this.cancelExpiry = null;
    this.setState({ phase: "failed", session: null, failure: "expired" });
  }

  private clearPending(): void {
    this.pending = null;
    this.cancelExpiry?.();
    this.cancelExpiry = null;
  }

  private setState(next: HandoffState): void {
    this.state = next;
    this.ports.emit(next);
  }
}
