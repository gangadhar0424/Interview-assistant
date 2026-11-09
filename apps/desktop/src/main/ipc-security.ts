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
