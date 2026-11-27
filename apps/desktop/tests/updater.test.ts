import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  canInstallNow,
  classifyEngineError,
  UPDATE_TIMINGS,
  UpdateFetchError,
  UpdateManager,
  type EngineEvent,
  type UpdateDeps,
  type UpdateEngine,
} from "../src/main/updater";
import { sha512Base64OfFile } from "../src/main/updater-io";
import { generateManifestKeyPair, signManifest } from "../src/shared/update-manifest.mjs";
import type { UpdateState } from "../src/shared/updates";

// SEC-09: every manifest the fake feed serves is signed with this key; the app pins its public half.
const KEYS = generateManifestKeyPair();
const DOWNLOADED_PATH = "C:/Users/test/AppData/Local/updates/installer.exe";

const SHA = createHash("sha512").update("installer").digest("base64");
const NAME = "Interview Assistant-0.2.0-Windows-x64.exe";
const SIZE = 120_000_000;
const FEED = "https://updates.example.com/desktop/";
const SIGNED_CONFIG = `provider: generic\nurl: ${FEED}\npublisherName:\n  - Example Ltd\n`;

function manifest(version = "0.2.0", over: Record<string, string> = {}): string {
  const o = { url: NAME, sha512: SHA, size: String(SIZE), ...over };
  return `version: ${version}\nfiles:\n  - url: ${o.url}\n    sha512: ${o.sha512}\n    size: ${o.size}\npath: ${o.url}\nsha512: ${o.sha512}\nreleaseDate: '2026-11-22T10:00:00.000Z'\n`;
}

