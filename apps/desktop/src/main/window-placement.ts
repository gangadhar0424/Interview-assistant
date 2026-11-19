/**
 * Window placement across displays (M-6, D-71). Pure geometry plus a manager over injected ports, so
 * everything is unit-tested with fake displays and a fake window and no Electron. Mocked tests cannot
 * establish how real Windows behaves with several monitors, mixed scaling, docking or sleep; that
 * needs the manual checks in docs/MULTI_MONITOR_TESTING.md.
 *
 * Principles:
 *  - The user's last placement is the preference (per mode: compact and expanded). It changes only
 *    when the USER moves or resizes the window (or picks a display); never because we relocated it.
 *  - Normal use never moves the window. It is moved only at startup, on a display change (after a
 *    short quiet period, because docking fires bursts of events), when its size changes (a mode
 *    switch) or when the user asks.
 *  - A window that is at least 60% visible is left exactly where it is (it may straddle two
 *    displays). Otherwise it is fitted fully inside a work area, preferring the display it was on,
 *    then the NEAREST display (never "the primary" by assumption).
 *  - When it had to be relocated, the preference is kept, so if the monitor comes back (or the
 *    resolution is restored) it returns to where the user put it, unless the user has moved it since.
 *  - Saved coordinates are never trusted: invalid, absurd, or Windows' "minimised" sentinel values are
 *    discarded and the default placement is used.
 *
 * This module never touches always-on-top, opacity, audio, screenshots or any service, and nothing
 * here logs.
 */
import type { DisplayChoice, WindowMode } from "../shared/placement";

export type Rect = { x: number; y: number; width: number; height: number };
export type DisplayInfo = { id: number; bounds: Rect; workArea: Rect; scaleFactor: number };
export type SavedPlacement = Rect & { display: Rect & { id: number } };

export const MIN_WINDOW = { width: 360, height: 460 } as const;
export const DEFAULT_INSET = 24;
export const MIN_VISIBLE_FRACTION = 0.6;
export const SETTLE_MS = 400;
/**
 * Electron on Windows converts window sizes between device-independent and physical pixels with
 * rounding, so at fractional scaling (125%, 150%) a window comes back a few DIP wider than asked, and
 * re-applying what it reported makes it wider still (measured on real Windows at 125%: a requested
 * 390 came back 391-397, and re-applying 395 gave 397). To stop the window creeping wider on every
 * restart, the size we INTEND is tracked and saved, never the size read back; a difference within
 * SIZE_TOLERANCE is rounding, not a user resize. DPI_SLACK keeps that rounding from pushing the
 * window past the right or bottom edge of the work area.
 */
export const SIZE_TOLERANCE = 12;
export const POSITION_TOLERANCE = 2;
export const DPI_SLACK = 8;
export const RECONCILE_MS = 500;
/** Windows reports a minimised window at about (-32000, -32000); that is never a real position. */
const SENTINEL = -30_000;
const MAX_COORD = 100_000;
const MAX_SIZE = 20_000;
const MIN_SIZE = 200;

const isInt = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value);

function parseRect(value: unknown): Rect | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as Record<string, unknown>;
  if (!isInt(v.x) || !isInt(v.y) || !isInt(v.width) || !isInt(v.height)) return null;
  if (Math.abs(v.x) > MAX_COORD || Math.abs(v.y) > MAX_COORD) return null;
  if (v.x <= SENTINEL || v.y <= SENTINEL) return null;
  if (v.width < MIN_SIZE || v.height < MIN_SIZE || v.width > MAX_SIZE || v.height > MAX_SIZE) return null;
  return { x: v.x, y: v.y, width: v.width, height: v.height };
}

/** Validate a stored placement. Anything unusable returns null (the default placement is used). */
export function parseSavedPlacement(value: unknown): SavedPlacement | null {
  const rect = parseRect(value);
  if (!rect) return null;
  const display = (value as { display?: unknown }).display;
  const d = parseRect(display);
  const id = (display as { id?: unknown } | undefined)?.id;
  if (!d || !isInt(id) || id < 0) return null;
  return { ...rect, display: { ...d, id } };
}

export function intersectionArea(a: Rect, b: Rect): number {
  const width = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const height = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  return width > 0 && height > 0 ? width * height : 0;
}

/** The share of `bounds` that lies on any display's work area (work areas do not overlap). */
export function visibleFraction(bounds: Rect, displays: readonly DisplayInfo[]): number {
  const area = bounds.width * bounds.height;
  if (area <= 0) return 0;
  const visible = displays.reduce((sum, d) => sum + intersectionArea(bounds, d.workArea), 0);
  return Math.min(1, visible / area);
}

