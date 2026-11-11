import type { DesktopSettings } from "../shared/contracts";

export const MIN_OPACITY = 0.65;
export const MAX_OPACITY = 1;
export const MAX_COPY_LENGTH = 100_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Validates an untrusted settings patch from the renderer. Messages are user-safe. */
export function validateSettingsPatch(value: unknown): Partial<DesktopSettings> {
  if (!isRecord(value)) throw new Error("Settings must be an object.");
  const patch: Partial<DesktopSettings> = {};
  if ("backendBaseUrl" in value) {
    if (typeof value.backendBaseUrl !== "string") throw new Error("Backend URL must be text.");
    patch.backendBaseUrl = value.backendBaseUrl;
  }
  if ("alwaysOnTop" in value) {
    if (typeof value.alwaysOnTop !== "boolean") throw new Error("Always-on-top must be on or off.");
    patch.alwaysOnTop = value.alwaysOnTop;
  }
  if ("opacity" in value) {
    if (typeof value.opacity !== "number" || !Number.isFinite(value.opacity)) {
      throw new Error("Opacity must be a number.");
    }
    if (value.opacity < MIN_OPACITY || value.opacity > MAX_OPACITY) {
      throw new Error("Opacity must be between 65% and 100%.");
    }
    patch.opacity = value.opacity;
  }
  if ("expanded" in value) {
    if (typeof value.expanded !== "boolean") throw new Error("Expanded must be on or off.");
    patch.expanded = value.expanded;
  }
  if ("screenshotInputEnabled" in value) {
    if (typeof value.screenshotInputEnabled !== "boolean") {
      throw new Error("Screenshot input must be on or off.");
    }
    patch.screenshotInputEnabled = value.screenshotInputEnabled;
  }
  if ("audioInputEnabled" in value) {
    if (typeof value.audioInputEnabled !== "boolean") {
      throw new Error("Audio input must be on or off.");
    }
    patch.audioInputEnabled = value.audioInputEnabled;
  }
  const known = [
    "backendBaseUrl",
    "alwaysOnTop",
    "opacity",
    "expanded",
    "screenshotInputEnabled",
    "audioInputEnabled",
  ];
  if (Object.keys(value).some((key) => !known.includes(key))) {
    throw new Error("Unsupported setting.");
  }
  return patch;
}

/** Validates text the renderer asks the main process to place on the clipboard. */
export function validateCopyText(value: unknown): string {
  if (typeof value !== "string") throw new Error("Only text can be copied.");
  if (value.length > MAX_COPY_LENGTH) throw new Error("The text is too large to copy.");
  return value;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

/** A display id is the operating system's numeric id as text (never a name or a path). */
export function isDisplayId(value: unknown): value is string {
  return typeof value === "string" && /^[0-9]{1,12}$/.test(value);
}

/** The only audio sources the app can start (never a device id or a free-form value). */
export function isAudioSource(value: unknown): value is "microphone" | "system" {
  return value === "microphone" || value === "system";
}
