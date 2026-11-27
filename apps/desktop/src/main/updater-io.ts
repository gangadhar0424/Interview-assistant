import { createHash } from "node:crypto";
import { createReadStream, readFileSync, statfsSync } from "node:fs";
import { join } from "node:path";
import { MANIFEST_KEY_FILE, MAX_MANIFEST_BYTES } from "../shared/update-manifest.mjs";
import { UpdateFetchError } from "./updater";

/**
 * The operating-system side of the updater (M-11): reads the build's update configuration, fetches
 * the manifest and measures free disk space. Kept apart from `updater.ts` so the rules there are
 * testable without a network or a disk.
 */

/** Text of `app-update.yml` next to the packaged app's resources; null when the build has none. */
export function readBuiltUpdateConfig(resourcesPath: string): string | null {
  try {
    return readFileSync(join(resourcesPath, "app-update.yml"), "utf8");
  } catch {
    return null;
  }
}

/** The manifest-signing public key shipped in the installer's resources (SEC-09); null when absent. */
export function readBuiltManifestKey(resourcesPath: string): string | null {
  try {
    const text = readFileSync(join(resourcesPath, MANIFEST_KEY_FILE), "utf8").trim();
    return text === "" ? null : text;
  } catch {
    return null;
  }
}

/** SHA-512 (base64) of a file, streamed so a large installer is never held in memory. */
export function sha512Base64OfFile(path: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const hash = createHash("sha512");
    const stream = createReadStream(path);
    stream.on("error", reject);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolve(hash.digest("base64")));
  });
}

/**
 * GET the update manifest. https only, no redirects (the feed is a static host we pinned), a hard
 * size cap, a hard timeout, no cookies or credentials, and a generic User-Agent: the only thing the
 * feed learns is the app version it chose to receive in the request line.
 */
export async function fetchManifestText(url: string, timeoutMs: number, appVersion: string): Promise<string> {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:") throw new UpdateFetchError("network");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    let response: Response;
    try {
      response = await fetch(parsed, {
        redirect: "error",
        credentials: "omit",
        cache: "no-store",
        signal: controller.signal,
        headers: { "User-Agent": `InterviewAssistant/${appVersion}`, Accept: "text/yaml, text/plain, */*" },
      });
    } catch (error) {
      if (controller.signal.aborted) throw new UpdateFetchError("timeout");
      const name = (error as { name?: string })?.name;
      throw new UpdateFetchError(name === "TypeError" && /redirect/i.test(String((error as Error).message)) ? "redirect" : "network");
    }
    if (!response.ok) throw new UpdateFetchError("status", response.status);
    const declared = Number(response.headers.get("content-length") ?? "0");
    if (declared > MAX_MANIFEST_BYTES) throw new UpdateFetchError("too_large");
    const reader = response.body?.getReader();
    if (!reader) return "";
    const chunks: Uint8Array[] = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_MANIFEST_BYTES) {
        await reader.cancel();
        throw new UpdateFetchError("too_large");
      }
      chunks.push(value);
    }
    return Buffer.concat(chunks).toString("utf8");
  } catch (error) {
    if (error instanceof UpdateFetchError) throw error;
    if (controller.signal.aborted) throw new UpdateFetchError("timeout");
    throw new UpdateFetchError("network");
  } finally {
    clearTimeout(timer);
  }
}

/** Free bytes on the volume that holds `directory` (where the installer is downloaded). */
export function freeDiskBytesAt(directory: string): number {
  const stats = statfsSync(directory);
  return Number(stats.bavail) * Number(stats.bsize);
}
