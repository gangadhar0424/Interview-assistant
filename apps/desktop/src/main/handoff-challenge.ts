import { createHash, randomBytes } from "node:crypto";

/**
 * Handoff binding (SEC-08, D-82). The web page asks the running desktop app for a CHALLENGE, binds
 * the one-time code it requests from the API to that challenge, and opens the desktop with the code
 * and the challenge. The desktop then redeems the code with the matching VERIFIER, which never left
 * this process.
 *
 * Why it stops the interception: a program that captures the link `interviewassistant://start?h=..&c=..`
 * learns the code and the challenge, but the challenge is only a SHA-256 hash of the verifier, and the
 * verifier exists nowhere except in this map. Without it the API refuses the code.
 *
 * Properties (each pinned by tests/handoff-challenge.test.ts):
 * - the verifier is 32 cryptographically random bytes (43 URL-safe characters, RFC 7636 alphabet);
 * - it lives in memory only: never logged, never written, never in a URL, never sent to the renderer;
 * - single use: `take` removes it whether or not the redemption later succeeds;
 * - short-lived: it expires after `CHALLENGE_TTL_MS` (longer than the code's 60 s so a native
 *   confirmation dialog does not strand a legitimate redemption, short enough to be useless later);
 * - bounded: at most `MAX_PENDING_CHALLENGES` at once (the oldest is dropped) and at most
 *   `MAX_ISSUES_PER_MINUTE` new ones, so a hostile web page cannot grow memory or churn the map.
 *
 * Known residual (a denial of service, never a way in): a web page the user visits can request
 * challenges from the loopback beacon. Rate limiting and the bound limit it; a legitimate flow that
 * is interrupted simply asks the user to press "Start Interview" again.
 */

export const CHALLENGE_TTL_MS = 120_000;
export const MAX_PENDING_CHALLENGES = 16;
export const MAX_ISSUES_PER_MINUTE = 30;
/** 32 random bytes in base64url, and a SHA-256 digest in base64url, are both exactly 43 characters. */
export const CHALLENGE_PATTERN = /^[A-Za-z0-9_-]{43}$/;

/** base64url(SHA-256(verifier)): the same function the server uses to check the verifier. */
export function challengeOf(verifier: string): string {
  return createHash("sha256").update(verifier, "ascii").digest("base64url");
}

export type ChallengeClock = {
  now(): number;
  randomBytes(size: number): Buffer;
};

const SYSTEM_CLOCK: ChallengeClock = { now: () => Date.now(), randomBytes: (size) => randomBytes(size) };

export type IssuedChallenge = { challenge: string; expiresInSeconds: number };

export class HandoffChallenges {
  private readonly pending = new Map<string, { verifier: string; expiresAt: number }>();
  private issuedAt: number[] = [];

  constructor(private readonly clock: ChallengeClock = SYSTEM_CLOCK) {}

  /** A new challenge, or null when too many were requested in the last minute. */
  issue(): IssuedChallenge | null {
    const now = this.clock.now();
    this.purge(now);
    this.issuedAt = this.issuedAt.filter((at) => now - at < 60_000);
    if (this.issuedAt.length >= MAX_ISSUES_PER_MINUTE) return null;
    this.issuedAt.push(now);
    const verifier = this.clock.randomBytes(32).toString("base64url");
    const challenge = challengeOf(verifier);
    this.pending.set(challenge, { verifier, expiresAt: now + CHALLENGE_TTL_MS });
    while (this.pending.size > MAX_PENDING_CHALLENGES) {
      const oldest = this.pending.keys().next().value;
      if (oldest === undefined) break;
      this.pending.delete(oldest);
    }
    return { challenge, expiresInSeconds: CHALLENGE_TTL_MS / 1000 };
  }

  /** The verifier for `challenge`, removed on the way out (single use). null if unknown or expired. */
  take(challenge: string): string | null {
    const now = this.clock.now();
    this.purge(now);
    if (!CHALLENGE_PATTERN.test(challenge)) return null;
    const entry = this.pending.get(challenge);
    if (entry === undefined) return null;
    this.pending.delete(challenge);
    return entry.verifier;
  }

  /** Sign-out, quit or any reset: nothing pending survives. */
  clear(): void {
    this.pending.clear();
  }

  /** For tests only: how many verifiers are held. */
  size(): number {
    return this.pending.size;
  }

  private purge(now: number): void {
    for (const [challenge, entry] of this.pending) {
      if (entry.expiresAt <= now) this.pending.delete(challenge);
    }
  }
}
