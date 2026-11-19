/**
 * Keyboard shortcuts (M-4, D-69). Shared by the main process (which validates and registers them)
 * and the renderer (which lets the user change them).
 *
 * None of these actions starts audio or screenshot capture. They only show, hide or focus the
 * assistant window and bring a panel into view; starting anything still needs the visible panel,
 * the user's own press, the acceptable-use acknowledgement, the existing consent and (for
 * screenshots) the countdown.
 */
export const SHORTCUT_ACTIONS = ["toggleWindow", "hideWindow", "focusAudio", "focusScreenshot"] as const;
export type ShortcutAction = (typeof SHORTCUT_ACTIONS)[number];

export const SHORTCUT_LABELS: Record<ShortcutAction, { label: string; description: string }> = {
  toggleWindow: {
    label: "Show or hide the assistant",
    description: "Shows and focuses the window, or hides it to the tray when it is already in front.",
  },
  hideWindow: {
    label: "Hide the assistant",
    description: "Hides the window to the tray. Listening and screenshots stop when it is hidden.",
  },
  focusAudio: {
    label: "Go to audio input",
    description: "Shows the window and moves to the audio panel. It does not start listening.",
  },
  focusScreenshot: {
    label: "Go to screenshot input",
    description: "Shows the window and moves to the screenshot panel. It does not capture anything.",
  },
};

/**
 * Defaults: Alt+Shift+letter. Every shortcut must be Shift plus exactly one of Ctrl or Alt plus one
 * key (see `normalizeAccelerator`). Ctrl+Alt is refused because Windows treats it as AltGr on many
 * keyboard layouts, so it would swallow ordinary typing.
 */
export const DEFAULT_SHORTCUTS: Record<ShortcutAction, string> = {
  toggleWindow: "Alt+Shift+A",
  hideWindow: "Alt+Shift+H",
  focusAudio: "Alt+Shift+L",
  focusScreenshot: "Alt+Shift+S",
};

export type ShortcutConfig = {
  enabled: boolean;
  bindings: Record<ShortcutAction, string>;
};

export const DEFAULT_SHORTCUT_CONFIG: ShortcutConfig = {
  enabled: true,
  bindings: { ...DEFAULT_SHORTCUTS },
};

export type ShortcutStatusKind = "active" | "disabled" | "invalid" | "duplicate" | "unavailable";
export type ShortcutStatus = { kind: ShortcutStatusKind; message: string };

export type ShortcutState = {
  config: ShortcutConfig;
  defaults: Record<ShortcutAction, string>;
  status: Record<ShortcutAction, ShortcutStatus>;
};

export type ShortcutPanel = "audio" | "screenshot";
/** Sent from the main process to the renderer: move to this panel. Never a capture request. */
export type ShortcutEvent = { panel: ShortcutPanel };

const NAMED_KEYS: Record<string, string> = {
  space: "Space",
  up: "Up",
  down: "Down",
  left: "Left",
  right: "Right",
  home: "Home",
  end: "End",
  pageup: "PageUp",
  pagedown: "PageDown",
};

function normalizeKey(raw: string): string | null {
  if (/^[a-z]$/i.test(raw)) return raw.toUpperCase();
  if (/^[0-9]$/.test(raw)) return raw;
  if (/^f([1-9]|1[0-2])$/i.test(raw)) return raw.toUpperCase();
  return NAMED_KEYS[raw.toLowerCase()] ?? null;
}

/**
 * Parse and canonicalise one accelerator, or return null if it is not acceptable. Accepted:
 * Shift + exactly one of Ctrl/Alt + exactly one key (A-Z, 0-9, F1-F12, Space, arrows, Home, End,
 * PageUp, PageDown). `Control`, `Ctrl`, `CommandOrControl` and `CmdOrCtrl` all mean Ctrl. Refused:
 * the Windows/Command key (reserved by the system), Ctrl+Alt together (AltGr), a missing Shift, a
 * missing key, unknown keys, repeated modifiers, and anything with stray characters. The result is
 * always `Ctrl|Alt+Shift+Key`.
 */
