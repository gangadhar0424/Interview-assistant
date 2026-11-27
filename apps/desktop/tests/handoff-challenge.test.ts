/**
 * SEC-08 (D-82): handoff binding on the desktop. The challenge store (single use, short-lived,
 * bounded, rate-limited, memory only), the loopback endpoint that hands the web page a challenge, the
 * link parser that accepts the challenge, and the controller that redeems a bound code only with the
 * verifier this app made for it. Pure fakes: no Electron, no network, no real port, no real clock.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";
import { describe, expect, it, vi } from "vitest";
import {
  CHALLENGE_PATTERN,
  CHALLENGE_TTL_MS,
  HandoffChallenges,
  MAX_ISSUES_PER_MINUTE,
  MAX_PENDING_CHALLENGES,
  challengeOf,
  type ChallengeClock,
} from "../src/main/handoff-challenge";
import { parseHandoffLinkParts } from "../src/main/deep-link";
import { HandoffController, type HandoffPorts, type RedeemedHandoff } from "../src/main/handoff";
import { CHALLENGE_PATH, PRESENCE_PATH, PRESENCE_PORT, handlePresenceRequest } from "../src/main/presence-server";
import { IDLE_HANDOFF, type HandoffState } from "../src/shared/contracts";

// ---------------------------------------------------------------------------------- the store

function clock(): ChallengeClock & { time: number; counter: number } {
  const c = {
    time: 1_000_000,
    counter: 0,
    now: () => c.time,
    // Deterministic but distinct 32-byte values (a real run uses crypto.randomBytes).
    randomBytes: (size: number) => {
      c.counter += 1;
      return Buffer.alloc(size, c.counter);
    },
  };
  return c;
}

describe("the challenge store", () => {
  it("makes a 43-character verifier whose SHA-256 is the challenge", () => {
    const store = new HandoffChallenges();
    const issued = store.issue();
    expect(issued).not.toBeNull();
    const challenge = (issued as { challenge: string }).challenge;
    expect(challenge).toMatch(CHALLENGE_PATTERN);
    const verifier = store.take(challenge);
    expect(verifier).toMatch(CHALLENGE_PATTERN);
    expect(challengeOf(verifier as string)).toBe(challenge);
  });

  it("uses the same hash as the server (a known vector)", () => {
    // base64url(SHA-256("abc")), computed independently of this code.
    expect(challengeOf("abc")).toBe("ungWv48Bz-pBQUDeXa4iI7ADYaOWF3qctBD_YfIAFa0");
  });

  it("draws the verifier from the cryptographic random source, never from Math.random", () => {
    const source = readFileSync(join(__dirname, "../src/main/handoff-challenge.ts"), "utf8");
    expect(source).toContain('from "node:crypto"');
    expect(source).not.toMatch(/Math\.random/);
  });

  it("hands out different challenges each time", () => {
    const store = new HandoffChallenges();
    const seen = new Set<string>();
    for (let i = 0; i < 20; i += 1) seen.add((store.issue() as { challenge: string }).challenge);
    expect(seen.size).toBe(20);
  });

  it("is single use: the second take, and a retry after a failed redemption, find nothing", () => {
    const store = new HandoffChallenges();
    const { challenge } = store.issue() as { challenge: string };
    expect(store.take(challenge)).not.toBeNull();
    expect(store.take(challenge)).toBeNull();
    expect(store.size()).toBe(0);
  });

  it("expires after the lifetime and not a moment before", () => {
    const c = clock();
    const store = new HandoffChallenges(c);
    const { challenge } = store.issue() as { challenge: string };
    c.time += CHALLENGE_TTL_MS - 1;
    const alive = store.issue() as { challenge: string }; // forces a purge pass; first is still valid
    expect(store.take(challenge)).not.toBeNull();
    c.time += 2;
    expect(store.take(alive.challenge)).not.toBeNull(); // issued later, still inside its own lifetime
    const { challenge: late } = store.issue() as { challenge: string };
    c.time += CHALLENGE_TTL_MS;
    expect(store.take(late)).toBeNull();
  });

  it("refuses anything that is not a well-formed challenge, without touching state", () => {
    const store = new HandoffChallenges();
    store.issue();
    for (const bad of ["", "short", "A".repeat(42), "A".repeat(44), "../".repeat(15), "é".repeat(43), "A".repeat(43)]) {
      expect(store.take(bad), bad).toBeNull();
    }
    expect(store.size()).toBe(1);
  });

  it("holds a bounded number and drops the oldest first", () => {
    const store = new HandoffChallenges(clock());
    const first = store.issue() as { challenge: string };
    for (let i = 0; i < MAX_PENDING_CHALLENGES; i += 1) store.issue();
    expect(store.size()).toBe(MAX_PENDING_CHALLENGES);
    expect(store.take(first.challenge)).toBeNull();
  });

  it("limits how fast challenges can be requested, and recovers", () => {
    const c = clock();
    const store = new HandoffChallenges(c);
    for (let i = 0; i < MAX_ISSUES_PER_MINUTE; i += 1) expect(store.issue()).not.toBeNull();
    expect(store.issue()).toBeNull();
    c.time += 60_001;
    expect(store.issue()).not.toBeNull();
  });

  it("SEC-32: a flooding page can interrupt a legitimate attempt, but cannot take it over, and the user recovers by retrying", () => {
    const c = clock();
    const store = new HandoffChallenges(c);
    const legit = store.issue() as { challenge: string };
    // A hostile page asks as fast as it is allowed to: the oldest pending challenge is evicted.
    const hostile: string[] = [];
    for (let i = 0; i < MAX_PENDING_CHALLENGES; i += 1) hostile.push((store.issue() as { challenge: string }).challenge);
    expect(store.size()).toBeLessThanOrEqual(MAX_PENDING_CHALLENGES);
    // Impact is an interruption: the legitimate challenge is gone (the page's link will not redeem).
    expect(store.take(legit.challenge)).toBeNull();
    // It is not a takeover: what the hostile page holds is only hashes. Taking one by its hash
    // returns the desktop's private verifier to the desktop's own code, never to the caller, and
    // nothing the endpoint returns lets a page derive a verifier (the body is the hash only).
    expect(hostile.every((h) => CHALLENGE_PATTERN.test(h))).toBe(true);
    // Recovery: once the rate window passes, the legitimate page can ask again and succeeds.
    c.time += 60_001;
    const retry = store.issue() as { challenge: string } | null;
    expect(retry).not.toBeNull();
    expect(store.take((retry as { challenge: string }).challenge)).not.toBeNull();
  });

  it("clear() forgets everything", () => {
    const store = new HandoffChallenges();
    const { challenge } = store.issue() as { challenge: string };
    store.clear();
    expect(store.take(challenge)).toBeNull();
  });

  it("keeps the verifier out of logs, disk and the network", () => {
    const source = readFileSync(join(__dirname, "../src/main/handoff-challenge.ts"), "utf8");
    expect(source).not.toMatch(/console\.|node:fs|writeFile|fetch\(|localStorage|process\.env/);
  });
});

// ---------------------------------------------------------------- the loopback challenge endpoint

type Reply = { status: number; headers: Record<string, string>; body: string };

function ask(
  method: string,
  path: string,
  challenges: HandoffChallenges | undefined,
  host = `127.0.0.1:${PRESENCE_PORT}`,
): Reply {
  const reply: Reply = { status: 0, headers: {}, body: "" };
  const res = {
    writeHead(status: number, headers: Record<string, string> = {}) {
      reply.status = status;
      reply.headers = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
      return res;
    },
    end(body = "") {
      reply.body = body;
    },
  };
  handlePresenceRequest({ method, url: path, headers: { host } } as IncomingMessage, res as unknown as ServerResponse, challenges);
  return reply;
}

describe("the loopback challenge endpoint", () => {
  it("returns exactly a challenge and its lifetime, as JSON, not cacheable", () => {
    const store = new HandoffChallenges();
    const reply = ask("GET", CHALLENGE_PATH, store);
    expect(reply.status).toBe(200);
    expect(reply.headers["content-type"]).toContain("application/json");
    expect(reply.headers["cache-control"]).toBe("no-store");
    expect(reply.headers["x-content-type-options"]).toBe("nosniff");
    const body = JSON.parse(reply.body) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["challenge", "expires_in"]);
    expect(body.challenge).toMatch(CHALLENGE_PATTERN);
    expect(body.expires_in).toBe(CHALLENGE_TTL_MS / 1000);
  });

  it("never reveals the verifier: the body is only the hash", () => {
    const store = new HandoffChallenges();
    const reply = ask("GET", CHALLENGE_PATH, store);
    const { challenge } = JSON.parse(reply.body) as { challenge: string };
    const verifier = store.take(challenge) as string;
    expect(reply.body).not.toContain(verifier);
    expect(verifier).not.toBe(challenge);
  });

  it("is readable cross-origin and answers the private-network preflight", () => {
    const store = new HandoffChallenges();
    const get = ask("GET", CHALLENGE_PATH, store);
    expect(get.headers["access-control-allow-origin"]).toBe("*");
    expect(get.headers["access-control-allow-private-network"]).toBe("true");
    const preflight = ask("OPTIONS", CHALLENGE_PATH, store);
    expect(preflight.status).toBe(204);
    expect(preflight.headers["access-control-allow-methods"]).toBe("GET");
    expect(preflight.headers["access-control-allow-private-network"]).toBe("true");
    expect(store.size()).toBe(1); // the preflight issued nothing
  });

  it("refuses other methods and a rebinding Host header, and issues nothing for them", () => {
    const store = new HandoffChallenges();
    expect(ask("POST", CHALLENGE_PATH, store).status).toBe(405);
    expect(ask("PUT", CHALLENGE_PATH, store).status).toBe(405);
    expect(ask("GET", CHALLENGE_PATH, store, "evil.example:47821").status).toBe(404);
    expect(ask("GET", CHALLENGE_PATH, store, "127.0.0.1.evil.example:47821").status).toBe(404);
    expect(ask("GET", CHALLENGE_PATH, store, "127.0.0.1:80").status).toBe(404);
    expect(store.size()).toBe(0);
  });

  it("answers 429 when asked too often and 404 when there is no store (the web page then falls back)", () => {
    const store = new HandoffChallenges();
    for (let i = 0; i < MAX_ISSUES_PER_MINUTE; i += 1) expect(ask("GET", CHALLENGE_PATH, store).status).toBe(200);
    expect(ask("GET", CHALLENGE_PATH, store).status).toBe(429);
    expect(ask("GET", CHALLENGE_PATH, undefined).status).toBe(404);
  });

  it("leaves the original presence path exactly as it was (empty 204, no CORS grant)", () => {
    const reply = ask("GET", PRESENCE_PATH, new HandoffChallenges());
    expect(reply.status).toBe(204);
    expect(reply.body).toBe("");
    expect(reply.headers["access-control-allow-origin"]).toBeUndefined();
  });
});

// ------------------------------------------------------------------------------------ the link

const CODE = "Ab3-_".repeat(8) + "xyz";
const CHALLENGE = "Zy9_-".repeat(8) + "abc";

describe("parsing a link that carries a challenge", () => {
  it("accepts the legacy shape and the bound shape, and nothing else", () => {
    expect(parseHandoffLinkParts(`interviewassistant://start?h=${CODE}`)).toEqual({ code: CODE, challenge: null });
    expect(parseHandoffLinkParts(`interviewassistant://start?h=${CODE}&c=${CHALLENGE}`)).toEqual({ code: CODE, challenge: CHALLENGE });
    expect(parseHandoffLinkParts(`InterviewAssistant://start/?h=${CODE}&c=${CHALLENGE}`)).toEqual({ code: CODE, challenge: CHALLENGE });
  });

  it.each([
    ["challenge first", `interviewassistant://start?c=${CHALLENGE}&h=${CODE}`],
    ["a repeated parameter", `interviewassistant://start?h=${CODE}&c=${CHALLENGE}&c=${CHALLENGE}`],
    ["an extra parameter", `interviewassistant://start?h=${CODE}&c=${CHALLENGE}&x=1`],
    ["a short challenge", `interviewassistant://start?h=${CODE}&c=short`],
    ["a long challenge", `interviewassistant://start?h=${CODE}&c=${CHALLENGE}A`],
    ["an empty challenge", `interviewassistant://start?h=${CODE}&c=`],
    ["a fragment", `interviewassistant://start?h=${CODE}&c=${CHALLENGE}#x`],
    ["a path", `interviewassistant://start/x?h=${CODE}&c=${CHALLENGE}`],
    ["userinfo", `interviewassistant://u:p@start?h=${CODE}&c=${CHALLENGE}`],
    ["a space", `interviewassistant://start?h=${CODE}&c=${CHALLENGE} `],
    ["a backslash", `interviewassistant://start?h=${CODE}&c=${"\\".repeat(43)}`],
    ["a verifier-looking parameter", `interviewassistant://start?h=${CODE}&v=${CHALLENGE}`],
    ["another scheme", `https://start?h=${CODE}&c=${CHALLENGE}`],
  ])("refuses %s", (_name, link) => {
    expect(parseHandoffLinkParts(link)).toBeNull();
  });

  it("refuses non-strings and oversized input without throwing", () => {
    for (const value of [undefined, null, 42, {}, [], "x".repeat(5000)]) {
      expect(() => parseHandoffLinkParts(value)).not.toThrow();
      expect(parseHandoffLinkParts(value)).toBeNull();
    }
  });
});

// -------------------------------------------------------------------------------- the controller

const USER = "00000000-0000-4000-8000-0000000000aa";
const SESSION = { id: "11111111-2222-4333-8444-555555555555", title: "Backend", company_name: "Acme", status: "ready" };
const VERIFIER = "V".repeat(43);

function minted(): RedeemedHandoff & { commit: ReturnType<typeof vi.fn>; discard: ReturnType<typeof vi.fn> } {
  return { userId: USER, session: SESSION, commit: vi.fn(), discard: vi.fn(async () => undefined) } as never;
}

function setup(overrides: Partial<HandoffPorts> = {}) {
  const emitted: HandoffState[] = [];
  const grant = minted();
  const ports: HandoffPorts = {
    settled: async () => undefined,
    currentUserId: () => null,
    redeem: vi.fn(async () => grant),
    takeVerifier: vi.fn(() => VERIFIER),
    isLive: () => false,
    stopLive: vi.fn(),
    resetForAccountChange: vi.fn(),
    confirmSwitch: vi.fn(async () => true),
    confirmAccountSwitch: vi.fn(async () => true),
    emit: (state) => emitted.push(state),
    showWindow: vi.fn(),
    now: () => 1_000_000,
    ...overrides,
  };
  const controller = new HandoffController(ports);
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  return { controller, ports, emitted, grant, flush };
}

const BOUND = `interviewassistant://start?h=${CODE}&c=${CHALLENGE}`;
const LEGACY = `interviewassistant://start?h=${CODE}`;

describe("redeeming a bound link", () => {
  it("sends the code together with the verifier this app made for the challenge", async () => {
    const t = setup();
    t.controller.receiveLink(BOUND);
    await t.flush();
    expect(t.ports.takeVerifier).toHaveBeenCalledExactlyOnceWith(CHALLENGE);
    expect(t.ports.redeem).toHaveBeenCalledExactlyOnceWith(CODE, VERIFIER);
    expect(t.controller.getState().phase).toBe("ready");
    expect(t.grant.commit).toHaveBeenCalledTimes(1);
  });

  it("never sends the code when the verifier is gone (expired, already used, or never ours)", async () => {
    const t = setup({ takeVerifier: vi.fn(() => null) });
    t.controller.receiveLink(BOUND);
    await t.flush();
    expect(t.ports.redeem).not.toHaveBeenCalled(); // an unbound attempt for a bound code would only burn it
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "expired" });
    expect(t.grant.commit).not.toHaveBeenCalled();
  });

  it("replaying the same link a second time cannot redeem: the verifier was single use", async () => {
    const verifiers = [VERIFIER];
    const t = setup({ takeVerifier: vi.fn(() => verifiers.shift() ?? null) });
    t.controller.receiveLink(BOUND);
    await t.flush();
    t.controller.receiveLink(BOUND);
    await t.flush();
    expect(t.ports.redeem).toHaveBeenCalledTimes(1);
    expect(t.controller.getState().failure).toBe("expired");
  });

  it("a rejected redemption (wrong or burned code) is a fixed failure and the verifier is already spent", async () => {
    const rejected = Object.assign(new Error("no"), { details: { status: 404 } });
    const verifiers = [VERIFIER];
    const t = setup({ redeem: vi.fn(async () => Promise.reject(rejected)), takeVerifier: vi.fn(() => verifiers.shift() ?? null) });
    t.controller.receiveLink(BOUND);
    await t.flush();
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "invalid" });
    t.controller.receiveLink(BOUND);
    await t.flush();
    expect(t.ports.redeem).toHaveBeenCalledTimes(1);
  });

  it("takes the verifier only after the capture confirmation, so declining keeps it for the user's retry", async () => {
    const t = setup({ isLive: () => true, confirmSwitch: vi.fn(async () => false) });
    t.controller.receiveLink(BOUND);
    await t.flush();
    expect(t.ports.takeVerifier).not.toHaveBeenCalled();
    expect(t.controller.getState().failure).toBe("declined");
  });

  it("does not take a verifier at all for a legacy link, and redeems it exactly as before", async () => {
    const t = setup();
    t.controller.receiveLink(LEGACY);
    await t.flush();
    expect(t.ports.takeVerifier).not.toHaveBeenCalled();
    expect(t.ports.redeem).toHaveBeenCalledExactlyOnceWith(CODE);
    expect(t.controller.getState().phase).toBe("ready");
  });

  it("refuses a link with a malformed challenge before anything is looked up", async () => {
    const t = setup();
    t.controller.receiveLink(`interviewassistant://start?h=${CODE}&c=short`);
    await t.flush();
    expect(t.ports.takeVerifier).not.toHaveBeenCalled();
    expect(t.ports.redeem).not.toHaveBeenCalled();
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "invalid" });
  });

  it("keeps the verifier out of every state the renderer can see", async () => {
    const t = setup();
    t.controller.receiveLink(BOUND);
    await t.flush();
    const everything = JSON.stringify([t.emitted, t.controller.getState(), IDLE_HANDOFF]);
    expect(everything).not.toContain(VERIFIER);
    expect(everything).not.toContain(CHALLENGE);
    expect(everything).not.toContain(CODE);
  });
});