/** Fit `bounds` fully inside `area`: shrink only if it does not fit, then slide it inside. */
export function fitInside(bounds: Rect, area: Rect, min = MIN_WINDOW, slack = 0): Rect {
  const width = Math.min(area.width, Math.max(min.width, bounds.width));
  const height = Math.min(area.height, Math.max(min.height, bounds.height));
  const right = Math.max(area.x, area.x + area.width - width - slack);
  const bottom = Math.max(area.y, area.y + area.height - height - slack);
  const x = Math.min(Math.max(bounds.x, area.x), right);
  const y = Math.min(Math.max(bounds.y, area.y), bottom);
  return { x, y, width, height };
}

const center = (r: Rect) => ({ x: r.x + r.width / 2, y: r.y + r.height / 2 });

/** The display that holds most of `bounds`; if none does, the one whose centre is nearest. */
export function displayForBounds(bounds: Rect, displays: readonly DisplayInfo[]): DisplayInfo {
  let best: DisplayInfo | undefined;
  let bestArea = 0;
  for (const display of displays) {
    const area = intersectionArea(bounds, display.workArea);
    if (area > bestArea) {
      best = display;
      bestArea = area;
    }
  }
  if (best) return best;
  const c = center(bounds);
  let nearest = displays[0] as DisplayInfo;
  let nearestDistance = Number.POSITIVE_INFINITY;
  for (const display of displays) {
    const dc = center(display.workArea);
    const distance = (dc.x - c.x) ** 2 + (dc.y - c.y) ** 2;
    if (distance < nearestDistance) {
      nearest = display;
      nearestDistance = distance;
    }
  }
  return nearest;
}

/** The display a saved placement was on, if it is connected: by id, else by identical bounds. */
export function savedDisplayIfPresent(saved: SavedPlacement, displays: readonly DisplayInfo[]): DisplayInfo | null {
  const byId = displays.find((d) => d.id === saved.display.id);
  if (byId) return byId;
  const { x, y, width, height } = saved.display;
  return displays.find((d) => d.bounds.x === x && d.bounds.y === y && d.bounds.width === width && d.bounds.height === height) ?? null;
}

export type Resolved = { bounds: Rect; relocated: boolean; display: DisplayInfo };

/**
 * Where the window should be, given a saved preference (or none). `relocated` is true when the saved
 * position was not usable and the window had to be moved somewhere else.
 */
export function resolvePlacement(
  saved: SavedPlacement | null,
  displays: readonly DisplayInfo[],
  primaryId: number,
  fallback: { width: number; height: number },
): Resolved {
  const primary = displays.find((d) => d.id === primaryId) ?? (displays[0] as DisplayInfo);
  if (!saved) {
    const wa = primary.workArea;
    const size = fitInside({ x: 0, y: 0, ...fallback }, wa);
    const bounds = { ...size, x: wa.x + wa.width - size.width - DEFAULT_INSET, y: wa.y + DEFAULT_INSET };
    return { bounds: fitInside(bounds, wa), relocated: false, display: primary };
  }
  const rect: Rect = { x: saved.x, y: saved.y, width: saved.width, height: saved.height };
  if (visibleFraction(rect, displays) >= MIN_VISIBLE_FRACTION) {
    return { bounds: rect, relocated: false, display: displayForBounds(rect, displays) };
  }
  const home = savedDisplayIfPresent(saved, displays) ?? displayForBounds(rect, displays);
  return { bounds: fitInside(rect, home.workArea, MIN_WINDOW, DPI_SLACK), relocated: true, display: home };
}

/** Same place, and a size that differs only by rounding. */
const near = (a: Rect, b: Rect) =>
  Math.abs(a.x - b.x) <= POSITION_TOLERANCE &&
  Math.abs(a.y - b.y) <= POSITION_TOLERANCE &&
  Math.abs(a.width - b.width) <= SIZE_TOLERANCE &&
  Math.abs(a.height - b.height) <= SIZE_TOLERANCE;

// ------------------------------------------------------------------ manager

export type PlacementWindow = {
  getBounds(): Rect;
  setBounds(bounds: Rect): void;
  isMinimized(): boolean;
  isVisible(): boolean;
  isDestroyed(): boolean;
};

