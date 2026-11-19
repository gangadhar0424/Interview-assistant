/**
 * M-6 window placement: geometry and the WindowPlacementManager driven with fake displays and a fake
 * window. These are MOCKED tests: they prove our logic, not how real Windows behaves with several
 * monitors, mixed scaling, docking or sleep. That needs docs/MULTI_MONITOR_TESTING.md.
 */
import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_INSET,
  DPI_SLACK,
  MIN_VISIBLE_FRACTION,
  MIN_WINDOW,
  RECONCILE_MS,
  SETTLE_MS,
  WindowPlacementManager,
  displayForBounds,
  fitInside,
  parseSavedPlacement,
  resolvePlacement,
  savedDisplayIfPresent,
  visibleFraction,
  type DisplayInfo,
  type PlacementPorts,
  type Rect,
  type SavedPlacement,
} from "../src/main/window-placement";
import type { WindowMode } from "../src/shared/placement";

const wa = (x: number, y: number, width: number, height: number): Rect => ({ x, y, width, height });
const D1: DisplayInfo = { id: 1, bounds: wa(0, 0, 1920, 1080), workArea: wa(0, 0, 1920, 1040), scaleFactor: 1 };
const D2: DisplayInfo = { id: 2, bounds: wa(1920, 0, 2560, 1440), workArea: wa(1920, 0, 2560, 1400), scaleFactor: 1.5 };
const D3: DisplayInfo = { id: 3, bounds: wa(-1280, 0, 1280, 1024), workArea: wa(-1280, 0, 1280, 984), scaleFactor: 1 };
const COMPACT = { width: 390, height: 520 };
const EXPANDED = { width: 760, height: 740 };

const saved = (rect: Rect, display: DisplayInfo): SavedPlacement => ({ ...rect, display: { id: display.id, ...display.bounds } });

function harness(initialDisplays: DisplayInfo[], stored: Partial<Record<WindowMode, SavedPlacement>> = {}, quirk = false) {
  let displays = initialDisplays;
  const win = { bounds: wa(0, 0, 0, 0), minimized: false, visible: true, destroyed: false, quirkLeft: quirk ? 1 : 0 };
  const setBoundsCalls: Rect[] = [];
  const saves: Array<{ mode: WindowMode; placement: SavedPlacement }> = [];
  const timers: Array<{ at: number; fn: () => void; cancelled: boolean }> = [];
  let clock = 0;
  const ports: PlacementPorts = {
    displays: () => displays,
    primaryId: () => 1,
    window: {
      getBounds: () => ({ ...win.bounds }),
      setBounds: (bounds) => {
        setBoundsCalls.push({ ...bounds });
        // Mixed-DPI quirk: the first resize after crossing displays applies the wrong size.
        win.bounds = win.quirkLeft > 0 ? { ...bounds, width: bounds.width + 40, height: bounds.height + 30 } : { ...bounds };
        if (win.quirkLeft > 0) win.quirkLeft -= 1;
      },
      isMinimized: () => win.minimized,
      isVisible: () => win.visible,
      isDestroyed: () => win.destroyed,
    },
    load: () => stored,
    save: (mode, placement) => void saves.push({ mode, placement }),
    defaultSize: (mode) => (mode === "expanded" ? EXPANDED : COMPACT),
    schedule: (ms, fn) => {
      const timer = { at: clock + ms, fn, cancelled: false };
      timers.push(timer);
      return () => {
        timer.cancelled = true;
      };
    },
  };
  const manager = new WindowPlacementManager(ports);
  const advance = (ms: number) => {
    clock += ms;
    for (const timer of timers) {
      if (!timer.cancelled && timer.at <= clock) {
        timer.cancelled = true;
        timer.fn();
      }
    }
  };
  /** Create the window where the manager says (what index.ts does). */
  const open = (mode: WindowMode = "compact") => {
    win.bounds = manager.initialBounds(mode);
    return win.bounds;
  };
  /** The user drags the window: the OS moves it and fires move events. */
  const userMove = (to: Rect) => {
    win.bounds = { ...to };
    manager.onBoundsEvent();
    manager.onBoundsEvent();
    advance(SETTLE_MS + 1);
  };
  const setDisplays = (next: DisplayInfo[]) => {
    displays = next;
    manager.scheduleReconcile();
    manager.scheduleReconcile(); // docking fires bursts
    advance(RECONCILE_MS + 1);
  };
  return { manager, win, setBoundsCalls, saves, advance, open, userMove, setDisplays, ports };
}

