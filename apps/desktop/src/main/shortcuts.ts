/**
 * Global keyboard shortcuts (M-4, D-69). Pure logic: the operating-system registrar and the window
 * come in through ports, so everything here is unit-tested without Electron. Mocked tests cannot
 * establish how Windows really behaves; that needs the manual checks in docs/DESKTOP_SHORTCUTS.md.
 *
 * What a shortcut can do: show, hide, or focus the assistant window, and ask the renderer to move
 * to the audio or screenshot panel. What it can NEVER do: start listening or capture. This module
 * has no access to the audio or capture services (the ports below have no such method, and a guard
 * test checks the source). Starting either still needs the visible panel, the user's own press, the
 * acceptable-use acknowledgement, the existing consent, and for screenshots the countdown.
 *
 * Nothing here logs.
 */
import {
  SHORTCUT_ACTIONS,
  normalizeAccelerator,
  type ShortcutAction,
  type ShortcutConfig,
  type ShortcutPanel,
  type ShortcutStatus,
} from "../shared/shortcuts";

/** The operating-system shortcut registrar (Electron's global shortcut API, wired in index.ts). */
export type ShortcutRegistrar = {
  /** True if registered; false if another application already owns the combination. May throw. */
  register(accelerator: string, callback: () => void): boolean;
  unregister(accelerator: string): void;
};

/** Everything a shortcut may do to the window. Deliberately has no capture or audio methods. */
export type WindowActions = {
  isVisible(): boolean;
  isFocused(): boolean;
  isMinimized(): boolean;
  restore(): void;
  show(): void;
  focus(): void;
  /** Hide to the tray, exactly like the close button (listening and held screenshots then stop). */
  hide(): void;
  /** Ask the renderer to bring a panel into view. It never starts anything. */
  showPanel(panel: ShortcutPanel): void;
};

export type ShortcutManagerPorts = {
  registrar: ShortcutRegistrar;
  window: WindowActions;
  now(): number;
};

/** Ignore a repeat of the same shortcut within this time (held keys, double presses). */
export const SHORTCUT_COOLDOWN_MS = 300;

export type ShortcutStatusMap = Record<ShortcutAction, ShortcutStatus>;

const MESSAGES = {
  active: "Active.",
  disabled: "Shortcuts are turned off.",
  invalid: "Not a valid shortcut. Use Shift plus Ctrl or Alt plus one letter, number or function key.",
  duplicate: "Another action already uses this shortcut.",
  unavailable: "Another application is already using this shortcut. Choose a different one.",
} as const;

function allWith(kind: keyof typeof MESSAGES): ShortcutStatusMap {
  const out = {} as ShortcutStatusMap;
  for (const action of SHORTCUT_ACTIONS) out[action] = { kind, message: MESSAGES[kind] };
  return out;
}

export class ShortcutManager {
  /** Accelerators THIS manager registered, and which action each one runs. */
  private readonly owned = new Map<string, ShortcutAction>();
  private readonly lastRun = new Map<ShortcutAction, number>();
  private current: ShortcutStatusMap = allWith("disabled");
  private disposed = false;

  constructor(private readonly ports: ShortcutManagerPorts) {}

  status(): ShortcutStatusMap {
    return { ...this.current };
  }

  /**
   * Replace every registration with `config`. First removes only what this manager registered
   * (never other registrations), then registers each valid, unique shortcut. A failure of one
   * shortcut never affects the others and never throws.
   */
  apply(config: ShortcutConfig): ShortcutStatusMap {
    this.releaseAll();
    if (this.disposed) return (this.current = allWith("disabled"));
    if (!config.enabled) return (this.current = allWith("disabled"));
    const next = {} as ShortcutStatusMap;
    const seen = new Set<string>();
    for (const action of SHORTCUT_ACTIONS) {
      const accelerator = normalizeAccelerator(config.bindings[action]);
      if (!accelerator) {
        next[action] = { kind: "invalid", message: MESSAGES.invalid };
        continue;
      }
      if (seen.has(accelerator)) {
        next[action] = { kind: "duplicate", message: MESSAGES.duplicate };
        continue;
      }
      seen.add(accelerator);
      let registered = false;
      try {
        registered = this.ports.registrar.register(accelerator, () => this.run(action));
      } catch {
        registered = false;
      }
      if (registered) {
        this.owned.set(accelerator, action);
        next[action] = { kind: "active", message: MESSAGES.active };
      } else {
        next[action] = { kind: "unavailable", message: MESSAGES.unavailable };
      }
    }
    return (this.current = next);
  }

  /** Remove everything this manager registered. Safe to call repeatedly; used at shutdown. */
  dispose(): void {
    this.disposed = true;
    this.releaseAll();
    this.current = allWith("disabled");
  }

  private releaseAll(): void {
    for (const accelerator of this.owned.keys()) {
      try {
        this.ports.registrar.unregister(accelerator);
      } catch {
        // Nothing useful to do; the application is reconfiguring or quitting.
      }
    }
    this.owned.clear();
  }

  /** Run one action. Never throws into the operating system's shortcut callback. */
  private run(action: ShortcutAction): void {
    if (this.disposed) return;
    const now = this.ports.now();
    const last = this.lastRun.get(action);
    if (last !== undefined && now - last < SHORTCUT_COOLDOWN_MS) return;
    this.lastRun.set(action, now);
    try {
      this.perform(action);
    } catch {
      // A window that is closing or destroyed must not crash the main process.
    }
  }

  private perform(action: ShortcutAction): void {
    const window = this.ports.window;
    switch (action) {
      case "toggleWindow":
        if (window.isVisible() && window.isFocused() && !window.isMinimized()) window.hide();
        else this.bringToFront();
        return;
      case "hideWindow":
        if (window.isVisible()) window.hide();
        return;
      case "focusAudio":
        this.bringToFront();
        window.showPanel("audio");
        return;
      case "focusScreenshot":
        this.bringToFront();
        window.showPanel("screenshot");
        return;
    }
  }

  private bringToFront(): void {
    const window = this.ports.window;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  }
}