export type PlacementPorts = {
  displays(): DisplayInfo[];
  primaryId(): number;
  window: PlacementWindow;
  load(): Partial<Record<WindowMode, SavedPlacement>>;
  save(mode: WindowMode, placement: SavedPlacement): void;
  defaultSize(mode: WindowMode): { width: number; height: number };
  /** Run `fn` after `ms`; returns a function that cancels it. */
  schedule(ms: number, fn: () => void): () => void;
};

export class WindowPlacementManager {
  private mode: WindowMode = "compact";
  private preferred: Partial<Record<WindowMode, SavedPlacement>> = {};
  private displaced = false;
  private lastApplied: Rect | null = null;
  /** The size we mean the window to be (see SIZE_TOLERANCE). */
  private nominal: { width: number; height: number } | null = null;
  private cancelSettle: (() => void) | null = null;
  private cancelReconcile: (() => void) | null = null;
  private disposed = false;

  constructor(private readonly ports: PlacementPorts) {}

  /** The bounds to create the window with. Called once, before the window exists. */
  initialBounds(mode: WindowMode): Rect {
    this.mode = mode;
    this.preferred = this.ports.load();
    const displays = this.ports.displays();
    if (displays.length === 0) {
      const size = this.ports.defaultSize(mode);
      return { x: 0, y: 0, ...size };
    }
    const resolved = resolvePlacement(this.preferred[mode] ?? null, displays, this.ports.primaryId(), this.ports.defaultSize(mode));
    this.displaced = resolved.relocated;
    this.lastApplied = resolved.bounds;
    this.nominal = { width: resolved.bounds.width, height: resolved.bounds.height };
    return resolved.bounds;
  }

  /** The window moved or was resized (any cause). Settled after a quiet period, so a drag is one change. */
  onBoundsEvent(): void {
    if (this.disposed) return;
    this.cancelSettle?.();
    this.cancelSettle = this.ports.schedule(SETTLE_MS, () => this.settle());
  }

  /** Displays changed or the window was shown or restored. Docking fires bursts, so wait for quiet. */
  scheduleReconcile(): void {
    if (this.disposed) return;
    this.cancelReconcile?.();
    this.cancelReconcile = this.ports.schedule(RECONCILE_MS, () => this.reconcileNow());
  }

  /** Record the user's own placement. Ignores our own moves, and a minimised or hidden window. */
  settle(): void {
    this.cancelSettle?.();
    this.cancelSettle = null;
    const window = this.ports.window;
    if (this.disposed || window.isDestroyed() || window.isMinimized() || !window.isVisible()) return;
    const displays = this.ports.displays();
    if (displays.length === 0) return;
    const bounds = window.getBounds();
    if (this.lastApplied && near(bounds, this.lastApplied)) return; // our own move echoing back
    if (!parseRect(bounds)) return; // never store nonsense
    this.remember(bounds, displays);
    this.displaced = false; // the user chose this place
  }

  /** Bring the window back to a valid place after a display change, only if it needs it. */
  reconcileNow(): void {
    this.cancelReconcile?.();
    this.cancelReconcile = null;
    const window = this.ports.window;
    if (this.disposed || window.isDestroyed() || window.isMinimized()) return;
    const displays = this.ports.displays();
    if (displays.length === 0) return;
    const current = window.getBounds();
    const preference = this.preferred[this.mode] ?? null;
    if (this.displaced && preference) {
      const resolved = resolvePlacement(preference, displays, this.ports.primaryId(), this.ports.defaultSize(this.mode));
      if (!resolved.relocated) {
        if (!near(current, resolved.bounds)) this.apply(resolved.bounds);
        this.displaced = false; // back where the user put it
        return;
      }
    }
    if (visibleFraction(current, displays) >= MIN_VISIBLE_FRACTION) return; // never jump a usable window
    const home = (preference ? savedDisplayIfPresent(preference, displays) : null) ?? displayForBounds(current, displays);
    this.apply(fitInside({ x: current.x, y: current.y, ...this.intended(current) }, home.workArea, MIN_WINDOW, DPI_SLACK));
    this.displaced = true; // keep the preference, so a returning monitor gets the window back
  }