describe("parseSavedPlacement", () => {
  const good = saved(wa(100, 100, 390, 520), D1);
  it("accepts a complete placement", () => {
    expect(parseSavedPlacement(good)).toEqual(good);
  });
  it("rejects everything unusable, including Windows' minimised sentinel", () => {
    const bad: unknown[] = [
      null, undefined, "x", 5, [], {}, { ...good, x: "100" }, { ...good, x: 1.5 }, { ...good, x: Number.NaN },
      { ...good, x: -32000, y: -32000 }, { ...good, y: -30000 }, { ...good, x: 1e9 }, { ...good, width: 50 },
      { ...good, height: 99999 }, { ...good, width: -390 }, { ...good, display: undefined }, { ...good, display: { ...good.display, id: -1 } },
      { ...good, display: { ...good.display, width: 0 } }, { ...good, display: "main" },
    ];
    for (const value of bad) expect(parseSavedPlacement(value), JSON.stringify(value)).toBeNull();
  });
});

describe("geometry", () => {
  it("visibleFraction counts only what lies on a display's work area", () => {
    expect(visibleFraction(wa(100, 100, 400, 400), [D1])).toBe(1);
    expect(visibleFraction(wa(1820, 100, 200, 100), [D1])).toBe(0.5); // half on D1, the rest off screen
    expect(visibleFraction(wa(1820, 100, 200, 100), [D1, D2])).toBe(1); // straddling two displays is fully visible
    expect(visibleFraction(wa(5000, 5000, 400, 400), [D1, D2])).toBe(0);
    expect(visibleFraction(wa(0, 1000, 400, 400), [D1])).toBeCloseTo(40 / 400); // under the taskbar is not visible
  });

  it("fitInside slides a window inside the work area, and shrinks it only if it cannot fit", () => {
    expect(fitInside(wa(1800, 900, 390, 520), D1.workArea)).toEqual(wa(1530, 520, 390, 520));
    expect(fitInside(wa(-50, -50, 390, 520), D1.workArea)).toEqual(wa(0, 0, 390, 520));
    expect(fitInside(wa(0, 0, 760, 740), wa(0, 0, 700, 600))).toEqual(wa(0, 0, 700, 600));
    expect(fitInside(wa(0, 0, 100, 100), D1.workArea)).toEqual(wa(0, 0, MIN_WINDOW.width, MIN_WINDOW.height));
  });

  it("displayForBounds picks the display holding most of the window, else the nearest, not the primary", () => {
    expect(displayForBounds(wa(1900, 0, 390, 520), [D1, D2]).id).toBe(2); // 370 of 390 px are on D2
    expect(displayForBounds(wa(8000, 100, 390, 520), [D1, D2]).id).toBe(2); // off the right: nearest is D2
    expect(displayForBounds(wa(-5000, 100, 390, 520), [D1, D2]).id).toBe(1);
    expect(displayForBounds(wa(-5000, 100, 390, 520), [D1, D2, D3]).id).toBe(3);
  });

  it("finds a saved display by id, or by identical bounds if the id changed, or reports it absent", () => {
    const s = saved(wa(2000, 50, 390, 520), D2);
    expect(savedDisplayIfPresent(s, [D1, D2])?.id).toBe(2);
    expect(savedDisplayIfPresent(s, [D1, { ...D2, id: 99 }])?.id).toBe(99);
    expect(savedDisplayIfPresent(s, [D1])).toBeNull();
    expect(savedDisplayIfPresent(s, [D1, { ...D2, id: 99, bounds: wa(1920, 0, 1920, 1080) }])).toBeNull();
  });
});