export function normalizeAccelerator(value: unknown): string | null {
  if (typeof value !== "string" || value.length === 0 || value.length > 40) return null;
  if (!/^[A-Za-z0-9+]+$/.test(value)) return null;
  const parts = value.split("+");
  if (parts.some((part) => part.length === 0)) return null;
  const key = normalizeKey(parts[parts.length - 1] as string);
  if (!key) return null;
  let ctrl = false;
  let alt = false;
  let shift = false;
  for (const raw of parts.slice(0, -1)) {
    const modifier = raw.toLowerCase();
    if (modifier === "ctrl" || modifier === "control" || modifier === "commandorcontrol" || modifier === "cmdorctrl") {
      if (ctrl) return null;
      ctrl = true;
    } else if (modifier === "alt") {
      if (alt) return null;
      alt = true;
    } else if (modifier === "shift") {
      if (shift) return null;
      shift = true;
    } else {
      return null; // Super, Meta, Command, Win, anything unknown
    }
  }
  if (!shift || ctrl === alt) return null; // Shift and exactly one of Ctrl/Alt
  return `${ctrl ? "Ctrl" : "Alt"}+Shift+${key}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Validate a configuration sent by the renderer. Throws a user-safe Error. Unknown keys, a missing
 * action, an unacceptable shortcut or the same shortcut used twice are all refused, so nothing
 * unvalidated is ever registered or stored.
 */
export function validateShortcutConfig(value: unknown): ShortcutConfig {
  if (!isRecord(value)) throw new Error("Shortcut settings must be an object.");
  if (Object.keys(value).some((key) => key !== "enabled" && key !== "bindings")) {
    throw new Error("Unsupported shortcut setting.");
  }
  if (typeof value.enabled !== "boolean") throw new Error("Shortcuts must be on or off.");
  if (!isRecord(value.bindings)) throw new Error("Shortcut settings are incomplete.");
  const given = value.bindings;
  if (Object.keys(given).some((key) => !(SHORTCUT_ACTIONS as readonly string[]).includes(key))) {
    throw new Error("Unsupported shortcut action.");
  }
  const bindings = {} as Record<ShortcutAction, string>;
  const seen = new Map<string, ShortcutAction>();
  for (const action of SHORTCUT_ACTIONS) {
    const normalized = normalizeAccelerator(given[action]);
    if (!normalized) {
      throw new Error(
        `"${SHORTCUT_LABELS[action].label}" needs Shift plus Ctrl or Alt plus one letter, number or function key.`,
      );
    }
    const clash = seen.get(normalized);
    if (clash) {
      throw new Error(
        `"${SHORTCUT_LABELS[action].label}" and "${SHORTCUT_LABELS[clash].label}" use the same shortcut.`,
      );
    }
    seen.set(normalized, action);
    bindings[action] = normalized;
  }
  return { enabled: value.enabled, bindings };
}

/** Build an accelerator from a key event (renderer helper). Null for modifier-only or other keys. */
export function acceleratorFromKeyEvent(event: {
  key: string;
  code: string;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}): string | null {
  let key: string | null = null;
  if (/^Key[A-Z]$/.test(event.code)) key = event.code.slice(3);
  else if (/^Digit[0-9]$/.test(event.code)) key = event.code.slice(5);
  else if (/^F([1-9]|1[0-2])$/.test(event.code)) key = event.code;
  else if (event.code === "Space") key = "Space";
  else if (/^Arrow(Up|Down|Left|Right)$/.test(event.code)) key = event.code.slice(5);
  else if (["Home", "End", "PageUp", "PageDown"].includes(event.code)) key = event.code;
  if (!key) return null;
  const parts = [event.ctrlKey ? "Ctrl" : "", event.altKey ? "Alt" : "", event.shiftKey ? "Shift" : ""].filter(Boolean);
  return [...parts, key].join("+");
}
