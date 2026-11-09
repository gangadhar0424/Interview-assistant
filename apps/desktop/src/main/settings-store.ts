import { randomUUID } from "node:crypto";
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { DesktopSettings } from "../shared/contracts";
import { normalizeBackendUrl } from "./api-client";

export const DEFAULT_SETTINGS: DesktopSettings = {
  backendBaseUrl: process.env.INTERVIEW_API_URL ?? "http://127.0.0.1:8000",
  alwaysOnTop: true,
  opacity: 0.96,
  expanded: false,
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
    typeof value.backendBaseUrl !== "string"
  ) {
    throw new Error("Desktop settings file is invalid.");
  }
  return {
    backendBaseUrl: normalizeBackendUrl(value.backendBaseUrl),
    alwaysOnTop: value.alwaysOnTop,
    opacity: value.opacity,
    expanded: value.expanded,
  };
}

export class SettingsStore {
  constructor(
    private readonly filePath: string,
    private settings: DesktopSettings = { ...DEFAULT_SETTINGS },
  ) {}

  load(): DesktopSettings {
    try {
      this.settings = validateSettings(JSON.parse(readFileSync(this.filePath, "utf8")) as unknown);
    } catch (error) {
      if (isNodeError(error) && error.code === "ENOENT") return { ...this.settings };
      throw new Error("Could not read desktop settings.", { cause: error });
    }
    return { ...this.settings };
  }

  update(patch: Partial<DesktopSettings>): DesktopSettings {
    const next = validateSettings({ ...this.settings, ...patch });
    const tempPath = join(dirname(this.filePath), `.settings-${randomUUID()}.tmp`);
    try {
      writeFileSync(tempPath, JSON.stringify(next, null, 2), { encoding: "utf8", mode: 0o600 });
      renameSync(tempPath, this.filePath);
      this.settings = next;
      return { ...next };
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
