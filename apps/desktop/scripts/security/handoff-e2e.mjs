// End-to-end check of handoff binding with the REAL desktop challenge store and presence server, the REAL
// web launch logic, and a live API that requires binding. Run: node --experimental-strip-types handoff_e2e.mjs
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { request as httpRequest } from "node:http";

const [repo, apiBase, token, sessionId] = process.argv.slice(2);
const u = (p) => pathToFileURL(`${repo}/${p}`).href;
const { HandoffChallenges, challengeOf } = await import(u("apps/desktop/src/main/handoff-challenge.ts"));
const { parseHandoffLinkParts } = await import(u("apps/desktop/src/main/deep-link.ts"));
const { startPresenceServer, CHALLENGE_PATH, PRESENCE_PORT } = await import(u("apps/desktop/src/main/presence-server.ts"));
const { launchDesktop } = await import(u("apps/web/lib/handoff.ts"));
const { parseChallengeResponse } = await import(u("apps/web/lib/desktop-presence.ts"));

const results = [];
const rec = (id, ok, detail) => { results.push(Boolean(ok)); console.log(`${id.padEnd(7)} ${ok ? "PASS" : "FAIL"}  ${detail}`); };
const auth = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
const redeem = (body) => fetch(`${apiBase}/sessions/handoff/redeem`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ---- the desktop side: the real challenge store behind the real beacon on the real loopback port ----
const challenges = new HandoffChallenges();
const server = await startPresenceServer(PRESENCE_PORT, challenges);
if (!server) { console.log("ERROR the beacon port is taken (is the installed app running?). Nothing was tested."); process.exit(2); }
try {
  const beacon = (path = CHALLENGE_PATH) => fetch(`http://127.0.0.1:${PRESENCE_PORT}${path}`, { headers: { Origin: "http://localhost:3000" } });

  // ---- the web side: the real launchDesktop with the real parser, real HTTP to the beacon and the API ----
  const captured = [];
  const outcome = await launchDesktop({
    detect: async () => (await beacon("/ia-presence")).status === 204,
    fetchChallenge: async () => { const r = await beacon(); return r.ok ? parseChallengeResponse(await r.json()) : null; },
    requestCode: async (challenge) => {
      const r = await fetch(`${apiBase}/sessions/${sessionId}/handoff`, { method: "POST", headers: auth, body: challenge ? JSON.stringify({ code_challenge: challenge }) : undefined });
      if (!r.ok) throw Object.assign(new Error("issue failed"), { status: r.status, body: await r.text() });
      return r.json();
    },
    navigate: (url) => captured.push(url),
    watchForLeave: async () => true,
  });
  rec("E2E-1", outcome.kind === "opened" && captured.length === 1, `web launch flow ran against the real beacon and API: ${outcome.kind}`);
  const link = captured[0];
  const parts = parseHandoffLinkParts(link);
  rec("E2E-2", parts && parts.challenge !== null, `the launch link carries a code and a challenge: ${link.replace(/(h=)[^&]{6}[^&]*/, "$1<code>").replace(/(c=)[^&]{6}[^&]*/, "$1<challenge>")}`);

  // ---- the attacker: has the link (code + challenge) but not the verifier ----
  const stolen = await redeem({ code: parts.code });
  rec("E2E-4", stolen.status === 404, `intercepted link replayed WITHOUT a verifier -> ${stolen.status}`);
  // That attempt consumed the code, so even the legitimate desktop now fails: deliberately (single use).
  const verifier = challenges.take(parts.challenge);
  rec("E2E-5", verifier !== null && challengeOf(verifier) === parts.challenge && !link.includes(verifier), "the desktop holds the matching verifier, and it is nowhere in the launch link (the challenge is only a hash)");
  const late = await redeem({ code: parts.code, code_verifier: verifier });
  rec("E2E-6", late.status === 404, `the burned code cannot be redeemed afterwards, even with the right verifier -> ${late.status}`);

  // ---- the legitimate path again, start to finish ----
  const captured2 = [];
  await launchDesktop({
    fetchChallenge: async () => { const r = await beacon(); return r.ok ? parseChallengeResponse(await r.json()) : null; },
    requestCode: async (challenge) => (await fetch(`${apiBase}/sessions/${sessionId}/handoff`, { method: "POST", headers: auth, body: JSON.stringify({ code_challenge: challenge }) })).json(),
    navigate: (url) => captured2.push(url),
    watchForLeave: async () => true,
  });
  const p2 = parseHandoffLinkParts(captured2[0]);
  const v2 = challenges.take(p2.challenge);
  const good = await redeem({ code: p2.code, code_verifier: v2 });
  const body = await good.json();
  rec("E2E-7", good.status === 200 && typeof body.access_token === "string" && body.session?.id === sessionId, `legitimate desktop redeems code + its own verifier -> ${good.status}, signed in with the prepared session`);
  rec("E2E-8", challenges.take(p2.challenge) === null, "the verifier is single use: a second take finds nothing (a replayed link cannot redeem)");

  // ---- a guessed verifier, and an unbound code while the server requires binding ----
  const captured3 = [];
  await launchDesktop({
    fetchChallenge: async () => { const r = await beacon(); return r.ok ? parseChallengeResponse(await r.json()) : null; },
    requestCode: async (challenge) => (await fetch(`${apiBase}/sessions/${sessionId}/handoff`, { method: "POST", headers: auth, body: JSON.stringify({ code_challenge: challenge }) })).json(),
    navigate: (url) => captured3.push(url), watchForLeave: async () => true,
  });
  const p3 = parseHandoffLinkParts(captured3[0]);
  const guess = await redeem({ code: p3.code, code_verifier: createHash("sha256").update("guess").digest("base64url") });
  rec("E2E-9", guess.status === 404, `a guessed verifier -> ${guess.status}`);
  const unbound = await fetch(`${apiBase}/sessions/${sessionId}/handoff`, { method: "POST", headers: auth });
  rec("E2E-10", unbound.status === 422, `an unbound code request while the server requires binding -> ${unbound.status} (an older desktop must update)`);

  // ---- the beacon's own hygiene, over real HTTP ----
  const rebinding = await new Promise((resolve) => {
    const req = httpRequest({ host: "127.0.0.1", port: PRESENCE_PORT, path: CHALLENGE_PATH, method: "GET", headers: { Host: "evil.example:47821" }, agent: false }, (res) => { res.resume(); resolve({ status: res.statusCode }); });
    req.on("error", () => resolve({ status: 0 }));
    req.end();
  });
  rec("E2E-11", rebinding.status === 404, `a DNS-rebinding Host header gets no challenge -> ${rebinding.status}`);
  const pre = await fetch(`http://127.0.0.1:${PRESENCE_PORT}${CHALLENGE_PATH}`, { method: "OPTIONS", headers: { Origin: "http://localhost:3000", "Access-Control-Request-Method": "GET", "Access-Control-Request-Private-Network": "true" } });
  rec("E2E-12", pre.status === 204 && pre.headers.get("access-control-allow-private-network") === "true", `the browser's private-network preflight is answered -> ${pre.status}`);
} finally {
  await new Promise((r) => server.close(r));
}
console.log(`SUMMARY ${results.filter(Boolean).length} PASS, ${results.filter((x) => !x).length} FAIL of ${results.length}`);
process.exit(results.every(Boolean) ? 0 : 1);
