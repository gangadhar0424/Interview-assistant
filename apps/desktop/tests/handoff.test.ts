/**
 * Web -> desktop handoff controller (D-55/D-56): state transitions with fake ports. The code is
 * redeemed at once with NO desktop login; the minted login is adopted only when it is safe to.
 * Covers cold start, an already-signed-in desktop, another account, running captures, local
 * expiry, supersession, sign-out and the fixed failure kinds. No Electron, no network.
 */
import { describe, expect, it, vi } from "vitest";
import {
  classifyRedeemError,
  HANDOFF_CODE_TTL_MS,
  HandoffController,
  type HandoffPorts,
  type RedeemedHandoff,
} from "../src/main/handoff";
import { IDLE_HANDOFF, type HandoffState } from "../src/shared/contracts";

const CODE = "Ab3-_".repeat(8) + "xyz";
const LINK = `interviewassistant://start?h=${CODE}`;
const USER = "00000000-0000-4000-8000-0000000000aa";
const OTHER_USER = "00000000-0000-4000-8000-0000000000bb";
const SESSION = { id: "11111111-2222-4333-8444-555555555555", title: "Backend", company_name: "Acme", status: "ready" };

function redeemed(overrides: Partial<RedeemedHandoff> = {}): RedeemedHandoff & { commit: ReturnType<typeof vi.fn>; discard: ReturnType<typeof vi.fn> } {
  return {
    userId: USER,
    session: SESSION,
    commit: vi.fn(),
    discard: vi.fn(async () => undefined),
    ...overrides,
  } as never;
}

function setup(overrides: Partial<HandoffPorts> = {}, minted = redeemed()) {
  const clock = { now: 1_000_000 };
  const emitted: HandoffState[] = [];
  const flags = { current: null as string | null, live: false };
  const ports: HandoffPorts = {
    settled: async () => undefined,
    currentUserId: () => flags.current,
    redeem: vi.fn(async () => minted),
    isLive: () => flags.live,
    stopLive: vi.fn(),
    resetForAccountChange: vi.fn(),
    confirmSwitch: vi.fn(async () => true),
    confirmAccountSwitch: vi.fn(async () => true),
    emit: (state) => emitted.push(state),
    showWindow: vi.fn(),
    now: () => clock.now,
    ...overrides,
  };
  const controller = new HandoffController(ports);
  const phases = () => emitted.map((s) => s.phase);
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  return { controller, ports, clock, emitted, flags, phases, flush, minted };
}

describe("a link with nobody signed in on the desktop (cold start)", () => {
  it("redeems at once, adopts the minted login and enters READY: no sign-in step", async () => {
    const t = setup();
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.redeem).toHaveBeenCalledExactlyOnceWith(CODE);
    expect(t.minted.commit).toHaveBeenCalledTimes(1);
    expect(t.minted.discard).not.toHaveBeenCalled();
    expect(t.phases()).toEqual(["redeeming", "ready"]);
    expect(t.controller.getState()).toEqual({
      phase: "ready",
      session: { id: SESSION.id, title: "Backend", company_name: "Acme" },
      failure: null,
    });
    expect(t.ports.showWindow).toHaveBeenCalled();
  });

  it("never asks to confirm anything when nothing is signed in and nothing is running", async () => {
    const t = setup();
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.confirmSwitch).not.toHaveBeenCalled();
    expect(t.ports.confirmAccountSwitch).not.toHaveBeenCalled();
    expect(t.ports.resetForAccountChange).not.toHaveBeenCalled();
  });

  it("never exposes the code, the user id or any token in an emitted state", async () => {
    const t = setup();
    t.controller.receiveLink(LINK);
    await t.flush();
    const text = JSON.stringify(t.emitted);
    expect(text).not.toContain(CODE);
    expect(text).not.toContain(USER);
    expect(text).not.toMatch(/status|token|access|refresh|userId/i);
  });

  it("waits for a sign-in already in flight before redeeming, so two logins never race", async () => {
    const order: string[] = [];
    let finishAuth: () => void = () => undefined;
    const t = setup({
      settled: () => new Promise<void>((resolve) => (finishAuth = () => { order.push("auth-done"); resolve(); })),
      redeem: vi.fn(async () => { order.push("redeem"); return redeemed(); }),
    });
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(order).toEqual([]);
    finishAuth();
    await t.flush();
    expect(order).toEqual(["auth-done", "redeem"]);
  });
});

