import { randomUUID } from "node:crypto";
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import {
  ACCEPTABLE_USE_VERSION,
  type AcknowledgementState,
} from "../shared/acknowledgement";
import type { DesktopSettings } from "../shared/contracts";
import {
  DEFAULT_SHORTCUT_CONFIG,
  DEFAULT_SHORTCUTS,
  SHORTCUT_ACTIONS,
  normalizeAccelerator,
  validateShortcutConfig,
  type ShortcutConfig,
} from "../shared/shortcuts";
import { normalizeBackendUrl } from "./api-client";
import { parseSavedPlacement, type SavedPlacement } from "./window-placement";
import type { WindowMode } from "../shared/placement";

export const DEFAULT_SETTINGS: DesktopSettings = {
  backendBaseUrl: process.env.INTERVIEW_API_URL ?? "http://127.0.0.1:8000",
  alwaysOnTop: true,
  opacity: 0.96,
  expanded: false,
  screenshotInputEnabled: false,
  audioInputEnabled: false,
};

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validateSettings(value: unknown): DesktopSettings {
  if (!isObject(value)) throw new Error("Desktop settings file is invalid.");
  if (
    typeof value.alwaysOnTop !== "boolean" ||
    typeof value.expanded !== "boolean" ||
    typeof value.opacity !== "number" ||
    !Number.isFinite(value.opacity) ||
    value.opacity < 0.65 ||
    value.opacity > 1 ||
    typeof value.backendBaseUrl !== "string" ||
    // Files written before Phase 11B have no such field: that means off.
    (value.screenshotInputEnabled !== undefined && typeof value.screenshotInputEnabled !== "boolean") ||
    (value.audioInputEnabled !== undefined && typeof value.audioInputEnabled !== "boolean")
  ) {
    throw new Error("Desktop settings file is invalid.");
  }
  return {
    backendBaseUrl: normalizeBackendUrl(value.backendBaseUrl),
    alwaysOnTop: value.alwaysOnTop,
    opacity: value.opacity,
    expanded: value.expanded,
    screenshotInputEnabled: value.screenshotInputEnabled === true,
    audioInputEnabled: value.audioInputEnabled === true,
  };
}

/** What is stored for the acceptable-use acknowledgement: who accepted which wording, and when. */
type StoredAcknowledgement = { version: number; userId: string; acceptedAt: string };

const USER_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** A damaged or foreign entry is treated as "not accepted", never as an error that blocks startup. */
function parseAcknowledgement(value: unknown): StoredAcknowledgement | null {
  if (!isObject(value)) return null;
  const { version, userId, acceptedAt } = value;
  if (
    typeof version !== "number" ||
    !Number.isInteger(version) ||
    typeof userId !== "string" ||
    !USER_ID.test(userId) ||
    typeof acceptedAt !== "string" ||
    Number.isNaN(Date.parse(acceptedAt))
  ) {
    return null;
  }
  return { version, userId, acceptedAt };
}

/**
 * Stored shortcuts. Lenient on purpose: a missing section means the defaults, and one unusable
 * binding falls back to that action's default instead of failing the whole settings file.
 */
function parseShortcuts(value: unknown): ShortcutConfig {
  if (!isObject(value)) return structuredClone(DEFAULT_SHORTCUT_CONFIG);
  const bindings = { ...DEFAULT_SHORTCUTS };
  if (isObject(value.bindings)) {
    for (const action of SHORTCUT_ACTIONS) {
      const normalized = normalizeAccelerator(value.bindings[action]);
      if (normalized) bindings[action] = normalized;
    }
  }
  return { enabled: value.enabled !== false, bindings };
}

function isDefaultShortcuts(config: ShortcutConfig): boolean {
  return (
    config.enabled === DEFAULT_SHORTCUT_CONFIG.enabled &&
    SHORTCUT_ACTIONS.every((action) => config.bindings[action] === DEFAULT_SHORTCUTS[action])
  );
}

type StoredPlacements = Partial<Record<WindowMode, SavedPlacement>>;

/** Lenient: an unusable entry is dropped and the default placement is used for that mode. */
function parsePlacements(value: unknown): StoredPlacements {
  if (!isObject(value)) return {};
  const out: StoredPlacements = {};
  for (const mode of ["compact", "expanded"] as const) {
    const parsed = parseSavedPlacement(value[mode]);
    if (parsed) out[mode] = parsed;
  }
  return out;
}

export class SettingsStore {
  private ack: StoredAcknowledgement | null = null;
  private placements: StoredPlacements = {};
  private shortcuts: ShortcutConfig = structuredClone(DEFAULT_SHORTCUT_CONFIG);

