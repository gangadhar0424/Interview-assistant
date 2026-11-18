import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

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

const LOOPBACK_HOSTS = new Set([`127.0.0.1:${PRESENCE_PORT}`, `localhost:${PRESENCE_PORT}`]);

export function handlePresenceRequest(req: IncomingMessage, res: ServerResponse): void {
  const host = (req.headers.host ?? "").toLowerCase();
  const url = (req.url ?? "").split("?")[0];
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
export function startPresenceServer(port: number = PRESENCE_PORT): Promise<Server | null> {
  return new Promise((resolve) => {
    const server = createServer(handlePresenceRequest);
    server.once("error", () => resolve(null));
    server.listen(port, "127.0.0.1", () => {
      server.removeAllListeners("error");
      server.on("error", () => undefined);
      resolve(server);
    });
  });
}
