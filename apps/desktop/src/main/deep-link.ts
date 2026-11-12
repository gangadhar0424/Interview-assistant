// Strict parsing of the web -> desktop handoff link (D-55):
//
//   interviewassistant://start?h=<43 url-safe characters>
//
// The link carries ONLY an opaque, short-lived, single-use code. Anything else (another scheme or
// host, extra parameters, a path, a fragment, credentials, a port, an oversized value) is ignored.
// The code is never logged and never forwarded to the renderer. Pure functions: no Electron.

export const PROTOCOL_SCHEME = "interviewassistant";
const LINK_PREFIX = `${PROTOCOL_SCHEME}://`;
const CODE_PATTERN = /^[A-Za-z0-9_-]{43}$/;
/** A real link is about 70 characters; anything much longer is not ours. */
const MAX_LINK_LENGTH = 200;

/** The handoff code in `raw`, or null for anything that is not exactly a well-formed handoff link. */
export function parseHandoffLink(raw: unknown): string | null {
  if (typeof raw !== "string" || raw.length > MAX_LINK_LENGTH) return null;
  // Control characters, whitespace and backslashes never appear in a genuine link.
  if (/[\u0000- \u007f\\]/.test(raw)) return null;
  if (!raw.toLowerCase().startsWith(LINK_PREFIX)) return null;
  // Exactly `<scheme>://start?h=<code>`: no path, no fragment, no userinfo, no port, no extras.
  const rest = raw.slice(LINK_PREFIX.length);
  const match = /^start\/?\?h=([^&#?]*)$/i.exec(rest);
  if (!match) return null;
  const code = match[1] ?? "";
  return CODE_PATTERN.test(code) ? code : null;
}

/**
 * The first argument that looks like one of our links. On Windows the OS starts (or re-starts) the
 * app with the link in argv; other arguments (flags, paths) are ignored.
 */
export function extractLinkFromArgv(argv: readonly unknown[]): string | null {
  for (const arg of argv) {
    if (typeof arg === "string" && arg.toLowerCase().startsWith(LINK_PREFIX)) return arg;
  }
  return null;
}

/**
 * Hands links to the handoff controller once it exists. A cold start can receive the link before
 * the window and IPC are ready, so early links are held (at most a few, newest wins) in memory only.
 */
export class DeepLinkInbox {
  private pending: string[] = [];
  private handler: ((raw: string) => void) | null = null;

  deliver(raw: string): void {
    if (this.handler) {
      this.handler(raw);
      return;
    }
    this.pending.push(raw);
    if (this.pending.length > 3) this.pending.shift();
  }

  attach(handler: (raw: string) => void): void {
    this.handler = handler;
    const queued = this.pending;
    this.pending = [];
    for (const raw of queued) handler(raw);
  }

  detach(): void {
    this.handler = null;
    this.pending = [];
  }
}
