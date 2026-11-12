/**
 * Web -> desktop handoff controller (D-55): state transitions with fake ports. Covers redemption
 * once signed in, waiting for sign-in, local expiry, supersession, sign-out, capture confirmation
 * and the fixed failure kinds. No Electron, no network.
 */
import { describe, expect, it, vi } from "vitest";
import { classifyRedeemError, HANDOFF_CODE_TTL_MS, HandoffController, type HandoffPorts } from "../src/main/handoff";
import { IDLE_HANDOFF, type HandoffState } from "../src/shared/contracts";

const CODE = "Ab3-_".repeat(8) + "xyz";
const LINK = `interviewassistant://start?h=${CODE}`;
const SESSION = { id: "11111111-2222-4333-8444-555555555555", title: "Backend", company_name: "Acme", status: "ready" };

class Timers {
  now = 1_000_000;
  private jobs: Array<{ at: number; fn: () => void; live: boolean }> = [];
  schedule = (fn: () => void, ms: number) => {
    const job = { at: this.now + ms, fn, live: true };
    this.jobs.push(job);
    return () => {
      job.live = false;
    };
  };
  advance(ms: number) {
    this.now += ms;
    for (const job of this.jobs) {
      if (job.live && job.at <= this.now) {
        job.live = false;
        job.fn();
      }
    }
  }
}

function setup(overrides: Partial<HandoffPorts> = {}) {
  const timers = new Timers();
  const emitted: HandoffState[] = [];
  const flags = { signedIn: true, live: false };
  const ports: HandoffPorts = {
    isSignedIn: () => flags.signedIn,
    redeem: vi.fn(async () => SESSION),
    isLive: () => flags.live,
    stopLive: vi.fn(),
    confirmSwitch: vi.fn(async () => true),
    emit: (state) => emitted.push(state),
    showWindow: vi.fn(),
    onAuthExpired: vi.fn(),
    now: () => timers.now,
    schedule: timers.schedule,
    ...overrides,
  };
  const controller = new HandoffController(ports);
  const phases = () => emitted.map((s) => s.phase);
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  return { controller, ports, timers, emitted, flags, phases, flush };
}

describe("a link while signed in", () => {
  it("redeems once and enters READY with the prepared session", async () => {
    const t = setup();
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.redeem).toHaveBeenCalledExactlyOnceWith(CODE);
    expect(t.phases()).toEqual(["redeeming", "ready"]);
    expect(t.controller.getState()).toEqual({
      phase: "ready",
      session: { id: SESSION.id, title: "Backend", company_name: "Acme" },
      failure: null,
    });
    expect(t.ports.showWindow).toHaveBeenCalled();
  });

  it("never exposes the code or the session status in any emitted state", async () => {
    const t = setup();
    t.controller.receiveLink(LINK);
    await t.flush();
    const text = JSON.stringify(t.emitted);
    expect(text).not.toContain(CODE);
    expect(text).not.toContain("status");
  });

  it("does not stop anything when nothing is running", async () => {
    const t = setup();
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.confirmSwitch).not.toHaveBeenCalled();
    expect(t.ports.stopLive).toHaveBeenCalledTimes(1); // the existing session-change stop, a no-op when idle
  });

  it("redeeming the same link twice is the server's job: the second answer is invalid", async () => {
    const redeem = vi.fn().mockResolvedValueOnce(SESSION).mockRejectedValueOnce({ details: { status: 404 } });
    const t = setup({ redeem });
    t.controller.receiveLink(LINK);
    await t.flush();
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "invalid" });
  });

  it("a session that is not ready is refused", async () => {
    const t = setup({ redeem: vi.fn(async () => ({ ...SESSION, status: "ended" })) });
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "invalid" });
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

  it("a bad link replaces and cancels a pending good one", async () => {
    const t = setup();
    t.flags.signedIn = false;
    t.controller.receiveLink(LINK);
    t.controller.receiveLink("garbage");
    t.flags.signedIn = true;
    t.controller.onSignedIn();
    await t.flush();
    expect(t.ports.redeem).not.toHaveBeenCalled();
  });
});

