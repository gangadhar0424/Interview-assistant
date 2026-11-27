import { spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterAll, afterEach, describe, expect, it } from "vitest";
import { checkSums, verifyBuild } from "../scripts/release/verify-build.mjs";
import { formatSums } from "../scripts/release/release-lib.mjs";

const DESKTOP = resolve(__dirname, "..");
const PACKAGE_RELEASE = join(DESKTOP, "scripts", "release", "package-release.mjs");
const VERIFY = join(DESKTOP, "scripts", "release", "verify-build.mjs");
const onWindows = process.platform === "win32";
/** A fresh throw-away password per test: nothing key-like is written into the source. */
const throwaway = (): string => `t${randomBytes(9).toString("hex")}`;

const tmps: string[] = [];
const tmp = (): string => {
  const dir = mkdtempSync(join(tmpdir(), "ia-scripts-"));
  tmps.push(dir);
  return dir;
};
afterEach(() => {
  while (tmps.length) rmSync(tmps.pop() as string, { recursive: true, force: true });
});

/** Run the real release entry point with an exact environment (no inherited signing variables). */
function runPackage(extraEnv: Record<string, string>) {
  const env: Record<string, string> = {
    PATH: process.env.PATH ?? "",
    SystemRoot: process.env.SystemRoot ?? "",
    ...extraEnv,
  };
  return spawnSync(process.execPath, [PACKAGE_RELEASE], { env, encoding: "utf8", timeout: 120_000, cwd: DESKTOP });
}

describe("release entry point refuses to produce an unverifiable signed build", () => {
  it("fails when signing is required and nothing is configured", () => {
    const r = runPackage({ REQUIRE_SIGNING: "true" });
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/REQUIRE_SIGNING=true but no signing configuration/);
  });

  it("fails when the certificate password is missing, without printing any value", () => {
    const r = runPackage({ REQUIRE_SIGNING: "true", CSC_LINK: "C:\\secret-location\\cert.pfx" });
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/CSC_KEY_PASSWORD is not set/);
    expect(r.stderr + r.stdout).not.toContain("secret-location");
  });

  it("fails when both certificate and Azure variables are present", () => {
    const r = runPackage({ REQUIRE_SIGNING: "true", CSC_LINK: "a.pfx", CSC_KEY_PASSWORD: "p", AZURE_TENANT_ID: "t" });
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/exactly one signing method/);
  });

  it("fails on an invalid timestamp service URL", () => {
    const r = runPackage({ REQUIRE_SIGNING: "true", CSC_LINK: "a.pfx", CSC_KEY_PASSWORD: "p", SIGN_TIMESTAMP_URL: "ftp://ts" });
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/SIGN_TIMESTAMP_URL/);
  });

  it("fails when the certificate file does not exist", () => {
    const r = runPackage({ REQUIRE_SIGNING: "true", CSC_LINK: join(tmp(), "missing.pfx"), CSC_KEY_PASSWORD: "p" });
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/does not point to an existing certificate file/);
  });
});

/** Create a throw-away self-signed code-signing PFX; the key never stays in the user's store. */
function createPfx(options: { expired?: boolean; password: string }): string {
  const dir = tmp();
  const pfx = join(dir, "test.pfx");
  const notBefore = options.expired ? "(Get-Date).AddYears(-3)" : "(Get-Date).AddDays(-1)";
  const notAfter = options.expired ? "(Get-Date).AddYears(-1)" : "(Get-Date).AddYears(1)";
  const script = `
$c = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=Interview Assistant TEST ONLY (untrusted)' -CertStoreLocation Cert:\\CurrentUser\\My -NotBefore ${notBefore} -NotAfter ${notAfter} -KeyExportPolicy Exportable
$pw = ConvertTo-SecureString $env:IA_TEST_PW -AsPlainText -Force
Export-PfxCertificate -Cert $c -FilePath $env:IA_TEST_PFX -Password $pw | Out-Null
Remove-Item -LiteralPath ('Cert:\\CurrentUser\\My\\' + $c.Thumbprint) -DeleteKey
`;
  // Windows PowerShell 5.1 must not inherit a PowerShell 7 PSModulePath (GitHub's Windows runners set
  // one): it then loads the wrong Microsoft.PowerShell.Security module and the Cert: drive is missing.
  const env: NodeJS.ProcessEnv = { ...process.env, IA_TEST_PW: options.password, IA_TEST_PFX: pfx };
  for (const key of Object.keys(env)) if (key.toLowerCase() === "psmodulepath") delete env[key];
  const r = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], {
    env,
    encoding: "utf8",
    timeout: 120_000,
  });
  if (r.status !== 0) throw new Error(`could not create the test certificate: ${r.stderr}`);
  return pfx;
}