describe("a link while the same user is already signed in on the desktop", () => {
  it("keeps the existing login and drops the spare one the server minted", async () => {
    const t = setup();
    t.flags.current = USER;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.minted.commit).not.toHaveBeenCalled();
    expect(t.minted.discard).toHaveBeenCalledTimes(1);
    expect(t.ports.confirmAccountSwitch).not.toHaveBeenCalled();
    expect(t.controller.getState().phase).toBe("ready");
  });
});

describe("a link for a different account than the one signed in", () => {
  it("asks first, then replaces the account and ends the old account's work", async () => {
    const order: string[] = [];
    const minted = redeemed({ userId: OTHER_USER, commit: vi.fn(() => order.push("commit")) as never });
    const t = setup(
      {
        confirmAccountSwitch: vi.fn(async () => { order.push("confirm"); return true; }),
        resetForAccountChange: vi.fn(() => order.push("reset")),
      },
      minted,
    );
    t.flags.current = USER;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(order).toEqual(["confirm", "reset", "commit"]);
    expect(t.controller.getState().phase).toBe("ready");
  });

  it("declining keeps the current account and gives the minted login up", async () => {
    const minted = redeemed({ userId: OTHER_USER });
    const t = setup({ confirmAccountSwitch: vi.fn(async () => false) }, minted);
    t.flags.current = USER;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(minted.commit).not.toHaveBeenCalled();
    expect(minted.discard).toHaveBeenCalledTimes(1);
    expect(t.ports.resetForAccountChange).not.toHaveBeenCalled();
    expect(t.ports.stopLive).not.toHaveBeenCalled();
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "declined" });
  });
});

describe("a malformed link", () => {
  it.each(["", "interviewassistant://start?h=short", "https://example.com", `${LINK}&x=1`])(
    "is refused without contacting the server (%s)",
    async (raw) => {
      const t = setup();
      t.controller.receiveLink(raw);
      await t.flush();
      expect(t.ports.redeem).not.toHaveBeenCalled();
      expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "invalid" });
    },
  );
});

describe("redemption answers", () => {
  it("a session that is not ready is refused and its login is given up", async () => {
    const minted = redeemed({ session: { ...SESSION, status: "ended" } });
    const t = setup({}, minted);
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(minted.commit).not.toHaveBeenCalled();
    expect(minted.discard).toHaveBeenCalledTimes(1);
    expect(t.controller.getState().failure).toBe("invalid");
  });

  it("redeeming the same link twice is the server's job: the second answer is invalid", async () => {
    const redeem = vi.fn().mockResolvedValueOnce(redeemed()).mockRejectedValueOnce({ details: { status: 404 } });
    const t = setup({ redeem });
    t.controller.receiveLink(LINK);
    await t.flush();
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "invalid" });
  });

  it.each([
    [404, "invalid"],
    [429, "rate_limited"],
    [503, "unavailable"],
    [500, "unavailable"],
    [undefined, "unavailable"],
  ])("status %s becomes %s and no server text is kept", async (status, failure) => {
    const error = { details: { status, message: "SERVER-SECRET-TEXT" }, message: "SERVER-SECRET-TEXT" };
    const t = setup({ redeem: vi.fn().mockRejectedValue(error) });
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure });
    expect(JSON.stringify(t.emitted)).not.toContain("SERVER-SECRET-TEXT");
  });

  it("classifyRedeemError copes with anything thrown", () => {
    for (const value of [null, undefined, "x", 3, new Error("boom"), {}]) {
      expect(classifyRedeemError(value)).toBe("unavailable");
    }
    expect(classifyRedeemError({ details: { status: 404 } })).toBe("invalid");
  });
});

