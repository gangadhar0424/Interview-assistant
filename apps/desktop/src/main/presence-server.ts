import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { HandoffChallenges } from "./handoff-challenge";

/**
 * Presence beacon (D-60). A browser cannot ask Windows whether an app handles a custom protocol,
 * so while the desktop app is running it answers one content-free request on the loopback
 * interface. The web app uses it only to decide between "open the app" and "show the install
 * hint" before launching `interviewassistant://`.
 *
 * Deliberately minimal:
 * - bound to 127.0.0.1 only, never a public interface;
 * - one path, GET (plus the OPTIONS preflight browsers send for loopback requests);
 * - the response has no body and carries no user, session, token or version data;
 * - requests whose Host header is not a loopback name are refused (DNS-rebinding hygiene);
 * - failure to bind (port taken) is silent: the web app then just shows its install hint.
 */
export const PRESENCE_PORT = 47821;
export const PRESENCE_PATH = "/ia-presence";
/** SEC-08: where the web page asks for a handoff challenge (a SHA-256 hash; the secret stays here). */
export const CHALLENGE_PATH = "/ia-presence/challenge";

const LOOPBACK_HOSTS = new Set([`127.0.0.1:${PRESENCE_PORT}`, `localhost:${PRESENCE_PORT}`]);

/**
 * The challenge endpoint. The body is exactly `{"challenge","expires_in"}`: a hash, never the
 * verifier, never a user, token or version. It is readable cross-origin on purpose (the web page must
 * read it) and that is safe because the value is not a secret: it only commits to a verifier that never
 * leaves this process. The Host check below still refuses DNS-rebinding hosts, so only a page that
 * reaches 127.0.0.1 by its real name can ask. Without a challenge store, or when the caller asked too
 * often, the answer carries no challenge and the web page falls back to the unbound flow (or the API
 * refuses it when binding is required).
 */
function handleChallengeRequest(
  req: IncomingMessage,
  res: ServerResponse,
  challenges: HandoffChallenges | undefined,
): void {
  const cors: Record<string, string> = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Private-Network": "true",
  };
  if (req.method === "OPTIONS") {
    res.writeHead(204, { ...cors, "Access-Control-Allow-Methods": "GET", "Access-Control-Max-Age": "60" }).end();
    return;
  }
  if (req.method !== "GET") {
    res.writeHead(405, { ...cors, Allow: "GET, OPTIONS" }).end();
    return;
  }
  const issued = challenges?.issue() ?? null;
  if (issued === null) {
    res.writeHead(challenges ? 429 : 404, cors).end();
    return;
  }
  res
    .writeHead(200, { ...cors, "Content-Type": "application/json; charset=utf-8" })
    .end(JSON.stringify({ challenge: issued.challenge, expires_in: issued.expiresInSeconds }));
}

export function handlePresenceRequest(
  req: IncomingMessage,
  res: ServerResponse,
  challenges?: HandoffChallenges,
): void {
  const host = (req.headers.host ?? "").toLowerCase();
  const url = (req.url ?? "").split("?")[0];
  if (LOOPBACK_HOSTS.has(host) && url === CHALLENGE_PATH) {
    handleChallengeRequest(req, res, challenges);
    return;
  }
  if (!LOOPBACK_HOSTS.has(host) || url !== PRESENCE_PATH) {
    res.writeHead(404, { "Cache-Control": "no-store" }).end();
    return;
  }
  if (req.method !== "GET" && req.method !== "OPTIONS") {
    res.writeHead(405, { Allow: "GET, OPTIONS", "Cache-Control": "no-store" }).end();
    return;
  }
  const headers: Record<string, string> = {
    "Cache-Control": "no-store",
    // Lets a page on a public origin reach loopback in browsers that gate it (Private Network
    // Access). The web app reads no body (no-cors), so no CORS allow-origin is granted.
    "Access-Control-Allow-Private-Network": "true",
  };
  if (req.method === "OPTIONS") {
    headers["Access-Control-Allow-Methods"] = "GET";
    headers["Access-Control-Max-Age"] = "60";
  }
  res.writeHead(204, headers).end();
}

/** Starts the beacon. Resolves with the server, or null when it could not bind. */
export function startPresenceServer(
  port: number = PRESENCE_PORT,
  challenges?: HandoffChallenges,
): Promise<Server | null> {
  return new Promise((resolve) => {
    const server = createServer((req, res) => handlePresenceRequest(req, res, challenges));
    server.once("error", () => resolve(null));
    server.listen(port, "127.0.0.1", () => {
      server.removeAllListeners("error");
      server.on("error", () => undefined);
      resolve(server);
    });
  });
}