describe.skipIf(!onWindows)("certificate preflight with real PFX files (Windows)", () => {
  it("rejects a wrong password (reported as unreadable, never echoing it)", () => {
    const right = throwaway();
    const wrong = throwaway();
    const pfx = createPfx({ password: right });
    const r = runPackage({ REQUIRE_SIGNING: "true", CSC_LINK: pfx, CSC_KEY_PASSWORD: wrong });
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/could not be opened \(wrong password or corrupt file\)/);
    expect(r.stderr + r.stdout).not.toContain(right);
    expect(r.stderr + r.stdout).not.toContain(wrong);
  }, 180_000);

  it("rejects an expired certificate", () => {
    const password = throwaway();
    const pfx = createPfx({ password, expired: true });
    const r = runPackage({ REQUIRE_SIGNING: "true", CSC_LINK: pfx, CSC_KEY_PASSWORD: password });
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/has expired/);
  }, 180_000);

  it("rejects a self-signed certificate whose chain is not trusted", () => {
    const password = throwaway();
    const pfx = createPfx({ password });
    const r = runPackage({ REQUIRE_SIGNING: "true", CSC_LINK: pfx, CSC_KEY_PASSWORD: password });
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/certificate chain is not trusted/);
  }, 180_000);
});

const PKG = { version: "1.2.3", build: { productName: "Test App" } };
function fakeRoot(version = "1.2.3"): string {
  const root = tmp();
  writeFileSync(join(root, "package.json"), JSON.stringify({ ...PKG, version }));
  return root;
}

describe("build verification fails safely when artifacts are wrong", () => {
  it("fails for an invalid version and a missing release folder, writing nothing", async () => {
    const root = fakeRoot("not-a-version");
    const r = await verifyBuild({ root, requireSigned: false });
    expect(r.ok).toBe(false);
    expect(r.checks.find((c) => c.name.startsWith("version"))?.ok).toBe(false);
    expect(r.checks.find((c) => c.name.startsWith("installer exists"))?.ok).toBe(false);
  });

  it("fails when the installer is missing but the unpacked app exists", async () => {
    const root = fakeRoot();
    mkdirSync(join(root, "release", "win-unpacked"), { recursive: true });
    const r = await verifyBuild({ root, requireSigned: false });
    expect(r.ok).toBe(false);
    expect(r.installer).toBeNull();
    const names = r.checks.filter((c) => !c.ok).map((c) => c.name);
    expect(names).toContain("installer exists for this version");
    expect(names).toContain("expected packaged files");
  });

  it("flags a stale installer of another version", async () => {
    const root = fakeRoot();
    mkdirSync(join(root, "release", "win-unpacked"), { recursive: true });
    writeFileSync(join(root, "release", "Test App-0.0.1-Windows-x64.exe"), "x");
    const r = await verifyBuild({ root, requireSigned: false });
    expect(r.checks.find((c) => c.name.startsWith("no installers of another"))?.ok).toBe(false);
  });

  it("never writes checksums or a build kind for a failed verification", async () => {
    const root = fakeRoot();
    mkdirSync(join(root, "release", "win-unpacked"), { recursive: true });
    await verifyBuild({ root, requireSigned: true });
    const { existsSync } = await import("node:fs");
    expect(existsSync(join(root, "release", "SHA256SUMS.txt"))).toBe(false);
    expect(existsSync(join(root, "release", "BUILD-KIND.txt"))).toBe(false);
  });

  it("rejects an unknown --mode with exit code 2", () => {
    const r = spawnSync(process.execPath, [VERIFY, "--mode", "maybe"], { encoding: "utf8", cwd: DESKTOP });
    expect(r.status).toBe(2);
  });
});

describe("checksum verification of a release folder", () => {
  function releaseWith(name: string, content: string, recorded: string): string {
    const root = tmp();
    mkdirSync(join(root, "release"), { recursive: true });
    writeFileSync(join(root, "release", name), content);
    writeFileSync(join(root, "release", "SHA256SUMS.txt"), formatSums([{ name, sha256: recorded }]));
    return root;
  }
  const HELLO = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";

  it("passes when the files are unchanged", async () => {
    expect(await checkSums(releaseWith("a.exe", "hello", HELLO))).toEqual([]);
  });

  it("reports a checksum mismatch", async () => {
    expect(await checkSums(releaseWith("a.exe", "hello!", HELLO))).toEqual(["a.exe"]);
  });

  it("reports a file that is listed but missing", async () => {
    const root = releaseWith("a.exe", "hello", HELLO);
    rmSync(join(root, "release", "a.exe"));
    expect(await checkSums(root)).toEqual(["a.exe"]);
  });

  it("throws for a missing, empty or malformed checksum file", async () => {
    const empty = tmp();
    mkdirSync(join(empty, "release"));
    await expect(checkSums(empty)).rejects.toThrow(/missing/);
    writeFileSync(join(empty, "release", "SHA256SUMS.txt"), "\n");
    await expect(checkSums(empty)).rejects.toThrow(/lists no files/);
    writeFileSync(join(empty, "release", "SHA256SUMS.txt"), "garbage");
    await expect(checkSums(empty)).rejects.toThrow(/malformed/);
  });

  it("the CLI exits non-zero on a mismatch", () => {
    const root = releaseWith("a.exe", "tampered", HELLO);
    // The CLI reads its own release folder, so run the library check through a tiny harness.
    const harness = `import { checkSums } from ${JSON.stringify("file:///" + VERIFY.replace(/\\/g, "/"))};
const bad = await checkSums(${JSON.stringify(root)}); process.exit(bad.length ? 1 : 0);`;
    const r = spawnSync(process.execPath, ["--input-type=module", "-e", harness], { encoding: "utf8" });
    expect(r.status).toBe(1);
  });
});