describe("a running capture is never switched silently", () => {
  it("asks first, and only then redeems and stops the capture", async () => {
    const order: string[] = [];
    const t = setup({
      confirmSwitch: vi.fn(async () => { order.push("confirm"); return true; }),
      redeem: vi.fn(async () => { order.push("redeem"); return redeemed(); }),
      stopLive: vi.fn(() => order.push("stop")),
    });
    t.flags.current = USER;
    t.flags.live = true;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(order).toEqual(["confirm", "redeem", "stop"]);
    expect(t.controller.getState().phase).toBe("ready");
  });

  it("declining keeps the capture, never redeems and never consumes the code", async () => {
    const t = setup({ confirmSwitch: vi.fn(async () => false) });
    t.flags.current = USER;
    t.flags.live = true;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.redeem).not.toHaveBeenCalled();
    expect(t.ports.stopLive).not.toHaveBeenCalled();
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "declined" });
  });

  it("a code that expired while the dialog was open is not redeemed", async () => {
    const t = setup();
    (t.ports.confirmSwitch as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      t.clock.now += HANDOFF_CODE_TTL_MS + 5;
      return true;
    });
    t.flags.current = USER;
    t.flags.live = true;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.redeem).not.toHaveBeenCalled();
    expect(t.ports.stopLive).not.toHaveBeenCalled();
    expect(t.controller.getState().failure).toBe("expired");
  });

  it("a failed redemption leaves the running capture untouched", async () => {
    const t = setup({ redeem: vi.fn().mockRejectedValue({ details: { status: 404 } }) });
    t.flags.current = USER;
    t.flags.live = true;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.stopLive).not.toHaveBeenCalled();
    expect(t.controller.getState().failure).toBe("invalid");
  });
});

describe("sign-out and supersession", () => {
  it("signing out clears a ready handoff", async () => {
    const t = setup();
    t.controller.receiveLink(LINK);
    await t.flush();
    t.controller.onSignedOut();
    expect(t.controller.getState()).toEqual(IDLE_HANDOFF);
  });

  it("an answer that arrives after sign-out is dropped, adopted nowhere and given up", async () => {
    let finish: (v: RedeemedHandoff) => void = () => undefined;
    const t = setup({ redeem: vi.fn(() => new Promise<RedeemedHandoff>((resolve) => (finish = resolve))) });
    t.controller.receiveLink(LINK);
    await t.flush();
    t.controller.onSignedOut();
    const late = redeemed();
    finish(late);
    await t.flush();
    expect(late.commit).not.toHaveBeenCalled();
    expect(late.discard).toHaveBeenCalledTimes(1);
    expect(t.controller.getState()).toEqual(IDLE_HANDOFF);
    expect(t.ports.stopLive).not.toHaveBeenCalled();
  });

  it("a newer link supersedes an in-flight older one", async () => {
    const OTHER = "Z".repeat(43);
    const resolvers: Array<(v: RedeemedHandoff) => void> = [];
    const t = setup({ redeem: vi.fn(() => new Promise<RedeemedHandoff>((resolve) => resolvers.push(resolve))) });
    t.controller.receiveLink(LINK);
    await t.flush();
    t.controller.receiveLink(`interviewassistant://start?h=${OTHER}`);
    await t.flush();
    const old = redeemed({ session: { ...SESSION, id: "old" } });
    const fresh = redeemed();
    resolvers[0]?.(old);
    resolvers[1]?.(fresh);
    await t.flush();
    expect(old.commit).not.toHaveBeenCalled();
    expect(old.discard).toHaveBeenCalledTimes(1);
    expect(t.controller.getState().session?.id).toBe(SESSION.id);
  });

  it("dismiss clears ready and failed notices only", async () => {
    const t = setup();
    t.controller.receiveLink(LINK);
    await t.flush();
    t.controller.dismiss();
    expect(t.controller.getState()).toEqual(IDLE_HANDOFF);
    t.controller.dismiss();
    expect(t.controller.getState()).toEqual(IDLE_HANDOFF);
  });
});