describe("resolvePlacement", () => {
  it("with nothing saved, opens at the top right of the primary display", () => {
    const r = resolvePlacement(null, [D2, D1], 1, COMPACT);
    expect(r.bounds).toEqual(wa(1920 - 390 - DEFAULT_INSET, DEFAULT_INSET, 390, 520));
    expect(r.relocated).toBe(false);
    expect(r.display.id).toBe(1);
  });

  it("keeps a saved position that is mostly visible, even straddling two displays", () => {
    const rect = wa(1800, 200, 390, 520);
    const r = resolvePlacement(saved(rect, D1), [D1, D2], 1, COMPACT);
    expect(r.bounds).toEqual(rect);
    expect(r.relocated).toBe(false);
    expect(MIN_VISIBLE_FRACTION).toBeGreaterThan(0.5);
  });

  it("relocates a saved position that is off every display, onto the NEAREST display", () => {
    const farRight = resolvePlacement(saved(wa(6000, 100, 390, 520), D2), [D1, D2], 1, COMPACT);
    expect(farRight.relocated).toBe(true);
    expect(farRight.display.id).toBe(2); // not the primary: D2 is nearer
    expect(visibleFraction(farRight.bounds, [D1, D2])).toBe(1);
    const farLeft = resolvePlacement(saved(wa(-3000, 100, 390, 520), D3), [D1, D2], 1, COMPACT);
    expect(farLeft.display.id).toBe(1);
  });

  it("prefers the display the window was on when it is still connected but smaller now", () => {
    const r = resolvePlacement(saved(wa(2000, 1300, 390, 520), D2), [D1, { ...D2, workArea: wa(1920, 0, 1280, 720) }], 1, COMPACT);
    expect(r.relocated).toBe(true);
    expect(r.display.id).toBe(2);
    expect(r.bounds.y + r.bounds.height).toBeLessThanOrEqual(720);
  });

  it("works on a single small display and never returns bounds larger than the work area", () => {
    const tiny: DisplayInfo = { id: 1, bounds: wa(0, 0, 800, 600), workArea: wa(0, 0, 800, 560), scaleFactor: 1 };
    const r = resolvePlacement(null, [tiny], 1, EXPANDED);
    expect(r.bounds.width).toBeLessThanOrEqual(800);
    expect(r.bounds.height).toBeLessThanOrEqual(560);
  });
});

describe("startup", () => {
  it("single monitor, first run: the default place on the only display", () => {
    const h = harness([D1]);
    expect(h.open()).toEqual(wa(1506, 24, 390, 520));
  });

  it("multiple monitors: restores the saved place on the second display exactly", () => {
    const rect = wa(2400, 300, 420, 600);
    const h = harness([D1, D2], { compact: saved(rect, D2) });
    expect(h.open()).toEqual(rect);
  });

  it("multiple monitors but the saved one is gone: opens inside a connected display, and remembers it was displaced", () => {
    const h = harness([D1], { compact: saved(wa(2400, 300, 390, 520), D2) });
    const bounds = h.open();
    expect(visibleFraction(bounds, [D1])).toBe(1);
    h.win.bounds = bounds;
    h.setDisplays([D1, D2]); // the monitor shows up while the app is running
    expect(h.win.bounds).toEqual(wa(2400, 300, 390, 520)); // goes back where the user left it
  });

  it("restores a window saved on a monitor left of the primary (negative coordinates)", () => {
    const rect = wa(-1000, 100, 390, 520);
    const h = harness([D3, D1], { compact: saved(rect, D3) });
    expect(h.open()).toEqual(rect);
  });

  it("opens the expanded window with its own remembered size", () => {
    const h = harness([D1], { expanded: saved(wa(300, 100, 900, 800), D1), compact: saved(wa(50, 50, 390, 520), D1) });
    expect(h.open("expanded")).toEqual(wa(300, 100, 900, 800));
  });

  it("never reads displays or saves anything when asked for its initial bounds", () => {
    const h = harness([D1]);
    h.open();
    expect(h.saves).toEqual([]);
    expect(h.setBoundsCalls).toEqual([]);
  });

  it("survives a system that reports no displays", () => {
    const h = harness([]);
    expect(h.open()).toEqual(wa(0, 0, 390, 520));
  });
});

