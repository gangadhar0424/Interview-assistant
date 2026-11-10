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
  if (Object.keys(value).some((key) => !["backendBaseUrl", "alwaysOnTop", "opacity", "expanded"].includes(key))) {
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