describe("a link while signed out", () => {
  it("waits for sign-in and redeems only after it", async () => {
    const t = setup();
    t.flags.signedIn = false;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.controller.getState().phase).toBe("waiting_signin");
    expect(t.ports.redeem).not.toHaveBeenCalled();
    t.flags.signedIn = true;
    t.controller.onSignedIn();
    await t.flush();
    expect(t.ports.redeem).toHaveBeenCalledExactlyOnceWith(CODE);
    expect(t.controller.getState().phase).toBe("ready");
  });

  it("a code that outlived its lifetime is dropped locally and never sent", async () => {
    const t = setup();
    t.flags.signedIn = false;
    t.controller.receiveLink(LINK);
    t.timers.advance(HANDOFF_CODE_TTL_MS + 1);
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure: "expired" });
    t.flags.signedIn = true;
    t.controller.onSignedIn();
    await t.flush();
    expect(t.ports.redeem).not.toHaveBeenCalled();
  });

  it("signing in just inside the lifetime still redeems", async () => {
    const t = setup();
    t.flags.signedIn = false;
    t.controller.receiveLink(LINK);
    t.timers.advance(HANDOFF_CODE_TTL_MS - 1_000);
    t.flags.signedIn = true;
    t.controller.onSignedIn();
    await t.flush();
    expect(t.controller.getState().phase).toBe("ready");
  });

  it("onSignedIn with nothing pending does nothing", async () => {
    const t = setup();
    t.controller.onSignedIn();
    await t.flush();
    expect(t.ports.redeem).not.toHaveBeenCalled();
    expect(t.emitted).toEqual([]);
  });

  it("the code is used for a single attempt only", async () => {
    const t = setup();
    t.flags.signedIn = false;
    t.controller.receiveLink(LINK);
    t.flags.signedIn = true;
    t.controller.onSignedIn();
    t.controller.onSignedIn(); // e.g. the restored-session hook plus the login hook
    await t.flush();
    expect(t.ports.redeem).toHaveBeenCalledTimes(1);
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

  it("signing out while waiting forgets the code", async () => {
    const t = setup();
    t.flags.signedIn = false;
    t.controller.receiveLink(LINK);
    t.controller.onSignedOut();
    t.flags.signedIn = true;
    t.controller.onSignedIn();
    await t.flush();
    expect(t.ports.redeem).not.toHaveBeenCalled();
  });

  it("an answer that arrives after sign-out is ignored", async () => {
    let finish: (v: typeof SESSION) => void = () => undefined;
    const redeem = vi.fn(() => new Promise<typeof SESSION>((resolve) => (finish = resolve)));
    const t = setup({ redeem });
    t.controller.receiveLink(LINK);
    await t.flush();
    t.controller.onSignedOut();
    finish(SESSION);
    await t.flush();
    expect(t.controller.getState()).toEqual(IDLE_HANDOFF);
    expect(t.ports.stopLive).not.toHaveBeenCalled();
  });

  it("a newer link supersedes an in-flight older one", async () => {
    const OTHER = "Z".repeat(43);
    const resolvers: Array<(v: typeof SESSION) => void> = [];
    const redeem = vi.fn(() => new Promise<typeof SESSION>((resolve) => resolvers.push(resolve)));
    const t = setup({ redeem });
    t.controller.receiveLink(LINK);
    await t.flush();
    t.controller.receiveLink(`interviewassistant://start?h=${OTHER}`);
    await t.flush();
    resolvers[0]?.({ ...SESSION, id: "old" });
    resolvers[1]?.(SESSION);
    await t.flush();
    expect(t.controller.getState().session?.id).toBe(SESSION.id);
  });

  it("dismiss clears ready and failed notices only", async () => {
    const t = setup();
    t.controller.receiveLink(LINK);
    await t.flush();
    t.controller.dismiss();
    expect(t.controller.getState()).toEqual(IDLE_HANDOFF);
    t.flags.signedIn = false;
    t.controller.receiveLink(LINK);
    t.controller.dismiss(); // waiting is not dismissable
    expect(t.controller.getState().phase).toBe("waiting_signin");
  });
});

describe("a running capture is never switched silently", () => {
  it("asks first, and only then redeems and stops the capture", async () => {
    const order: string[] = [];
    const t = setup({
      confirmSwitch: vi.fn(async () => {
        order.push("confirm");
        return true;
      }),
      redeem: vi.fn(async () => {
        order.push("redeem");
        return SESSION;
      }),
      stopLive: vi.fn(() => order.push("stop")),
    });
    t.flags.live = true;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(order).toEqual(["confirm", "redeem", "stop"]);
    expect(t.controller.getState().phase).toBe("ready");
  });

  it("declining keeps the capture and never redeems", async () => {
    const t = setup({ confirmSwitch: vi.fn(async () => false) });
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
      t.timers.now += HANDOFF_CODE_TTL_MS + 5;
      return true;
    });
    t.flags.live = true;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.redeem).not.toHaveBeenCalled();
    expect(t.ports.stopLive).not.toHaveBeenCalled();
    expect(t.controller.getState().failure).toBe("expired");
  });

  it("a failed redemption leaves the running capture untouched", async () => {
    const t = setup({ redeem: vi.fn().mockRejectedValue({ details: { status: 404 } }) });
    t.flags.live = true;
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.stopLive).not.toHaveBeenCalled();
    expect(t.controller.getState().failure).toBe("invalid");
  });
});

describe("failures are fixed kinds, never server text", () => {
  it.each([
    [404, "invalid"],
    [429, "rate_limited"],
    [503, "unavailable"],
    [500, "unavailable"],
    [undefined, "unavailable"],
  ])("status %s becomes %s", async (status, failure) => {
    const error = { details: { status, message: "SERVER-SECRET-TEXT" }, message: "SERVER-SECRET-TEXT" };
    const t = setup({ redeem: vi.fn().mockRejectedValue(error) });
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.controller.getState()).toEqual({ phase: "failed", session: null, failure });
    expect(JSON.stringify(t.emitted)).not.toContain("SERVER-SECRET-TEXT");
  });

  it("an unauthorized answer follows the normal expired-session path", async () => {
    const t = setup({ redeem: vi.fn().mockRejectedValue({ details: { status: 401 } }) });
    t.controller.receiveLink(LINK);
    await t.flush();
    expect(t.ports.onAuthExpired).toHaveBeenCalledTimes(1);
    expect(t.controller.getState().failure).toBe("signed_out");
  });

  it("classifyRedeemError copes with anything thrown", () => {
    for (const value of [null, undefined, "x", 3, new Error("boom"), {}]) {
      expect(classifyRedeemError(value)).toBe("unavailable");
    }
    expect(classifyRedeemError({ details: { status: 404 } })).toBe("invalid");
  });
});
