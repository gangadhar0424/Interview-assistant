// Authorized packaged-app probe: launches the app built from this repository with an isolated profile,
// inspects it through the Chromium DevTools protocol, and stops ONLY the process tree it spawned.
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [exe, port, root, bridgeName] = process.argv.slice(2);
const { getCurrentFuseWire, FuseV1Options } = await import(`file:///${root}/node_modules/@electron/fuses/dist/index.js`);
const FuseState = { ENABLE: 49, DISABLE: 48, REMOVED: 114 }; // wire bytes: '1', '0', 'r'
const out = [];
const rec = (id, ok, detail) => {
  out.push(Boolean(ok));
  console.log(`${id.padEnd(8)} ${ok ? "PASS" : "FAIL"}  ${detail}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- fuses actually present in the packaged executable ----
const wire = await getCurrentFuseWire(exe);
const names = Object.keys(FuseV1Options).filter((k) => Number.isNaN(Number(k)));
const state = {};
for (const n of names) {
  const v = wire[FuseV1Options[n]];
  state[n] = v === FuseState.ENABLE ? "ENABLED" : v === FuseState.DISABLE ? "DISABLED" : v === FuseState.REMOVED ? "REMOVED" : String(v);
}
console.log("FUSES   ", JSON.stringify(state));
console.log("GrantFileProtocolExtraPrivileges =", state.GrantFileProtocolExtraPrivileges);
rec("FUSE-1", state.RunAsNode === "DISABLED" && state.EnableNodeOptionsEnvironmentVariable === "DISABLED" && state.EnableNodeCliInspectArguments === "DISABLED", "RunAsNode, NODE_OPTIONS and node inspect arguments are DISABLED in the packaged exe");
rec("FUSE-2", state.OnlyLoadAppFromAsar === "ENABLED" && state.EnableEmbeddedAsarIntegrityValidation === "ENABLED", "app only loads from ASAR and ASAR integrity validation is ENABLED");

const userData = mkdtempSync(join(tmpdir(), "ia-probe-ud-"));
const child = spawn(exe, [`--remote-debugging-port=${port}`, `--user-data-dir=${userData}`], { stdio: "ignore" });
console.log("spawned pid", child.pid);
const killTree = () => {
  spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
};

try {
  let target = null;
  for (let i = 0; i < 60 && !target; i++) {
    await sleep(500);
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json`);
      const j = await r.json();
      target = j.find((t) => t.type === "page") ?? null;
    } catch {
      /* not up yet */
    }
  }
  if (!target) {
    rec("LAUNCH", false, "the app did not expose a page target within 30 s");
    throw new Error("no page");
  }
  console.log("page url:", target.url);
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = rej;
  });
  let id = 0;
  const pending = new Map();
  let shown = 0;
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) {
      pending.get(d.id)(d);
      pending.delete(d.id);
    } else if ((d.method === "Runtime.exceptionThrown" || (d.method === "Log.entryAdded" && d.params?.entry?.level === "error") || (d.method === "Runtime.consoleAPICalled" && d.params?.type === "error")) && shown < 8) {
      shown += 1;
      const e = d.params?.exceptionDetails ?? d.params?.entry ?? d.params;
      console.log("RENDERER-EVENT", d.method, JSON.stringify(e).slice(0, 260));
    }
  };
  ws.onclose = () => console.log("DEVTOOLS socket closed");
  const send = (method, params = {}) =>
    Promise.race([
      new Promise((res) => {
        const n = ++id;
        pending.set(n, res);
        ws.send(JSON.stringify({ id: n, method, params }));
      }),
      new Promise((res) => setTimeout(() => res({ result: { exceptionDetails: { text: "timeout: the page did not answer DevTools within 8 s" } } }), 8000)),
    ]);
  await send("Runtime.enable");
  await send("Log.enable");
  const evalJs = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.result?.exceptionDetails) return { error: r.result.exceptionDetails.exception?.description ?? r.result.exceptionDetails.text };
    return { value: r.result?.result?.value };
  };
  await sleep(2500);

  const render = await evalJs("JSON.stringify({children: document.getElementById('root') ? document.getElementById('root').children.length : -1, title: document.title, href: location.href})");
  const rv = JSON.parse(render.value ?? "{}");
  rec("LOAD", rv.children > 0 && String(rv.href).startsWith("file:"), `renderer loaded and rendered (root children=${rv.children}, scheme=${String(rv.href).split(":")[0]})`);

  const g = JSON.parse((await evalJs("JSON.stringify({require: typeof require, process: typeof process, module: typeof module, Buffer: typeof Buffer, global: typeof global, ipcRenderer: typeof ipcRenderer, electron: typeof electron})")).value);
  rec("ISO-1", Object.values(g).every((v) => v === "undefined"), `no Node or Electron globals in the renderer: ${JSON.stringify(g)}`);

  const surface = JSON.parse((await evalJs(`JSON.stringify(Object.fromEntries(Object.entries(window['${bridgeName}'] || {}).map(([k, v]) => [k, typeof v === 'object' && v ? Object.keys(v) : typeof v])))`)).value);
  const flat = Object.values(surface).flat().concat(Object.keys(surface));
  rec("ISO-2", Object.keys(surface).length > 0 && !flat.some((k) => /^(send|invoke|on|once|sendSync|removeAllListeners|postMessage)$/.test(k)), `bridge exposes only named functions (groups: ${Object.keys(surface).join(", ")}); no generic send/invoke/on`);

  await evalJs("location.href = 'https://example.com/'");
  await sleep(2000);
  const after = (await evalJs("location.href")).value;
  rec("NAV-1", String(after).startsWith("file:"), `navigating the window to an external site is blocked (still ${String(after).split(":")[0]}:)`);
  const opened = (await evalJs("window.open('https://example.com/') === null")).value;
  rec("NAV-2", opened === true, "window.open() is denied (returns null)");

  const f = await evalJs("fetch('https://example.com/').then(() => 'ALLOWED', (e) => 'BLOCKED: ' + e.name)");
  rec("CSP-1", String(f.value).startsWith("BLOCKED"), `fetch to an external origin is blocked by the CSP (${f.value})`);
  const fl = await evalJs("fetch('file:///C:/Windows/win.ini').then(() => 'ALLOWED', (e) => 'BLOCKED: ' + e.name)");
  rec("CSP-2", String(fl.value).startsWith("BLOCKED"), `fetch of a local file is blocked (${fl.value})`);
  // DevTools' Runtime.evaluate is exempt from the page's eval restriction, so a dynamic eval() run through it
  // proves nothing. The policy the browser enforces is verified instead (dynamic eval is NOT TESTED here).
  const policy = (await evalJs("(document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]') || {}).content || ''")).value ?? "";
  rec("CSP-3", policy.includes("script-src 'self'") && !/unsafe-eval|unsafe-inline|wasm-unsafe-eval/.test(policy) && policy.includes("connect-src 'none'") && policy.includes("object-src 'none'") && policy.includes("frame-src 'none'"), `the enforced CSP has no unsafe-eval or unsafe-inline and denies connect, object and frame: ${policy}`);
  const inl = await evalJs("(() => { const s = document.createElement('script'); s.textContent = 'window.__probe = 1'; document.head.appendChild(s); return typeof window.__probe; })()");
  rec("CSP-4", inl.value === "undefined", `an injected inline script does not run (typeof marker = ${inl.value})`);
  const img = await evalJs("new Promise((res) => { const i = new Image(); i.onload = () => res('LOADED'); i.onerror = () => res('BLOCKED'); i.src = 'https://example.com/x.png'; setTimeout(() => res('BLOCKED(timeout)'), 4000); })");
  rec("CSP-5", String(img.value).startsWith("BLOCKED"), `an external image is blocked by img-src (${img.value})`);

  const cam = await evalJs("navigator.mediaDevices.getUserMedia({ video: true }).then(() => 'GRANTED', (e) => 'DENIED: ' + e.name)");
  rec("PERM-1", String(cam.value).startsWith("DENIED"), `camera (video) request is denied (${cam.value})`);
  const mic = await evalJs("navigator.mediaDevices.getUserMedia({ audio: true }).then(() => 'GRANTED', (e) => 'DENIED: ' + e.name)");
  rec("PERM-2", String(mic.value).startsWith("DENIED"), `microphone request is denied while the user has not pressed Start (${mic.value})`);
  const disp = await evalJs("navigator.mediaDevices.getDisplayMedia({ video: true }).then(() => 'GRANTED', (e) => 'DENIED: ' + e.name)");
  rec("PERM-3", String(disp.value).startsWith("DENIED"), `screen capture request (getDisplayMedia) is denied (${disp.value})`);

  const cp = await evalJs(`window['${bridgeName}'].app.getCapturePrivacy().then((r) => JSON.stringify(r))`);
  const cpv = JSON.parse(cp.value ?? "{}");
  rec("M15-1", cpv.ok === true && cpv.data?.enabled === false, `screen-capture privacy is OFF by default in a fresh profile (${String(cp.value).slice(0, 150)})`);
  const settings = await evalJs(`window['${bridgeName}'].app.getSettings().then((r) => r.ok)`);
  rec("IPC-1", settings.value === true, "a legitimate IPC call from the trusted renderer works (settings)");
  const bad = await evalJs(`window['${bridgeName}'].app.setCapturePrivacy('yes').then((r) => JSON.stringify(r))`);
  rec("IPC-2", JSON.parse(bad.value ?? "{}").ok === false, `IPC input validation: a non-boolean for setCapturePrivacy is refused (${String(bad.value).slice(0, 110)})`);
  const ack = await evalJs(`window['${bridgeName}'].app.setCapturePrivacy(true).then((r) => JSON.stringify(r))`);
  const av = JSON.parse(ack.value ?? "{}");
  rec("M15-2", av.ok === false || av.data?.enabled === false, `the renderer cannot turn capture privacy on without the acknowledgement and native confirmation (${String(ack.value).slice(0, 150)})`);
  ws.close();
} catch (error) {
  console.log("ERROR", error instanceof Error ? error.message : String(error));
} finally {
  killTree();
  await sleep(1500);
  try {
    rmSync(userData, { recursive: true, force: true });
  } catch {
    /* profile files may still be locked */
  }
}
console.log(`SUMMARY ${out.filter(Boolean).length} PASS, ${out.filter((x) => !x).length} FAIL of ${out.length}`);
process.exit(out.length > 0 && out.every(Boolean) ? 0 : 1);
