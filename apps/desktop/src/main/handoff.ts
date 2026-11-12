// Web -> desktop handoff controller (D-55, sign-in changed by D-56). Main process only; the
// renderer receives `HandoffState` (a phase, the prepared session's id and name, or a failure
// kind) and never the code or any token.
//
// Flow: a handoff link arrives (cold start or second instance) -> the single-use code is redeemed
// at once WITHOUT a desktop login: the code proves the user, and redemption returns the desktop's
// own login session -> that login is adopted -> the prepared session is selected and the assistant
// shows READY. The user signed in once on the web and never types a password here.
//
// What this never does: start microphone, system-audio or screenshot capture; change the session's
// status; keep the code on disk, in logs or in the renderer; switch away from a running capture
// without an explicit native confirmation; or replace a different signed-in account without one.

import {
  IDLE_HANDOFF,
  type HandoffFailure,
  type HandoffSession,
  type HandoffState,
} from "../shared/contracts";
import { parseHandoffLink } from "./deep-link";

/** Matches the server code lifetime (60 s). A code that waited out a dialog longer is not sent. */
export const HANDOFF_CODE_TTL_MS = 60_000;

export type RedeemedHandoff = {
  /** The user the web session belongs to (never shown to the renderer). */
  userId: string;
  session: HandoffSession & { status: string };
  /** Makes the minted desktop login this app's login. Never throws. */
  commit(): void;
  /** Gives the minted login up (best effort revoke). Never throws. */
  discard(): Promise<void>;
};

export type HandoffPorts = {
  /** Resolves once any sign-in/restore already in flight has finished (no race on the stored login). */
  settled(): Promise<void>;
  /** The user currently signed in on this desktop, if any. */
  currentUserId(): string | null;
  /** `POST /sessions/handoff/redeem`: public; returns the prepared session and a new login. */
  redeem(code: string): Promise<RedeemedHandoff>;
  /** True while a screenshot countdown/capture/held image or live audio exists. */
  isLive(): boolean;
  /** Stops any capture (the existing "session changed" safety behaviour). */
  stopLive(): void;
  /** Ends in-flight answers of the previous account before another account takes over. */
  resetForAccountChange(): void;
  /** Native, main-process confirmation before switching away from a running capture. */
  confirmSwitch(): Promise<boolean>;
  /** Native, main-process confirmation before another account replaces the signed-in one. */
  confirmAccountSwitch(): Promise<boolean>;
  emit(state: HandoffState): void;
  /** Brings the window forward so the user sees the result. */
  showWindow(): void;
  now(): number;
};

/** Maps a thrown error to a fixed failure kind without exposing any server text. */
export function classifyRedeemError(error: unknown): HandoffFailure {
  const details = (error as { details?: { status?: number } } | null)?.details;
  const status = details?.status;
  if (status === 404) return "invalid";
  if (status === 429) return "rate_limited";
  return "unavailable";
}

export class HandoffController {
  private state: HandoffState = IDLE_HANDOFF;
  /** Bumped by every new link and by sign-out, so a stale in-flight redemption is dropped. */
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
    if (code === null) {
      this.setState({ phase: "failed", session: null, failure: "invalid" });
      return;
    }
    void this.process(code, this.ports.now(), this.epoch);
  }

  /** Sign-out or an expired session: nothing handed off survives it. */
  onSignedOut(): void {
    this.epoch += 1;
    if (this.state.phase !== "idle") this.setState(IDLE_HANDOFF);
  }

  dismiss(): void {
    if (this.state.phase === "ready" || this.state.phase === "failed") this.setState(IDLE_HANDOFF);
  }

  private expired(receivedAt: number): boolean {
    return this.ports.now() - receivedAt > HANDOFF_CODE_TTL_MS;
  }

  private fail(failure: HandoffFailure): void {
    this.setState({ phase: "failed", session: null, failure });
  }

  private async process(code: string, receivedAt: number, epoch: number): Promise<void> {
    this.setState({ phase: "redeeming", session: null, failure: null });
    try {
      await this.ports.settled();
      if (epoch !== this.epoch) return;
      if (this.ports.isLive()) {
        // Never switch away from a running capture silently.
        const confirmed = await this.ports.confirmSwitch();
        if (epoch !== this.epoch) return;
        if (!confirmed) {
          this.fail("declined");
          return;
        }
      }
      if (this.expired(receivedAt)) {
        this.fail("expired");
        return;
      }

      const redeemed = await this.ports.redeem(code); // the code is consumed here
      if (epoch !== this.epoch) {
        await redeemed.discard();
        return;
      }
      if (redeemed.session.status !== "ready") {
        await redeemed.discard();
        this.fail("invalid");
        return;
      }

      const current = this.ports.currentUserId();
      const differentAccount = current !== null && current !== redeemed.userId;
      if (differentAccount) {
        const confirmed = await this.ports.confirmAccountSwitch();
        if (epoch !== this.epoch) {
          await redeemed.discard();
          return;
        }
        if (!confirmed) {
          await redeemed.discard();
          this.fail("declined");
          return;
        }
        this.ports.resetForAccountChange();
      }
      if (current === redeemed.userId) {
        await redeemed.discard(); // already this user: keep the existing login, drop the spare
      } else {
        redeemed.commit();
      }
      this.ports.stopLive(); // selecting another session ends any capture, as before
      this.setState({
        phase: "ready",
        session: {
          id: redeemed.session.id,
          title: redeemed.session.title,
          company_name: redeemed.session.company_name,
        },
        failure: null,
      });
    } catch (error) {
      if (epoch !== this.epoch) return;
      this.fail(classifyRedeemError(error));
    }
  }

  private setState(next: HandoffState): void {
    this.state = next;
    this.ports.emit(next);
  }
}