  constructor(
    private readonly filePath: string,
    private settings: DesktopSettings = { ...DEFAULT_SETTINGS },
  ) {}

  load(): DesktopSettings {
    try {
      const parsed = JSON.parse(readFileSync(this.filePath, "utf8")) as unknown;
      this.settings = validateSettings(parsed);
      this.ack = isObject(parsed) ? parseAcknowledgement(parsed.acceptableUse) : null;
      this.shortcuts = parseShortcuts(isObject(parsed) ? parsed.shortcuts : undefined);
      this.placements = parsePlacements(isObject(parsed) ? parsed.windowPlacement : undefined);
    } catch (error) {
      if (isNodeError(error) && error.code === "ENOENT") return { ...this.settings };
      throw new Error("Could not read desktop settings.", { cause: error });
    }
    return { ...this.settings };
  }

  update(patch: Partial<DesktopSettings>): DesktopSettings {
    const next = validateSettings({ ...this.settings, ...patch });
    this.write(next, this.ack, this.shortcuts, this.placements);
    this.settings = next;
    return { ...next };
  }

  /**
   * Whether `userId` has accepted the CURRENT acceptable-use wording on this device. The renderer can
   * never set this: it is written only by `acknowledge`, which the main process calls after its own
   * native confirmation. Local only (the settings file in the user's profile); never sent anywhere.
   */
  acknowledgement(userId: string | null): AcknowledgementState {
    const ok =
      userId !== null &&
      this.ack !== null &&
      this.ack.userId === userId &&
      this.ack.version === ACCEPTABLE_USE_VERSION;
    return {
      accepted: ok,
      version: ACCEPTABLE_USE_VERSION,
      acceptedAt: ok && this.ack ? this.ack.acceptedAt : null,
    };
  }

  isAcknowledged(userId: string | null): boolean {
    return this.acknowledgement(userId).accepted;
  }

  /** Record the acceptance of the current wording by `userId`. One slot per device: a different
   * account must accept again. */
  acknowledge(userId: string, acceptedAt: string): AcknowledgementState {
    const next: StoredAcknowledgement = { version: ACCEPTABLE_USE_VERSION, userId, acceptedAt };
    if (!parseAcknowledgement(next)) throw new Error("Could not save the acknowledgement.");
    this.write(this.settings, next, this.shortcuts, this.placements);
    this.ack = next;
    return this.acknowledgement(userId);
  }

  /** The saved keyboard-shortcut preferences (defaults until the user changes them). */
  shortcutConfig(): ShortcutConfig {
    return structuredClone(this.shortcuts);
  }

  /** Validate (throws a user-safe Error) and save the shortcut preferences. */
  setShortcuts(value: unknown): ShortcutConfig {
    const next = validateShortcutConfig(value);
    this.write(this.settings, this.ack, next, this.placements);
    this.shortcuts = next;
    return structuredClone(next);
  }

  /** The saved window position and size for each mode (M-6). Empty until the window was placed. */
  windowPlacements(): StoredPlacements {
    return structuredClone(this.placements);
  }

  /** Save where the window is for one mode. Validated; an unusable placement throws and changes nothing. */
  setWindowPlacement(mode: WindowMode, placement: unknown): void {
    const parsed = parseSavedPlacement(placement);
    if (!parsed || (mode !== "compact" && mode !== "expanded")) throw new Error("Could not save the window position.");
    const next = { ...this.placements, [mode]: parsed };
    this.write(this.settings, this.ack, this.shortcuts, next);
    this.placements = next;
  }

  private write(
    settings: DesktopSettings,
    ack: StoredAcknowledgement | null,
    shortcuts: ShortcutConfig,
    placements: StoredPlacements,
  ): void {
    const tempPath = join(dirname(this.filePath), `.settings-${randomUUID()}.tmp`);
    try {
      // Optional sections are written only when they carry something, so the file stays exactly the
      // settings for anyone who never accepted the notice or changed a shortcut.
      const body = {
        ...settings,
        ...(ack ? { acceptableUse: ack } : {}),
        ...(isDefaultShortcuts(shortcuts) ? {} : { shortcuts }),
        ...(Object.keys(placements).length > 0 ? { windowPlacement: placements } : {}),
      };
      writeFileSync(tempPath, JSON.stringify(body, null, 2), { encoding: "utf8", mode: 0o600 });
      renameSync(tempPath, this.filePath);
    } catch (error) {
      throw new Error("Could not save desktop settings.", { cause: error });
    }
  }

  current(): DesktopSettings {
    return { ...this.settings };
  }
}

function isNodeError(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error;
}
