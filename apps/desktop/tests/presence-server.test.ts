/** Presence beacon (D-60): loopback-only, content-free, GET/OPTIONS on one path. */
import { request } from "node:http";
import type { Server } from "node:http";
import { afterEach, describe, expect, it } from "vitest";
import { PRESENCE_PATH, PRESENCE_PORT, startPresenceServer } from "../src/main/presence-server";

const TEST_PORT = PRESENCE_PORT; // Host-header allowlist is tied to the real port
let server: Server | null = null;

afterEach(async () => {
  await new Promise<void>((done) => (server ? server.close(() => done()) : done()));
  server = null;
});

function call(
  method: string,
  path: string,
  host = `127.0.0.1:${TEST_PORT}`,
): Promise<{ status: number; body: string; headers: Record<string, unknown> }> {
  return new Promise((resolve, reject) => {
    const req = request({ host: "127.0.0.1", port: TEST_PORT, method, path, agent: false, headers: { Host: host, Connection: "close" } }, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body, headers: res.headers }));
    });
    req.on("error", reject);
    req.end();
  });
}

describe("presence server", () => {
  it("answers GET on the presence path with an empty 204 and no data", async () => {
    server = await startPresenceServer();
    expect(server).not.toBeNull();
    const res = await call("GET", PRESENCE_PATH);
    expect(res.status).toBe(204);
    expect(res.body).toBe("");
    expect(res.headers["cache-control"]).toBe("no-store");
    expect(res.headers["access-control-allow-origin"]).toBeUndefined();
  });

  it("answers the loopback preflight", async () => {
    server = await startPresenceServer();
    const res = await call("OPTIONS", PRESENCE_PATH);
    expect(res.status).toBe(204);
    expect(res.headers["access-control-allow-private-network"]).toBe("true");
  });

  it("binds to the loopback interface only", async () => {
    server = await startPresenceServer();
    const address = server?.address();
    expect(typeof address === "object" && address?.address).toBe("127.0.0.1");
  });

  it("refuses other paths, other methods and non-loopback Host headers", async () => {
    server = await startPresenceServer();
    expect((await call("GET", "/")).status).toBe(404);
    expect((await call("GET", "/ia-presence/x")).status).toBe(404);
    expect((await call("POST", PRESENCE_PATH)).status).toBe(405);
    expect((await call("GET", PRESENCE_PATH, "evil.example:47821")).status).toBe(404);
  });

  it("resolves null (no throw) when the port is already taken", async () => {
    server = await startPresenceServer();
    expect(await startPresenceServer()).toBeNull();
  });
});