describe("remembering where the user puts the window", () => {
  it("saves once after a drag settles, with the display it is on", () => {
    const h = harness([D1, D2]);
    h.open();
    h.userMove(wa(2100, 200, 390, 520));
    expect(h.saves).toHaveLength(1);
    expect(h.saves[0]).toMatchObject({ mode: "compact", placement: { x: 2100, y: 200, display: { id: 2 } } });
  });

  it("a drag is one change: many move events within the quiet period save once", () => {
    const h = harness([D1]);
    h.open();
    for (let i = 0; i < 30; i += 1) {
      h.win.bounds = wa(100 + i * 5, 100, 390, 520);
      h.manager.onBoundsEvent();
      h.advance(20); // shorter than the quiet period
    }
    expect(h.saves).toHaveLength(0);
    h.advance(SETTLE_MS + 1);
    expect(h.saves).toHaveLength(1);
    expect(h.saves[0]?.placement.x).toBe(100 + 29 * 5);
  });

  it("remembers a user resize as well as a move", () => {
    const h = harness([D1]);
    h.open();
    h.userMove(wa(1400, 24, 480, 700));
    expect(h.saves[0]?.placement).toMatchObject({ width: 480, height: 700 });
  });

  it("does not save our own moves echoing back, nor a minimised or hidden window", () => {
    const h = harness([D1, D2]);
    h.open();
    h.manager.moveToDisplay(2);
    const savesAfterMove = h.saves.length;
    h.manager.onBoundsEvent(); // the OS reports the move we just made
    h.advance(SETTLE_MS + 1);
    expect(h.saves).toHaveLength(savesAfterMove);

    h.win.minimized = true;
    h.win.bounds = wa(-32000, -32000, 390, 520); // what Windows reports for a minimised window
    h.manager.onBoundsEvent();
    h.advance(SETTLE_MS + 1);
    expect(h.saves).toHaveLength(savesAfterMove);

    h.win.minimized = false;
    h.win.visible = false;
    h.win.bounds = wa(300, 300, 390, 520);
    h.manager.onBoundsEvent();
    h.advance(SETTLE_MS + 1);
    expect(h.saves).toHaveLength(savesAfterMove);
  });

  it("a settings file that cannot be written does not break the window", () => {
    const h = harness([D1]);
    h.ports.save = () => {
      throw new Error("disk full");
    };
    h.open();
    expect(() => h.userMove(wa(500, 100, 390, 520))).not.toThrow();
  });
});

describe("normal use never moves the window", () => {
  it("a usable window is left alone by display events, show and restore", () => {
    const h = harness([D1, D2]);
    h.open();
    h.userMove(wa(2100, 200, 390, 520));
    h.setBoundsCalls.length = 0;
    h.setDisplays([D1, D2]);
    h.setDisplays([D1, D2]);
    h.manager.scheduleReconcile(); // what a show or restore event does
    h.advance(RECONCILE_MS + 1);
    expect(h.setBoundsCalls).toEqual([]);
  });

  it("a burst of display events is one reconciliation", () => {
    const h = harness([D1, D2]);
    h.open();
    h.userMove(wa(2100, 200, 390, 520));
    h.setBoundsCalls.length = 0;
    h.manager.scheduleReconcile();
    h.manager.scheduleReconcile();
    h.manager.scheduleReconcile();
    h.advance(RECONCILE_MS + 1);
    h.setDisplays([D1]);
    expect(h.setBoundsCalls).toHaveLength(1);
  });
});

describe("monitor disconnection and reconnection", () => {
  let h: ReturnType<typeof harness>;
  beforeEach(() => {
    h = harness([D1, D2]);
    h.open();
    h.userMove(wa(2100, 200, 390, 520));
    h.saves.length = 0;
    h.setBoundsCalls.length = 0;
  });

  it("moves a window whose monitor was unplugged onto a connected display, fully visible", () => {
    h.setDisplays([D1]);
    expect(visibleFraction(h.win.bounds, [D1])).toBe(1);
    expect(h.win.bounds.x).toBeLessThanOrEqual(1920 - 390);
  });

  it("keeps the user's preference (nothing is saved) so the monitor can get the window back", () => {
    h.setDisplays([D1]);
    expect(h.saves).toEqual([]);
    h.setDisplays([D1, D2]);
    expect(h.win.bounds).toEqual(wa(2100, 200, 390, 520));
    expect(h.saves).toEqual([]); // restoring is not a user choice either
  });

  it("does not restore if the user moved the window after the disconnect", () => {
    h.setDisplays([D1]);
    h.userMove(wa(300, 300, 390, 520));
    expect(h.saves).toHaveLength(1);
    h.setBoundsCalls.length = 0;
    h.setDisplays([D1, D2]);
    expect(h.setBoundsCalls).toEqual([]);
    expect(h.win.bounds).toEqual(wa(300, 300, 390, 520));
  });

  it("restores when the monitor returns under a new id but the same geometry", () => {
    h.setDisplays([D1]);
    h.setDisplays([D1, { ...D2, id: 77 }]);
    expect(h.win.bounds).toEqual(wa(2100, 200, 390, 520));
  });

  it("handles the only other monitor disappearing from a three-monitor setup, choosing the nearest", () => {
    const t = harness([D3, D1, D2]);
    t.open();
    t.userMove(wa(-900, 100, 390, 520));
    t.setDisplays([D1, D2]);
    expect(t.win.bounds.x).toBeGreaterThanOrEqual(0);
    expect(t.win.bounds.x + t.win.bounds.width).toBeLessThanOrEqual(1920); // nearest to D3 is D1, not D2
  });

  it("leaves a minimised window alone and fixes it when it is restored", () => {
    h.win.minimized = true;
    h.setDisplays([D1]);
    expect(h.setBoundsCalls).toEqual([]);
    h.win.minimized = false;
    h.manager.scheduleReconcile(); // the restore event
    h.advance(RECONCILE_MS + 1);
    expect(visibleFraction(h.win.bounds, [D1])).toBe(1);
  });
});