  /** Switch between compact and expanded. Each mode remembers its own size and place. */
  setMode(expanded: boolean): void {
    const next: WindowMode = expanded ? "expanded" : "compact";
    if (this.disposed) return;
    this.settle(); // keep a move the user just made in the mode we are leaving
    const window = this.ports.window;
    if (window.isDestroyed()) {
      this.mode = next;
      return;
    }
    this.mode = next;
    const displays = this.ports.displays();
    if (displays.length === 0) return;
    const current = window.getBounds();
    const size = this.ports.defaultSize(next);
    const saved = this.preferred[next] ?? null;
    let target: Rect;
    if (saved && !window.isMinimized() && visibleFraction(saved, displays) >= MIN_VISIBLE_FRACTION) {
      target = { x: saved.x, y: saved.y, width: saved.width, height: saved.height };
    } else {
      // Keep the top-left corner where it is, so the window grows or shrinks in place.
      target = { x: current.x, y: current.y, ...size };
    }
    const home = displayForBounds(target, displays);
    const fitted = fitInside(target, home.workArea, MIN_WINDOW, DPI_SLACK);
    this.apply(fitted);
    this.remember(fitted, displays);
    this.displaced = false;
  }

  /** The user chose a display: put the window in the middle of its work area and remember it. */
  moveToDisplay(id: number): void {
    const displays = this.ports.displays();
    const display = displays.find((d) => d.id === id);
    if (!display) throw new Error("That display is no longer available.");
    const window = this.ports.window;
    if (window.isDestroyed()) return;
    const current = window.getBounds();
    const intended = this.intended(current);
    const size = fitInside({ x: 0, y: 0, width: intended.width, height: intended.height }, display.workArea);
    const wa = display.workArea;
    const target = fitInside(
      { x: Math.round(wa.x + (wa.width - size.width) / 2), y: Math.round(wa.y + (wa.height - size.height) / 2), width: size.width, height: size.height },
      wa,
      MIN_WINDOW,
      DPI_SLACK,
    );
    this.apply(target);
    this.remember(target, displays);
    this.displaced = false;
  }

  listDisplays(): DisplayChoice[] {
    const displays = [...this.ports.displays()].sort((a, b) => a.bounds.x - b.bounds.x || a.bounds.y - b.bounds.y);
    if (displays.length === 0) return [];
    const window = this.ports.window;
    const holding = window.isDestroyed() ? null : displayForBounds(window.getBounds(), displays);
    const primaryId = this.ports.primaryId();
    return displays.map((d, index) => {
      const scalePercent = Math.round(d.scaleFactor * 100);
      return {
        id: d.id,
        label: `Display ${index + 1}${d.id === primaryId ? " (primary)" : ""} · ${d.bounds.width}×${d.bounds.height} · ${scalePercent}% · at ${d.bounds.x}, ${d.bounds.y}`,
        primary: d.id === primaryId,
        current: holding?.id === d.id,
        width: d.bounds.width,
        height: d.bounds.height,
        scalePercent,
      };
    });
  }

  /** Save anything still pending and stop. Safe to call more than once (quit). */
  dispose(): void {
    if (this.disposed) return;
    this.settle();
    this.cancelReconcile?.();
    this.cancelReconcile = null;
    this.disposed = true;
  }

  // ------------------------------------------------------------------ internals

  /** The size to keep: the intended one if the window only differs by rounding, else the user's new size. */
  private intended(actual: Rect): { width: number; height: number } {
    const n = this.nominal;
    if (n && Math.abs(actual.width - n.width) <= SIZE_TOLERANCE && Math.abs(actual.height - n.height) <= SIZE_TOLERANCE) return n;
    return { width: actual.width, height: actual.height };
  }

  private remember(bounds: Rect, displays: readonly DisplayInfo[]): void {
    const display = displayForBounds(bounds, displays);
    const size = this.intended(bounds);
    this.nominal = size;
    const placement: SavedPlacement = {
      x: bounds.x,
      y: bounds.y,
      width: size.width,
      height: size.height,
      display: { id: display.id, ...display.bounds },
    };
    if (!parseSavedPlacement(placement)) return;
    this.preferred[this.mode] = placement;
    this.lastApplied = bounds;
    try {
      this.ports.save(this.mode, placement);
    } catch {
      // A settings file that cannot be written must never break the window.
    }
  }

  /**
   * Set the bounds. Windows can apply the wrong size when a window crosses displays with different
   * scaling, so if the size that results differs, set it once more (a known Electron workaround).
   */
  private apply(target: Rect): void {
    const window = this.ports.window;
    try {
      window.setBounds(target);
      const actual = window.getBounds();
      // Only a large error (the mixed-DPI case) is worth a second try; rounding is left alone.
      if (Math.abs(actual.width - target.width) > SIZE_TOLERANCE || Math.abs(actual.height - target.height) > SIZE_TOLERANCE) {
        window.setBounds(target);
      }
      this.nominal = { width: target.width, height: target.height };
      this.lastApplied = window.getBounds();
    } catch {
      this.lastApplied = null;
    }
  }
}