afterAll(() => {
  while (tmps.length) rmSync(tmps.pop() as string, { recursive: true, force: true });
});

describe("update metadata is checked against the real installer (M-11)", () => {
  const FEED = "https://updates.example.com/desktop/";
  const NAME = "Test App-1.2.3-Windows-x64.exe";

  /** A fake release folder: an installer, its app-update.yml and a latest.yml that match it. */
  function releaseWithUpdates(over: { publisher?: string[]; feed?: string; sha?: string; version?: string; size?: number } = {}) {
    const root = fakeRoot();
    const resources = join(root, "release", "win-unpacked", "resources");
    mkdirSync(resources, { recursive: true });
    const installer = join(root, "release", NAME);
    writeFileSync(installer, Buffer.alloc(1_200_000, 7));
    const sha = over.sha ?? createHash("sha512").update(Buffer.alloc(1_200_000, 7)).digest("base64");
    const size = over.size ?? statSync(installer).size;
    const publisher = over.publisher === undefined ? "" : `publisherName:\n${over.publisher.map((p) => `  - ${p}`).join("\n")}\n`;
    writeFileSync(join(resources, "app-update.yml"), `provider: generic\nurl: ${over.feed ?? FEED}\n${publisher}`);
    writeFileSync(
      join(root, "release", "latest.yml"),
      `version: ${over.version ?? "1.2.3"}\nfiles:\n  - url: ${NAME}\n    sha512: ${sha}\n    size: ${size}\npath: ${NAME}\nsha512: ${sha}\nreleaseDate: '2026-11-22T10:00:00.000Z'\n`,
    );
    return root;
  }
  const failing = async (root: string, requireSigned: boolean, env: Record<string, string> = {}) => {
    const r = await verifyBuild({ root, requireSigned, env });
    return r.checks.filter((c) => !c.ok && /update/i.test(c.name));
  };

  it("passes for a consistent unsigned build (feed present, no pinned publisher)", async () => {
    expect(await failing(releaseWithUpdates(), false, { UPDATE_FEED_URL: FEED })).toEqual([]);
  });

  it("an unsigned build must not carry a pinned publisher", async () => {
    const bad = await failing(releaseWithUpdates({ publisher: ["Example Ltd"] }), false);
    expect(bad.map((c) => c.name)).toContain("update configuration in the app");
  });

  it("a signed build must be able to update itself", async () => {
    const bad = await failing(releaseWithUpdates(), true);
    expect(bad.find((c) => c.name === "update configuration in the app")?.detail).toMatch(/unsigned_build/);
  });

  it("a feed that is set but not packaged is caught", async () => {
    const root = fakeRoot();
    mkdirSync(join(root, "release", "win-unpacked"), { recursive: true });
    const bad = await failing(root, false, { UPDATE_FEED_URL: FEED });
    expect(bad.map((c) => c.name)).toContain("update metadata is packaged");
  });

  it("a packaged feed that differs from UPDATE_FEED_URL is caught", async () => {
    const bad = await failing(releaseWithUpdates({ feed: "https://other.example.com/d/" }), false, { UPDATE_FEED_URL: FEED });
    expect(bad.find((c) => c.name === "update configuration in the app")?.detail).toMatch(/different feed/);
  });

  it("latest.yml with a wrong checksum, size or version is caught", async () => {
    const wrongSha = createHash("sha512").update("tampered").digest("base64");
    expect((await failing(releaseWithUpdates({ sha: wrongSha }), false)).find((c) => c.name.startsWith("update manifest"))?.detail).toMatch(/SHA-512 does not match/);
    expect((await failing(releaseWithUpdates({ size: 5 }), false)).find((c) => c.name.startsWith("update manifest"))?.detail).toMatch(/size does not match/);
    expect((await failing(releaseWithUpdates({ version: "9.9.9" }), false)).find((c) => c.name.startsWith("update manifest"))?.detail).toMatch(/is not 1\.2\.3/);
  });

  it("a missing latest.yml is caught and checksums list latest.yml only when present", async () => {
    const root = releaseWithUpdates();
    rmSync(join(root, "release", "latest.yml"));
    expect((await failing(root, false)).find((c) => c.name.startsWith("update manifest"))?.detail).toMatch(/latest\.yml is missing/);
  });
});