describe("resolution and DPI changes", () => {
  it("a smaller resolution pulls a now off-screen window back inside, and restoring it puts it back", () => {
    const h = harness([D1]);
    h.open();
    h.userMove(wa(1500, 500, 390, 520));
    h.saves.length = 0;
    const small: DisplayInfo = { id: 1, bounds: wa(0, 0, 1280, 720), workArea: wa(0, 0, 1280, 680), scaleFactor: 1 };
    h.setDisplays([small]);
    expect(h.win.bounds.x + h.win.bounds.width).toBeLessThanOrEqual(1280);
    expect(h.win.bounds.y + h.win.bounds.height).toBeLessThanOrEqual(680);
    h.setDisplays([D1]);
    expect(h.win.bounds).toEqual(wa(1500, 500, 390, 520));
    expect(h.saves).toEqual([]);
  });

  it("a scale change that leaves the window usable does not move it", () => {
    const h = harness([D1]);
    h.open();
    h.userMove(wa(600, 300, 390, 520));
    h.setBoundsCalls.length = 0;
    h.setDisplays([{ ...D1, scaleFactor: 1.75 }]);
    h.setDisplays([{ ...D1, scaleFactor: 2.5, workArea: wa(0, 0, 1920, 1040) }]);
    expect(h.setBoundsCalls).toEqual([]);
  });

  it("a taskbar that moves into the window's area nudges it out, only as far as needed", () => {
    const h = harness([D1]);
    h.open();
    h.userMove(wa(1500, 600, 390, 480));
    h.setDisplays([{ ...D1, workArea: wa(0, 0, 1920, 700) }]); // work area now ends at y=700
    expect(h.win.bounds.y + h.win.bounds.height).toBeLessThanOrEqual(700);
    expect(h.win.bounds.x).toBe(1500);
  });

  it("mixed-DPI quirk: if Windows applies the wrong size, it is set once more", () => {
    const h = harness([D1, D2], {}, true);
    h.open();
    h.manager.moveToDisplay(2);
    expect(h.setBoundsCalls).toHaveLength(2);
    expect(h.win.bounds.width).toBe(390);
    expect(h.win.bounds.height).toBe(520);
  });
});

