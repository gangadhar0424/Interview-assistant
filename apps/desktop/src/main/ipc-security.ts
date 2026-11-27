import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export function isTrustedRenderer(
  senderUrl: string,
  devServerUrl: string | undefined,
  rendererHtmlPath: string,
): boolean {
  if (devServerUrl) {
    try {
      const sender = new URL(senderUrl);
      const expected = new URL(devServerUrl);
      return (
        sender.origin === expected.origin &&
        sender.pathname.replace(/\/$/, "") === expected.pathname.replace(/\/$/, "")
      );
    } catch {
      return false;
    }
  }
  try {
    const sender = new URL(senderUrl);
    return sender.protocol === "file:" && resolve(fileURLToPath(sender)) === resolve(rendererHtmlPath);
  } catch {
    return false;
  }
}

/**
 * The renderer dev-server URL, honoured ONLY in an unpackaged (development) run. A packaged build
 * always loads its own bundled file, so an `ELECTRON_RENDERER_URL` set in the environment can never
 * make the window (and its preload bridge) load, or trust, a different origin (E-01).
 */
export function rendererDevUrl(
  env: Record<string, string | undefined>,
  isPackaged: boolean,
): string | undefined {
  if (isPackaged) return undefined;
  const url = env.ELECTRON_RENDERER_URL;
  return url ? url : undefined;
}