class FakeClock {
  time = 1_000_000;
  private next = 1;
  private timers = new Map<number, { at: number; run: () => void }>();
  now = (): number => this.time;
  setTimer = (run: () => void, ms: number) => {
    const id = this.next++;
    this.timers.set(id, { at: this.time + ms, run });
    return { cancel: () => void this.timers.delete(id) };
  };
  get pending(): number {
    return this.timers.size;
  }
  /** Run every timer due within `ms`, in order, letting promises settle between them. */
  async advance(ms: number): Promise<void> {
    const end = this.time + ms;
    for (;;) {
      const due = [...this.timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      this.timers.delete(due[0]);
      this.time = Math.max(this.time, due[1].at);
      due[1].run();
      await settle();
    }
    this.time = end;
  }
}
const settle = async (): Promise<void> => {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
};

class FakeEngine implements UpdateEngine {
  listener: ((event: EngineEvent) => void) | null = null;
  configured = 0;
  checks = 0;
  downloads = 0;
  cancelled = 0;
  installs = 0;
  onCheck: () => Promise<void> | void = () => this.emit({ type: "update-available", version: "0.2.0" });
  onDownload: () => Promise<void> | void = () => {
    this.emit({ type: "download-progress", percent: 50 });
    this.emit({ type: "update-downloaded", version: "0.2.0" });
  };
  onInstall: () => void = () => undefined;
  configure(): void {
    this.configured += 1;
  }
  subscribe(listener: (event: EngineEvent) => void): () => void {
    this.listener = listener;
    return () => {
      this.listener = null;
    };
  }
  emit(event: EngineEvent): void {
    // Like the real engine, a downloaded event names the installer file (unless a test omits it).
    const withFile =
      event.type === "update-downloaded" && !("downloadedFile" in event)
        ? { ...event, downloadedFile: DOWNLOADED_PATH }
        : event;
    this.listener?.(withFile);
  }
  async checkForUpdates(): Promise<void> {
    this.checks += 1;
    await this.onCheck();
  }
  async downloadUpdate(): Promise<void> {
    this.downloads += 1;
    await this.onDownload();
  }
  cancelDownload(): void {
    this.cancelled += 1;
  }
  quitAndInstall(): void {
    this.installs += 1;
    this.onInstall();
  }
}

type Harness = ReturnType<typeof harness>;
function harness(over: Partial<UpdateDeps> & { config?: string | null } = {}) {
  const clock = new FakeClock();
  const engine = new FakeEngine();
  const states: UpdateState[] = [];
  const fetchManifest = vi.fn<(url: string, ms: number) => Promise<string>>(async () => manifest());
  const prepare = vi.fn(async () => undefined);
  const { config = SIGNED_CONFIG, ...rest } = over;
  // The signature is made over whatever text the fake feed most recently served.
  let lastServed = "";
  const rawFetch = rest.fetchManifest ?? fetchManifest;
  const servedFetch = async (url: string, ms: number): Promise<string> => {
    lastServed = await rawFetch(url, ms);
    return lastServed;
  };
  const fetchSignature = vi.fn<(url: string, ms: number) => Promise<string>>(async () =>
    signManifest(lastServed, KEYS.privateKeyPem),
  );
  const hashFile = vi.fn<(path: string) => Promise<string>>(async () => SHA);
  const deps: UpdateDeps = {
    engine,
    currentVersion: "0.1.0",
    isPackaged: true,
    disabledByEnvironment: false,
    readUpdateConfig: () => config,
    fetchManifest: servedFetch,
    fetchSignature,
    readManifestKey: () => KEYS.publicKey,
    hashFile,
    freeDiskBytes: async () => 10 * 1024 * 1024 * 1024,
    prepareForInstall: prepare,
    now: clock.now,
    random: () => 0.5,
    setTimer: clock.setTimer,
    emit: (s) => states.push(s),
    log: () => undefined,
    ...rest,
  };
  const manager = new UpdateManager(deps);
  return { clock, engine, states, fetchManifest, fetchSignature, hashFile, prepare, manager, deps };
}
const statuses = (h: Harness) => h.states.map((s) => s.status);

describe("when updates are not allowed", () => {
  const cases: Array<[string, Partial<UpdateDeps> & { config?: string | null }, string]> = [
    ["a development build", { isPackaged: false }, "development"],
    ["turned off by the environment", { disabledByEnvironment: true }, "disabled_by_environment"],
    ["an installer without update metadata", { config: null }, "not_configured"],
    ["an unreadable configuration", { config: "!!!not yaml" }, "not_configured"],
    ["an unsigned build (no pinned publisher)", { config: `provider: generic\nurl: ${FEED}\n` }, "unsigned_build"],
    ["an insecure feed", { config: "provider: generic\nurl: http://updates.example.com/\npublisherName:\n  - X\n" }, "insecure_feed"],
  ];
  it.each(cases)("%s never checks, never loads the engine and schedules nothing", async (_name, over, reason) => {
    const h = harness(over);
    expect(h.manager.getState()).toEqual({ status: "disabled", reason });
    h.manager.start();
    await h.clock.advance(UPDATE_TIMINGS.periodicMs * 2);
    expect(h.engine.configured).toBe(0);
    expect(h.fetchManifest).not.toHaveBeenCalled();
    expect(h.clock.pending).toBe(0);
    expect(await h.manager.check("manual")).toEqual({ status: "disabled", reason });
    expect(await h.manager.download()).toEqual({ status: "disabled", reason });
    expect(await h.manager.installAndRestart()).toEqual({ status: "disabled", reason });
    expect(h.fetchManifest).not.toHaveBeenCalled();
    expect(h.engine.installs).toBe(0);
  });
});

describe("checking for updates", () => {
  it("starts quietly: configures the engine and checks after a delay, not immediately", async () => {
    const h = harness();
    h.manager.start();
    expect(h.engine.configured).toBe(1);
    expect(h.fetchManifest).not.toHaveBeenCalled();
    await h.clock.advance(UPDATE_TIMINGS.startupDelayMs - 1);
    expect(h.fetchManifest).not.toHaveBeenCalled();
    await h.clock.advance(2);
    expect(h.fetchManifest).toHaveBeenCalledTimes(1);
    expect(h.fetchManifest.mock.calls[0]?.[0]).toBe(`${FEED}latest.yml`);
    expect(h.manager.getState()).toMatchObject({ status: "available", version: "0.2.0", sizeBytes: SIZE });
  });

  it("start is idempotent", () => {
    const h = harness();
    h.manager.start();
    h.manager.start();
    expect(h.engine.configured).toBe(1);
  });

  it("repeats about every six hours with jitter", async () => {
    const h = harness({ random: () => 0 });
    h.fetchManifest.mockImplementation(async () => manifest("0.1.0"));
    h.manager.start();
    await h.clock.advance(UPDATE_TIMINGS.startupDelayMs + 1);
    expect(h.fetchManifest).toHaveBeenCalledTimes(1);
    await h.clock.advance(UPDATE_TIMINGS.periodicMs - 1000);
    expect(h.fetchManifest).toHaveBeenCalledTimes(1);
    await h.clock.advance(2000);
    expect(h.fetchManifest).toHaveBeenCalledTimes(2);
  });

  it("reports no update when the feed's version is the same, and does not involve the engine", async () => {
    const h = harness();
    h.fetchManifest.mockResolvedValue(manifest("0.1.0"));
    expect(await h.manager.check("manual")).toMatchObject({ status: "none" });
    expect(h.engine.checks).toBe(0);
  });

  it("never offers a downgrade", async () => {
    const h = harness({ currentVersion: "0.5.0" });
    h.fetchManifest.mockResolvedValue(manifest("0.2.0"));
    expect((await h.manager.check("manual")).status).toBe("none");
    expect(h.engine.checks).toBe(0);
  });

  it("refuses an invalid manifest without asking the engine", async () => {
    const bad = [
      "not yaml at all",
      manifest("latest"),
      manifest("0.2.0", { url: "..\\evil.exe" }),
      manifest("0.2.0", { sha512: "deadbeef" }),
      manifest("0.2.0", { size: "-1" }),
      "version: 0.2.0\n",
      "version: &x 0.2.0\nfiles: []\n",
    ];
    for (const text of bad) {
      const h = harness();
      h.fetchManifest.mockResolvedValue(text);
      const s = await h.manager.check("manual");
      expect(s, text).toMatchObject({ status: "error", code: "manifest_invalid", retryable: false });
      expect(h.engine.checks).toBe(0);
    }
  });

  it("collapses concurrent checks into one request", async () => {
    const h = harness();
    let release: (value: string) => void = () => undefined;
    h.fetchManifest.mockImplementation(() => new Promise<string>((resolve) => (release = resolve)));
    const a = h.manager.check("manual");
    const b = h.manager.check("manual");
    const c = h.manager.check("periodic");
    release(manifest("0.1.0"));
    await Promise.all([a, b, c]);
    expect(h.fetchManifest).toHaveBeenCalledTimes(1);
  });

  it("ignores repeated checks inside the minimum interval", async () => {
    const h = harness();
    h.fetchManifest.mockResolvedValue(manifest("0.1.0"));
    await h.manager.check("manual");
    await h.manager.check("manual");
    expect(h.fetchManifest).toHaveBeenCalledTimes(1);
    await h.clock.advance(UPDATE_TIMINGS.manualMinIntervalMs + 1);
    await h.manager.check("manual");
    expect(h.fetchManifest).toHaveBeenCalledTimes(2);
    await h.manager.check("periodic"); // an automatic check waits longer
    expect(h.fetchManifest).toHaveBeenCalledTimes(2);
    await h.clock.advance(UPDATE_TIMINGS.autoMinIntervalMs + 1);
    await h.manager.check("periodic");
    expect(h.fetchManifest).toHaveBeenCalledTimes(3);
  });

  it("a manual check never replaces a downloaded update", async () => {
    const h = harness();
    h.manager.start();
    await h.manager.check("manual");
    await h.manager.download();
    expect(h.manager.getState().status).toBe("downloaded");
    await h.clock.advance(UPDATE_TIMINGS.manualMinIntervalMs + 1);
    expect((await h.manager.check("manual")).status).toBe("downloaded");
  });

  it("refuses an engine that announces a different version than the manifest", async () => {
    const h = harness();
    h.manager.start();
    h.engine.onCheck = () => h.engine.emit({ type: "update-available", version: "9.9.9" });
    expect(await h.manager.check("manual")).toMatchObject({ status: "error", code: "version_mismatch" });
  });

  it("refuses an engine that denies an update the manifest announced", async () => {
    const h = harness();
    h.manager.start();
    h.engine.onCheck = () => h.engine.emit({ type: "update-not-available" });
    expect(await h.manager.check("manual")).toMatchObject({ status: "error", code: "version_mismatch" });
  });

  it("treats a silent engine as a failure, never as success", async () => {
    const h = harness();
    h.manager.start();
    h.engine.onCheck = () => undefined;
    expect(await h.manager.check("manual")).toMatchObject({ status: "error", code: "version_mismatch" });
  });

  it("a hung engine check times out", async () => {
    const h = harness();
    h.manager.start();
    h.engine.onCheck = () => new Promise<void>(() => undefined);
    const pending = h.manager.check("manual");
    await settle();
    await h.clock.advance(UPDATE_TIMINGS.engineCheckTimeoutMs + 1);
    expect(await pending).toMatchObject({ status: "error", code: "timeout", retryable: true });
  });

  it("ignores engine events nobody asked for", async () => {
    const h = harness();
    h.manager.start();
    for (const event of [
      { type: "update-available", version: "0.2.0" },
      { type: "update-downloaded", version: "0.2.0" },
      { type: "download-progress", percent: 40 },
      { type: "error", code: "network" },
      { type: "update-not-available" },
    ] as EngineEvent[]) {
      h.engine.emit(event);
    }
    expect(h.manager.getState()).toEqual({ status: "idle", lastCheckedAt: null });
    expect(h.states).toEqual([]);
  });
});

describe("failure handling when checking", () => {
  it.each([
    [new UpdateFetchError("network"), "network"],
    [new UpdateFetchError("timeout"), "timeout"],
    [new UpdateFetchError("status", 503), "feed_unavailable"],
    [new UpdateFetchError("status", 404), "feed_unavailable"],
    [new UpdateFetchError("too_large"), "manifest_invalid"],
    [new UpdateFetchError("redirect"), "manifest_invalid"],
    [Object.assign(new Error("getaddrinfo ENOTFOUND updates.example.com"), { code: "ENOTFOUND" }), "network"],
  ])("a failed manifest fetch (%#) becomes the fixed error %s", async (error, code) => {
    const h = harness();
    h.fetchManifest.mockRejectedValue(error);
    const s = await h.manager.check("manual");
    expect(s).toMatchObject({ status: "error", code });
    expect(JSON.stringify(s)).not.toMatch(/ENOTFOUND|updates\.example\.com|getaddrinfo/); // never the raw message or host
  });

  it("a manual check never retries on its own", async () => {
    const h = harness();
    h.fetchManifest.mockRejectedValue(new UpdateFetchError("network"));
    await h.manager.check("manual");
    await h.clock.advance(UPDATE_TIMINGS.retryCapMs * 5);
    expect(h.fetchManifest).toHaveBeenCalledTimes(1);
  });

  it("automatic retries are bounded, back off exponentially and use jitter", async () => {
    const h = harness({ random: () => 1 }); // maximum jitter
    h.fetchManifest.mockRejectedValue(new UpdateFetchError("network"));
    h.manager.start();
    await h.clock.advance(UPDATE_TIMINGS.startupDelayMs + 1);
    expect(h.fetchManifest).toHaveBeenCalledTimes(1);
    const base = UPDATE_TIMINGS.retryBaseMs;
    // first retry after 60 s + 10 % jitter
    await h.clock.advance(base * 1.1 - 1000);
    expect(h.fetchManifest).toHaveBeenCalledTimes(1);
    await h.clock.advance(2000);
    expect(h.fetchManifest).toHaveBeenCalledTimes(2);
    // second after 120 s + jitter, third after 240 s + jitter, then no more until the periodic check
    await h.clock.advance(base * 2 * 1.1 + 1);
    expect(h.fetchManifest).toHaveBeenCalledTimes(3);
    await h.clock.advance(base * 4 * 1.1 + 1);
    expect(h.fetchManifest).toHaveBeenCalledTimes(4);
    await h.clock.advance(UPDATE_TIMINGS.retryCapMs * 4);
    expect(h.fetchManifest).toHaveBeenCalledTimes(4);
  });

  it("does not retry a non-retryable failure", async () => {
    const h = harness();
    h.fetchManifest.mockResolvedValue("garbage");
    h.manager.start();
    await h.clock.advance(UPDATE_TIMINGS.startupDelayMs + 1);
    await h.clock.advance(UPDATE_TIMINGS.retryCapMs * 3);
    expect(h.fetchManifest).toHaveBeenCalledTimes(1);
    expect(h.manager.getState()).toMatchObject({ status: "error", code: "manifest_invalid" });
  });

  it("recovers by itself after the network returns", async () => {
    const h = harness();
    h.fetchManifest.mockRejectedValueOnce(new UpdateFetchError("network"));
    h.manager.start();
    await h.clock.advance(UPDATE_TIMINGS.startupDelayMs + 1);
    expect(h.manager.getState()).toMatchObject({ status: "error", code: "network" });
    await h.clock.advance(UPDATE_TIMINGS.retryBaseMs * 1.2);
    expect(h.manager.getState()).toMatchObject({ status: "available", version: "0.2.0" });
  });
});

describe("downloading", () => {
  async function available(over: Parameters<typeof harness>[0] = {}): Promise<Harness> {
    const h = harness(over);
    h.manager.start();
    await h.manager.check("manual");
    expect(h.manager.getState().status).toBe("available");
    return h;
  }

  it("only starts from an announced update, on the user's request", async () => {
    const h = harness();
    h.manager.start();
    expect((await h.manager.download()).status).toBe("idle");
    expect(h.engine.downloads).toBe(0);
    await h.manager.check("manual");
    expect(h.engine.downloads).toBe(0); // announcing never downloads
    expect((await h.manager.download()).status).toBe("downloaded");
    expect(h.engine.downloads).toBe(1);
  });

  it("reports monotonic progress once per percent and ignores duplicates", async () => {
    const h = await available();
    h.engine.onDownload = () => {
      for (const p of [10, 10, 5, 30.4, 30.9, 29, 99.9, 150, -4]) h.engine.emit({ type: "download-progress", percent: p });
      h.engine.emit({ type: "update-downloaded", version: "0.2.0" });
    };
    await h.manager.download();
    const progress = h.states.filter((s) => s.status === "downloading").map((s) => (s as { percent: number }).percent);
    expect(progress).toEqual([0, 10, 30, 99]);
    expect(h.manager.getState()).toEqual({ status: "downloaded", version: "0.2.0" });
  });

  it("ignores a duplicate 'downloaded' event", async () => {
    const h = await available();
    await h.manager.download();
    const before = h.states.length;
    h.engine.emit({ type: "update-downloaded", version: "0.2.0" });
    expect(h.states).toHaveLength(before);
  });

  it("refuses a downloaded update of an unexpected version", async () => {
    const h = await available();
    h.engine.onDownload = () => h.engine.emit({ type: "update-downloaded", version: "0.9.0" });
    expect(await h.manager.download()).toMatchObject({ status: "error", code: "version_mismatch" });
    expect(canInstallNow(h.manager.getState())).toBe(false);
  });

  it("stops before downloading when the disk is too small", async () => {
    const h = await available({ freeDiskBytes: async () => SIZE });
    expect(await h.manager.download()).toMatchObject({ status: "error", code: "insufficient_disk_space", retryable: false });
    expect(h.engine.downloads).toBe(0);
  });

  it("downloads when free space cannot be measured, leaving the engine to fail on a real shortage", async () => {
    const h = await available({ freeDiskBytes: async () => Promise.reject(new Error("statfs failed")) });
    expect((await h.manager.download()).status).toBe("downloaded");
  });

  it.each([
    ["a checksum mismatch", new Error("sha512 checksum mismatch, expected abc, got def"), "verification_failed"],
    ["an invalid publisher signature", new Error("New version 0.2.0 is not signed by the application owner"), "verification_failed"],
    ["a full disk", Object.assign(new Error("write failed"), { code: "ENOSPC" }), "insufficient_disk_space"],
    ["a dropped connection", Object.assign(new Error("read ECONNRESET"), { code: "ECONNRESET" }), "network"],
    ["a server error", new Error("Cannot download: 503 Service Unavailable"), "feed_unavailable"],
    ["something unexpected", new Error("boom"), "download_failed"],
  ])("turns %s into the fixed error %s", async (_name, error, code) => {
    const h = await available();
    h.engine.onDownload = () => Promise.reject(error);
    const s = await h.manager.download();
    expect(s).toMatchObject({ status: "error", code });
    expect(JSON.stringify(s)).not.toMatch(/abc|def|0\.2\.0 is not signed|ECONNRESET|boom/);
    expect(canInstallNow(h.manager.getState())).toBe(false); // a failed download can never be installed
  });

  it("a corrupted or unverified artifact leaves nothing installable and can be retried", async () => {
    const h = await available();
    h.engine.onDownload = () => Promise.reject(new Error("sha512 checksum mismatch"));
    await h.manager.download();
    expect(await h.manager.installAndRestart()).toMatchObject({ status: "error", code: "verification_failed" });
    expect(h.engine.installs).toBe(0);
    h.engine.onDownload = () => h.engine.emit({ type: "update-downloaded", version: "0.2.0" });
    await h.clock.advance(UPDATE_TIMINGS.manualMinIntervalMs + 1);
    await h.manager.check("manual");
    expect((await h.manager.download()).status).toBe("downloaded");
  });

  it("a download that returns without the downloaded event is a failure, not a success", async () => {
    const h = await available();
    h.engine.onDownload = () => undefined;
    expect(await h.manager.download()).toMatchObject({ status: "error", code: "download_failed" });
  });

  it("an interrupted download (stalled connection) is cancelled and reported", async () => {
    const h = await available();
    h.engine.onDownload = () => new Promise<void>(() => undefined);
    const pending = h.manager.download();
    await settle();
    await h.clock.advance(UPDATE_TIMINGS.downloadStallMs + 1);
    await pending.catch(() => undefined);
    expect(h.engine.cancelled).toBe(1);
    expect(h.manager.getState()).toMatchObject({ status: "error", code: "timeout", retryable: true });
  });

  it("progress keeps a download alive past the stall limit", async () => {
    const h = await available();
    let finish: () => void = () => undefined;
    h.engine.onDownload = () => new Promise<void>((resolve) => (finish = resolve));
    const pending = h.manager.download();
    await settle();
    await h.clock.advance(UPDATE_TIMINGS.downloadStallMs - 1000);
    h.engine.emit({ type: "download-progress", percent: 20 });
    await h.clock.advance(UPDATE_TIMINGS.downloadStallMs - 1000);
    expect(h.engine.cancelled).toBe(0);
    h.engine.emit({ type: "update-downloaded", version: "0.2.0" });
    finish();
    await pending;
    expect(h.manager.getState().status).toBe("downloaded");
  });

  it("a second download request while one runs does nothing", async () => {
    const h = await available();
    let finish: () => void = () => undefined;
    h.engine.onDownload = () => new Promise<void>((resolve) => (finish = resolve));
    const first = h.manager.download();
    await settle();
    await h.manager.download();
    await h.manager.download();
    expect(h.engine.downloads).toBe(1);
    h.engine.emit({ type: "update-downloaded", version: "0.2.0" });
    finish();
    await first;
  });
});

describe("installing", () => {
  async function downloaded(over: Parameters<typeof harness>[0] = {}): Promise<Harness> {
    const h = harness(over);
    h.manager.start();
    await h.manager.check("manual");
    await h.manager.download();
    expect(h.manager.getState().status).toBe("downloaded");
    return h;
  }

  it("is possible only after a download, and stops everything first", async () => {
    const idle = harness();
    idle.manager.start();
    await idle.manager.installAndRestart();
    expect(idle.engine.installs).toBe(0);
    expect(idle.prepare).not.toHaveBeenCalled();

    const h = await downloaded();
    const order: string[] = [];
    h.prepare.mockImplementation(async () => void order.push("prepare"));
    h.engine.onInstall = () => order.push("install");
    expect((await h.manager.installAndRestart()).status).toBe("installing");
    expect(order).toEqual(["prepare", "install"]);
  });

  it("does not install twice for a double click", async () => {
    const h = await downloaded();
    await h.manager.installAndRestart();
    await h.manager.installAndRestart();
    expect(h.engine.installs).toBe(1);
  });

  it("reports a failed restart and allows a retry without downloading again", async () => {
    const h = await downloaded();
    await h.manager.installAndRestart();
    await h.clock.advance(UPDATE_TIMINGS.installWatchdogMs + 1);
    expect(h.manager.getState()).toMatchObject({ status: "error", code: "install_failed", retryable: false });
    expect(canInstallNow(h.manager.getState())).toBe(true);
    expect((await h.manager.installAndRestart()).status).toBe("installing");
    expect(h.engine.installs).toBe(2);
    expect(h.engine.downloads).toBe(1);
  });

  it("reports an installer launch failure", async () => {
    const h = await downloaded();
    h.engine.onInstall = () => {
      throw new Error("spawn EACCES");
    };
    const s = await h.manager.installAndRestart();
    expect(s).toMatchObject({ status: "error", code: "install_failed" });
    expect(JSON.stringify(s)).not.toMatch(/EACCES/);
  });

  it("does not install when stopping capture failed", async () => {
    const h = await downloaded();
    h.prepare.mockRejectedValue(new Error("cannot stop"));
    expect(await h.manager.installAndRestart()).toMatchObject({ status: "error", code: "install_failed" });
    expect(h.engine.installs).toBe(0);
  });

  it("a successful install never leaves the 'installing' state by itself (the app quits)", async () => {
    const h = await downloaded();
    h.engine.onInstall = () => undefined;
    await h.manager.installAndRestart();
    h.manager.dispose(); // the process is exiting: timers are cancelled, no false 'failed' is reported
    await h.clock.advance(UPDATE_TIMINGS.installWatchdogMs * 3);
    expect(h.manager.getState().status).toBe("installing");
  });
});

describe("notifications", () => {
  it("emits each state once, in order, with no contradictions", async () => {
    const h = harness();
    h.manager.start();
    await h.manager.check("manual");
    await h.manager.download();
    await h.manager.installAndRestart();
    expect(statuses(h)).toEqual(["checking", "available", "downloading", "downloading", "downloaded", "installing"]);
  });

  it("emits nothing for a repeated identical state", async () => {
    const h = harness();
    h.manager.start();
    await h.manager.check("manual");
    const n = h.states.length;
    h.engine.emit({ type: "update-available", version: "0.2.0" });
    expect(h.states).toHaveLength(n);
  });

  it("dispose stops listening and scheduling", async () => {
    const h = harness();
    h.manager.start();
    h.manager.dispose();
    expect(h.engine.listener).toBeNull();
    await h.clock.advance(UPDATE_TIMINGS.periodicMs * 2);
    expect(h.fetchManifest).not.toHaveBeenCalled();
  });
});

describe("error classification", () => {
  it.each([
    ["ENOSPC", "insufficient_disk_space"],
    ["no space left on device", "insufficient_disk_space"],
    ["sha512 checksum mismatch", "verification_failed"],
    ["not signed by the application owner", "verification_failed"],
    ["publisher name mismatch", "verification_failed"],
    ["getaddrinfo ENOTFOUND host", "network"],
    ["net::ERR_INTERNET_DISCONNECTED", "network"],
    ["ETIMEDOUT", "timeout"],
    ["request timed out", "timeout"],
    ["HTTP 404 not found", "feed_unavailable"],
    ["HTTP 429", "feed_unavailable"],
    ["HTTP 503", "feed_unavailable"],
    ["something else entirely", "unknown"],
  ])("%s -> %s", (message, code) => {
    expect(classifyEngineError(new Error(message))).toBe(code);
  });

  it("copes with odd throwables", () => {
    expect(classifyEngineError(undefined)).toBe("unknown");
    expect(classifyEngineError(null)).toBe("unknown");
    expect(classifyEngineError("ENOSPC")).toBe("unknown");
    expect(classifyEngineError({ code: "ENOSPC" })).toBe("insufficient_disk_space");
  });
});

// ---- SEC-09: the manifest is authenticated and the installer is bound to it ---------------------

describe("manifest authentication and rollback protection (SEC-09)", () => {
  const OTHER = generateManifestKeyPair();
  // The manager listens to the engine only after start() (the first check itself is delayed).
  const started = (over: Parameters<typeof harness>[0] = {}): Harness => {
    const h = harness(over);
    h.manager.start();
    return h;
  };

  it("fetches the detached signature next to the manifest", async () => {
    const h = started();
    await h.manager.check("manual");
    expect(h.fetchSignature.mock.calls[0]?.[0]).toBe(`${FEED}latest.yml.sig`);
    expect(h.manager.getState()).toMatchObject({ status: "available", version: "0.2.0" });
  });

  it("an installer with no manifest key built in never updates (fail closed)", async () => {
    const h = started({ readManifestKey: () => null });
    expect(h.manager.getState()).toEqual({ status: "disabled", reason: "not_configured" });
    await h.manager.check("manual");
    expect(h.fetchManifest).not.toHaveBeenCalled();
    expect(h.fetchSignature).not.toHaveBeenCalled();
  });

  it("a malformed or non-Ed25519 manifest key disables updates", () => {
    expect(harness({ readManifestKey: () => "not a key" }).manager.getState()).toEqual({
      status: "disabled",
      reason: "not_configured",
    });
    expect(harness({ readManifestKey: () => "" }).manager.getState().status).toBe("disabled");
  });

  it("a tampered manifest (version raised after signing) is refused before the engine is asked", async () => {
    const h = started();
    h.fetchSignature.mockImplementation(async () => signManifest(manifest("0.2.0"), KEYS.privateKeyPem));
    h.fetchManifest.mockResolvedValue(manifest("9.9.9")); // what an attacker serves
    const state = await h.manager.check("manual");
    expect(state).toMatchObject({ status: "error", code: "verification_failed", retryable: false });
    expect(h.engine.checks).toBe(0);
    expect(statuses(h)).not.toContain("available");
  });

  it("a tampered installer hash inside the manifest is refused", async () => {
    const h = started();
    h.fetchSignature.mockImplementation(async () => signManifest(manifest(), KEYS.privateKeyPem));
    h.fetchManifest.mockResolvedValue(manifest("0.2.0", { sha512: createHash("sha512").update("evil").digest("base64") }));
    expect(await h.manager.check("manual")).toMatchObject({ status: "error", code: "verification_failed" });
    expect(h.engine.checks).toBe(0);
  });

  it("a signature made with a different key is refused", async () => {
    const h = started();
    h.fetchSignature.mockImplementation(async () => signManifest(manifest(), OTHER.privateKeyPem));
    expect(await h.manager.check("manual")).toMatchObject({ status: "error", code: "verification_failed" });
    expect(h.engine.checks).toBe(0);
  });

  it("a valid signature for a different manifest (swapped signature) is refused", async () => {
    const h = started();
    h.fetchSignature.mockImplementation(async () => signManifest(manifest("0.1.5"), KEYS.privateKeyPem));
    expect(await h.manager.check("manual")).toMatchObject({ status: "error", code: "verification_failed" });
  });

  it.each(["", "garbage", "ed25519:", "A".repeat(86) + "=="])(
    "a malformed signature %j is refused",
    async (signature) => {
      const h = started();
      h.fetchSignature.mockResolvedValue(signature);
      expect(await h.manager.check("manual")).toMatchObject({ status: "error", code: "verification_failed" });
      expect(h.engine.checks).toBe(0);
    },
  );

  it("a missing signature file (404) blocks the update instead of skipping the check", async () => {
    const h = started();
    h.fetchSignature.mockRejectedValue(new UpdateFetchError("status", 404));
    const state = await h.manager.check("manual");
    expect(state).toMatchObject({ status: "error", code: "feed_unavailable" });
    expect(h.engine.checks).toBe(0);
  });

  it("replaying an OLDER validly signed manifest never offers a downgrade", async () => {
    const h = started(); // running 0.1.0
    h.fetchManifest.mockResolvedValue(manifest("0.0.9"));
    const state = await h.manager.check("manual");
    expect(state.status).toBe("none");
    expect(h.engine.checks).toBe(0);
  });

  it("replaying the CURRENT version's signed manifest is not an update", async () => {
    const h = started();
    h.fetchManifest.mockResolvedValue(manifest("0.1.0"));
    expect((await h.manager.check("manual")).status).toBe("none");
    expect(h.engine.checks).toBe(0);
  });

  it("rollback: an older installer served under the signed new version is refused and never installable", async () => {
    const h = started();
    await h.manager.check("manual");
    // The engine (fed by a hostile feed) delivers a file whose hash is not the signed one.
    h.hashFile.mockResolvedValue(createHash("sha512").update("older signed installer").digest("base64"));
    const state = await h.manager.download();
    expect(state).toMatchObject({ status: "error", code: "verification_failed" });
    expect(h.hashFile).toHaveBeenCalledWith(DOWNLOADED_PATH);
    expect(statuses(h)).not.toContain("downloaded");
    expect(canInstallNow(h.manager.getState())).toBe(false);
    await h.manager.installAndRestart();
    expect(h.engine.installs).toBe(0);
    expect(h.prepare).not.toHaveBeenCalled();
  });

  it("a downloaded event that names no file is refused", async () => {
    const h = started();
    await h.manager.check("manual");
    h.engine.onDownload = () => h.engine.emit({ type: "update-downloaded", version: "0.2.0", downloadedFile: undefined } as never);
    const state = await h.manager.download();
    expect(state).toMatchObject({ status: "error", code: "verification_failed" });
    expect(h.hashFile).not.toHaveBeenCalled();
  });

  it("an installer that cannot be read for hashing is refused", async () => {
    const h = started();
    await h.manager.check("manual");
    h.hashFile.mockRejectedValue(new Error("EBUSY"));
    expect(await h.manager.download()).toMatchObject({ status: "error", code: "verification_failed" });
    expect(h.manager.getState().status).toBe("error");
  });

  it("the legitimate path still works: signed manifest, matching installer hash, install allowed", async () => {
    const h = started();
    await h.manager.check("manual");
    expect(await h.manager.download()).toEqual({ status: "downloaded", version: "0.2.0" });
    expect(h.hashFile).toHaveBeenCalledWith(DOWNLOADED_PATH);
    expect(await h.manager.installAndRestart()).toEqual({ status: "installing", version: "0.2.0" });
    expect(h.engine.installs).toBe(1);
  });

  it("recovery from a bad release is a NEWER signed version, never a downgrade", async () => {
    const h = started();
    h.fetchManifest.mockResolvedValue(manifest("0.2.1"));
    h.engine.onCheck = () => h.engine.emit({ type: "update-available", version: "0.2.1" });
    h.engine.onDownload = () => h.engine.emit({ type: "update-downloaded", version: "0.2.1" });
    await h.manager.check("manual");
    expect(await h.manager.download()).toEqual({ status: "downloaded", version: "0.2.1" });
  });
});

// ---- SEC-09 with REAL files and the REAL hash function (no fake hasher) -------------------------

describe("the installer is bound to the signed manifest with real file bytes (SEC-09)", () => {
  const started = (over: Partial<UpdateDeps> & { config?: string | null } = {}): Harness => {
    const h = harness(over);
    h.manager.start();
    return h;
  };

  function withInstaller(bytes: Buffer, run: (file: string, digest: string) => Promise<void>): Promise<void> {
    const dir = mkdtempSync(join(tmpdir(), "ia-installer-"));
    const file = join(dir, "installer.exe");
    writeFileSync(file, bytes);
    const digest = createHash("sha512").update(bytes).digest("base64");
    return run(file, digest).finally(() => rmSync(dir, { recursive: true, force: true }));
  }

  const genuine = Buffer.from("MZ genuine signed installer ".repeat(5000));

  it("the real hasher reproduces the manifest's SHA-512, and one flipped byte breaks it", async () => {
    await withInstaller(genuine, async (file, digest) => {
      expect(await sha512Base64OfFile(file)).toBe(digest);
      const tampered = Buffer.from(genuine);
      tampered[tampered.length - 1] = tampered[tampered.length - 1]! ^ 1;
      writeFileSync(file, tampered);
      expect(await sha512Base64OfFile(file)).not.toBe(digest);
    });
  });

  it("a genuine downloaded installer is accepted and the update can be installed", async () => {
    await withInstaller(genuine, async (file, digest) => {
      const h = started({ hashFile: sha512Base64OfFile });
      h.fetchManifest.mockResolvedValue(manifest("0.2.0", { sha512: digest }));
      h.engine.onDownload = () => h.engine.emit({ type: "update-downloaded", version: "0.2.0", downloadedFile: file });
      await h.manager.check("manual");
      expect(await h.manager.download()).toEqual({ status: "downloaded", version: "0.2.0" });
      expect(canInstallNow(h.manager.getState())).toBe(true);
    });
  });

  it("an installer changed by ONE byte after signing is refused and cannot be installed", async () => {
    await withInstaller(genuine, async (file, digest) => {
      const tampered = Buffer.from(genuine);
      tampered[1000] = tampered[1000]! ^ 0xff;
      writeFileSync(file, tampered);
      const h = started({ hashFile: sha512Base64OfFile });
      h.fetchManifest.mockResolvedValue(manifest("0.2.0", { sha512: digest }));
      h.engine.onDownload = () => h.engine.emit({ type: "update-downloaded", version: "0.2.0", downloadedFile: file });
      await h.manager.check("manual");
      expect(await h.manager.download()).toMatchObject({ status: "error", code: "verification_failed" });
      expect(canInstallNow(h.manager.getState())).toBe(false);
      await h.manager.installAndRestart();
      expect(h.engine.installs).toBe(0);
    });
  });

  it("an OLDER genuine installer served under the newer signed version is refused (downgrade by substitution)", async () => {
    const older = Buffer.from("MZ older, vulnerable but validly signed installer ".repeat(5000));
    await withInstaller(older, async (file) => {
      const newerDigest = createHash("sha512").update(genuine).digest("base64"); // what the signed manifest names
      const h = started({ hashFile: sha512Base64OfFile });
      h.fetchManifest.mockResolvedValue(manifest("0.2.0", { sha512: newerDigest }));
      h.engine.onDownload = () => h.engine.emit({ type: "update-downloaded", version: "0.2.0", downloadedFile: file });
      await h.manager.check("manual");
      expect(await h.manager.download()).toMatchObject({ status: "error", code: "verification_failed" });
      expect(h.engine.installs).toBe(0);
    });
  });

  it("a missing or unreadable downloaded file is refused", async () => {
    const h = started({ hashFile: sha512Base64OfFile });
    h.engine.onDownload = () => h.engine.emit({ type: "update-downloaded", version: "0.2.0", downloadedFile: join(tmpdir(), "does-not-exist-installer.exe") });
    await h.manager.check("manual");
    expect(await h.manager.download()).toMatchObject({ status: "error", code: "verification_failed" });
  });
});