describe("choosing a display", () => {
  it("lists displays left to right with primary, size, scale and which one holds the assistant", () => {
    const h = harness([D2, D1, D3]);
    h.open();
    const list = h.manager.listDisplays();
    expect(list.map((d) => d.id)).toEqual([3, 1, 2]);
    expect(list[1]).toMatchObject({ id: 1, primary: true, current: true, width: 1920, height: 1080, scalePercent: 100 });
    expect(list[2]).toMatchObject({ id: 2, primary: false, current: false, scalePercent: 150 });
    expect(list[1]?.label).toContain("Display 2 (primary) · 1920×1080 · 100%");
    expect(list.filter((d) => d.current)).toHaveLength(1);
  });

  it("moves the window to the middle of the chosen display, remembers it, and keeps its size", () => {
    const h = harness([D1, D2]);
    h.open();
    h.manager.moveToDisplay(2);
    expect(h.win.bounds).toEqual(wa(1920 + (2560 - 390) / 2, (1400 - 520) / 2, 390, 520));
    expect(h.saves.at(-1)?.placement.display.id).toBe(2);
    expect(h.manager.listDisplays().find((d) => d.id === 2)?.current).toBe(true);
  });

  it("works for a non-primary display and a negative-coordinate display, and a window too big for it is shrunk", () => {
    const h = harness([D3, D1]);
    h.open("expanded");
    h.manager.moveToDisplay(3);
    expect(h.win.bounds.x).toBeGreaterThanOrEqual(-1280);
    expect(h.win.bounds.x + h.win.bounds.width).toBeLessThanOrEqual(0);
    expect(h.win.bounds.y + h.win.bounds.height).toBeLessThanOrEqual(984);
  });

  it("refuses a display that is not connected, and changes nothing", () => {
    const h = harness([D1]);
    h.open();
    expect(() => h.manager.moveToDisplay(2)).toThrow(/no longer available/);
    expect(h.setBoundsCalls).toEqual([]);
    expect(h.saves).toEqual([]);
  });
});

describe("compact and expanded", () => {
  it("expanding near the bottom right keeps the window inside the display", () => {
    const h = harness([D1]);
    h.open();
    h.userMove(wa(1500, 500, 390, 520));
    h.manager.setMode(true);
    expect(h.win.bounds).toMatchObject({ width: 760, height: 740 });
    expect(visibleFraction(h.win.bounds, [D1])).toBe(1);
    expect(h.win.bounds.x + 760).toBeLessThanOrEqual(1920);
    expect(h.win.bounds.y + 740).toBeLessThanOrEqual(1040);
  });

  it("each mode remembers its own size and place, and returning to compact restores the compact place", () => {
    const h = harness([D1, D2]);
    h.open();
    h.userMove(wa(1000, 100, 420, 560));
    h.manager.setMode(true);
    h.userMove(wa(2300, 200, 800, 780)); // the user places the expanded window on D2
    h.manager.setMode(false);
    expect(h.win.bounds).toEqual(wa(1000, 100, 420, 560));
    h.manager.setMode(true);
    expect(h.win.bounds).toEqual(wa(2300, 200, 800, 780));
    expect(h.saves.some((s) => s.mode === "expanded")).toBe(true);
    expect(h.saves.some((s) => s.mode === "compact")).toBe(true);
  });

  it("a window larger than a small display shrinks to fit when expanded", () => {
    const tiny: DisplayInfo = { id: 1, bounds: wa(0, 0, 800, 600), workArea: wa(0, 0, 800, 560), scaleFactor: 1 };
    const h = harness([tiny]);
    h.open();
    h.manager.setMode(true);
    expect(h.win.bounds.width).toBeLessThanOrEqual(800);
    expect(h.win.bounds.height).toBeLessThanOrEqual(560);
  });

  it("switching mode right after a drag keeps that drag in the mode being left", () => {
    const h = harness([D1]);
    h.open();
    h.win.bounds = wa(700, 200, 390, 520);
    h.manager.onBoundsEvent(); // not settled yet
    h.manager.setMode(true);
    expect(h.saves.find((s) => s.mode === "compact")?.placement).toMatchObject({ x: 700, y: 200 });
  });
});

describe("application restart", () => {
  it("what one run saved, the next run restores", () => {
    const first = harness([D1, D2]);
    first.open();
    first.userMove(wa(2200, 250, 400, 540));
    first.manager.setMode(true);
    first.userMove(wa(2000, 100, 820, 800));
    const store: Partial<Record<WindowMode, SavedPlacement>> = {};
    for (const s of first.saves) store[s.mode] = s.placement;

    const second = harness([D1, D2], store);
    expect(second.open("expanded")).toEqual(wa(2000, 100, 820, 800));
    const third = harness([D1, D2], store);
    expect(third.open("compact")).toEqual(wa(2200, 250, 400, 540));
  });

  it("restarting after the monitor was removed still opens on a connected display", () => {
    const first = harness([D1, D2]);
    first.open();
    first.userMove(wa(2200, 250, 400, 540));
    const store: Partial<Record<WindowMode, SavedPlacement>> = {};
    for (const s of first.saves) store[s.mode] = s.placement;
    const second = harness([D1], store);
    expect(visibleFraction(second.open(), [D1])).toBe(1);
  });
});

describe("shutdown", () => {
  it("dispose saves a move that had not settled, then ignores everything and can be repeated", () => {
    const h = harness([D1]);
    h.open();
    h.win.bounds = wa(800, 300, 390, 520);
    h.manager.onBoundsEvent();
    h.manager.dispose();
    expect(h.saves.at(-1)?.placement).toMatchObject({ x: 800, y: 300 });
    const count = h.saves.length;
    h.manager.dispose();
    h.win.bounds = wa(10, 10, 390, 520);
    h.manager.onBoundsEvent();
    h.manager.scheduleReconcile();
    h.advance(SETTLE_MS + RECONCILE_MS + 1);
    expect(h.saves).toHaveLength(count);
  });

  it("does nothing for a destroyed window", () => {
    const h = harness([D1, D2]);
    h.open();
    h.win.destroyed = true;
    h.setBoundsCalls.length = 0;
    expect(() => {
      h.manager.setMode(true);
      h.manager.reconcileNow();
      h.manager.settle();
      h.manager.listDisplays();
    }).not.toThrow();
    expect(h.setBoundsCalls).toEqual([]);
  });
});

describe("fractional-scaling rounding (measured on real Windows at 125%)", () => {
  it("a window that comes back a few pixels wider is not saved wider, so restarts never make it creep", () => {
    const h = harness([D1]);
    const initial = h.open();
    h.win.bounds = { ...initial, width: initial.width + 5 }; // what Electron reported at 125%
    h.manager.onBoundsEvent();
    h.advance(SETTLE_MS + 1);
    expect(h.saves).toEqual([]); // rounding is not a user change

    h.win.bounds = wa(200, 100, 395, 520); // the user drags it; the rounded width comes along
    h.manager.onBoundsEvent();
    h.advance(SETTLE_MS + 1);
    expect(h.saves).toHaveLength(1);
    expect(h.saves[0]?.placement).toMatchObject({ x: 200, y: 100, width: 390, height: 520 }); // the intended size

    // Five restarts in a row, each creating the window a few pixels wider than asked.
    let store: Partial<Record<WindowMode, SavedPlacement>> = { compact: h.saves[0]!.placement };
    for (let i = 0; i < 5; i += 1) {
      const next = harness([D1], store);
      const created = next.open();
      next.win.bounds = { ...created, width: created.width + 5 };
      next.win.bounds = { ...next.win.bounds, x: next.win.bounds.x + 10 };
      next.manager.onBoundsEvent();
      next.advance(SETTLE_MS + 1);
      if (next.saves[0]) store = { compact: next.saves[0].placement };
      expect(store.compact?.width).toBe(390);
    }
  });

  it("a real user resize (well beyond rounding) is saved", () => {
    const h = harness([D1]);
    h.open();
    h.userMove(wa(1300, 24, 520, 700));
    expect(h.saves[0]?.placement).toMatchObject({ width: 520, height: 700 });
  });

  it("applying a size and getting back a slightly wider one does not trigger a second resize or a save", () => {
    const h = harness([D1, D2]);
    h.open();
    const original = h.ports.window.setBounds;
    h.ports.window.setBounds = (b) => original({ ...b, width: b.width + 7 });
    h.manager.moveToDisplay(2);
    expect(h.setBoundsCalls).toHaveLength(1); // no retry for rounding
    const saves = h.saves.length;
    h.manager.onBoundsEvent();
    h.advance(SETTLE_MS + 1);
    expect(h.saves).toHaveLength(saves);
    expect(h.saves.at(-1)?.placement.width).toBe(390);
  });

  it("a relocated window keeps a few pixels from the right and bottom edges, so rounding cannot push it off", () => {
    const r = resolvePlacement(saved(wa(6000, 5000, 390, 520), D1), [D1], 1, COMPACT);
    expect(r.relocated).toBe(true);
    expect(r.bounds.x + r.bounds.width).toBeLessThanOrEqual(D1.workArea.x + D1.workArea.width - DPI_SLACK);
    expect(r.bounds.y + r.bounds.height).toBeLessThanOrEqual(D1.workArea.y + D1.workArea.height - DPI_SLACK);
  });
});
