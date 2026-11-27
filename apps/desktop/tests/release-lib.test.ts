import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  builderConfig,
  cleanReleaseDir,
  commonNameOf,
  resolveFeedUrl,
  resolvePublisherName,
  compareSums,
  evaluateCertificate,
  evaluateFuses,
  evaluateSignature,
  expectedInstallerName,
  fileVersionMatches,
  formatSums,
  interruptHandler,
  isValidSemver,
  missingFiles,
  parseSums,
  peMachine,
  preflight,
  REQUIRED_FUSES,
  resolveSigningMode,
  resolveTimestampUrl,
  sha256File,
  signingRequired,
} from "../scripts/release/release-lib.mjs";

const NOW = new Date("2026-11-22T00:00:00Z");
const GOOD_CERT = {
  notBefore: "2025-01-01T00:00:00Z",
  notAfter: "2027-01-01T00:00:00Z",
  hasPrivateKey: true,
  codeSigningEku: true,
  chainValid: true,
};
const tmps: string[] = [];
const tmp = (): string => {
  const dir = mkdtempSync(join(tmpdir(), "ia-rel-"));
  tmps.push(dir);
  return dir;
};
afterEach(() => {
  while (tmps.length) rmSync(tmps.pop() as string, { recursive: true, force: true });
});

describe("version metadata", () => {
  it("accepts semantic versions and rejects everything else", () => {
    for (const v of ["0.1.0", "1.2.3", "10.20.30", "1.0.0-beta.1", "2.0.0-rc.1"]) expect(isValidSemver(v)).toBe(true);
    for (const v of ["", "1", "1.2", "01.2.3", "1.2.3.4", "v1.2.3", "1.2.x", "latest", "1.2.3-", " 1.2.3"]) {
      expect(isValidSemver(v)).toBe(false);
    }
    expect(isValidSemver(undefined as unknown as string)).toBe(false);
    expect(isValidSemver(123 as unknown as string)).toBe(false);
  });

  it("matches the executable FileVersion to the package version", () => {
    expect(fileVersionMatches("0.1.0", "0.1.0")).toBe(true);
    expect(fileVersionMatches("0.1.0.0", "0.1.0")).toBe(true);
    expect(fileVersionMatches("0.1.1", "0.1.0")).toBe(false);
    expect(fileVersionMatches("0.1", "0.1.0")).toBe(false);
    expect(fileVersionMatches(null, "0.1.0")).toBe(false);
  });

  it("derives the installer name from the product and version", () => {
    expect(expectedInstallerName("Interview Assistant", "1.2.3")).toBe("Interview Assistant-1.2.3-Windows-x64.exe");
  });
});

describe("signing configuration and preflight", () => {
  it("enforces signing only for the exact string true", () => {
    expect(signingRequired({ REQUIRE_SIGNING: "true" })).toBe(true);
    for (const v of ["TRUE", "1", "yes", "false", "", undefined]) expect(signingRequired({ REQUIRE_SIGNING: v })).toBe(false);
  });

  it("identifies a complete certificate configuration", () => {
    expect(resolveSigningMode({ CSC_LINK: "x.pfx", CSC_KEY_PASSWORD: "p" })).toEqual({ mode: "pfx", problems: [] });
  });

  it("reports which certificate variable is missing, never its value", () => {
    const r = resolveSigningMode({ CSC_LINK: "secret-path.pfx" });
    expect(r.mode).toBe("pfx");
    expect(r.problems).toEqual(["CSC_KEY_PASSWORD is not set."]);
    expect(JSON.stringify(r)).not.toContain("secret-path");
    expect(resolveSigningMode({ CSC_KEY_PASSWORD: "hunter2" }).problems).toEqual(["CSC_LINK is not set."]);
    expect(resolveSigningMode({ CSC_LINK: "  ", CSC_KEY_PASSWORD: "" }).mode).toBe("none");
  });

  it("identifies a complete Azure configuration and lists what is missing", () => {
    const full = {
      AZURE_TRUSTED_SIGNING_ENDPOINT: "https://eus.codesigning.azure.net",
      AZURE_TRUSTED_SIGNING_ACCOUNT: "acct",
      AZURE_TRUSTED_SIGNING_PROFILE: "prof",
      AZURE_TENANT_ID: "t",
      AZURE_CLIENT_ID: "c",
      AZURE_CLIENT_SECRET: "s",
    };
    expect(resolveSigningMode(full)).toEqual({ mode: "azure", problems: [] });
    const { AZURE_CLIENT_SECRET: _omitted, ...partial } = full;
    expect(resolveSigningMode(partial).problems).toEqual(["AZURE_CLIENT_SECRET is not set."]);
  });

  it("refuses an ambiguous configuration", () => {
    const r = resolveSigningMode({ CSC_LINK: "a.pfx", CSC_KEY_PASSWORD: "p", AZURE_TENANT_ID: "t" });
    expect(r.mode).toBe("none");
    expect(r.problems[0]).toMatch(/exactly one/);
  });

  it("validates the timestamp service URL", () => {
    expect(resolveTimestampUrl({})).toBe("http://timestamp.digicert.com/");
    expect(resolveTimestampUrl({ SIGN_TIMESTAMP_URL: "https://ts.example.com/tsa" })).toBe("https://ts.example.com/tsa");
    for (const bad of ["not a url", "ftp://ts.example.com", "javascript:alert(1)", "https://user:pw@ts.example.com"]) {
      expect(() => resolveTimestampUrl({ SIGN_TIMESTAMP_URL: bad })).toThrow();
    }
  });

  it("a required signed build fails without any signing configuration", () => {
    const plan = preflight({ REQUIRE_SIGNING: "true" }, "1.0.0");
    expect(plan.ok).toBe(false);
    expect(plan.kind).toBe("signed");
    expect(plan.errors.join(" ")).toMatch(/REQUIRE_SIGNING=true/);
  });

  it("a required signed build fails with an incomplete configuration", () => {
    const plan = preflight({ REQUIRE_SIGNING: "true", CSC_LINK: "a.pfx" }, "1.0.0");
    expect(plan.ok).toBe(false);
    expect(plan.errors).toContain("CSC_KEY_PASSWORD is not set.");
  });

  it("a required signed build passes with a complete configuration", () => {
    const plan = preflight({ REQUIRE_SIGNING: "true", CSC_LINK: "a.pfx", CSC_KEY_PASSWORD: "p" }, "1.0.0");
    expect(plan).toMatchObject({ ok: true, kind: "signed", mode: "pfx", errors: [] });
  });

  it("an invalid version fails every kind of build", () => {
    expect(preflight({}, "1.0").ok).toBe(false);
    expect(preflight({ REQUIRE_SIGNING: "true", CSC_LINK: "a", CSC_KEY_PASSWORD: "p" }, "latest").ok).toBe(false);
  });

  it("an invalid timestamp URL fails the preflight", () => {
    expect(preflight({ SIGN_TIMESTAMP_URL: "ftp://x" }, "1.0.0").ok).toBe(false);
  });

  it("an unsigned build is labelled as internal only and passes", () => {
    const plan = preflight({}, "1.0.0");
    expect(plan).toMatchObject({ ok: true, kind: "unsigned-internal" });
    expect(plan.warnings.join(" ")).toMatch(/UNSIGNED internal build/);
  });

  it("warns when signing variables exist but are not enforced", () => {
    const plan = preflight({ CSC_LINK: "a.pfx", CSC_KEY_PASSWORD: "p" }, "1.0.0");
    expect(plan.warnings.join(" ")).toMatch(/not enforced/);
  });
});

describe("electron-builder configuration", () => {
  const base = { productName: "X", win: { target: "nsis", icon: "i.ico" } };

  it("forces code signing with SHA-256 and a timestamp for a certificate build", () => {
    const config = builderConfig(base, {}, "pfx");
    expect(config.win).toMatchObject({
      forceCodeSigning: true,
      signtoolOptions: { signingHashAlgorithms: ["sha256"], rfc3161TimeStampServer: "http://timestamp.digicert.com/" },
      icon: "i.ico",
    });
  });

  it("configures Azure signing from the environment without copying secrets into the config", () => {
    const env = {
      AZURE_TRUSTED_SIGNING_ENDPOINT: "https://eus.codesigning.azure.net",
      AZURE_TRUSTED_SIGNING_ACCOUNT: "acct",
      AZURE_TRUSTED_SIGNING_PROFILE: "prof",
      AZURE_CLIENT_SECRET: "never-in-config",
    };
    const config = builderConfig(base, env, "azure");
    expect(config.win).toMatchObject({
      forceCodeSigning: true,
      azureSignOptions: { codeSigningAccountName: "acct", certificateProfileName: "prof", fileDigest: "SHA256" },
    });
    expect(JSON.stringify(config)).not.toContain("never-in-config");
  });

  it("adds no signing settings for an unsigned build and does not mutate the input", () => {
    const config = builderConfig(base, {}, "none");
    expect(config.win).toEqual(base.win);
    expect(base.win).toEqual({ target: "nsis", icon: "i.ico" });
  });
});

describe("certificate and signature evaluation", () => {
  it("accepts a valid code-signing certificate", () => {
    expect(evaluateCertificate(GOOD_CERT, NOW).errors).toEqual([]);
  });

  it("rejects an expired certificate", () => {
    const r = evaluateCertificate({ ...GOOD_CERT, notAfter: "2026-01-01T00:00:00Z" }, NOW);
    expect(r.errors).toContain("The signing certificate has expired.");
  });

  it("rejects a certificate that is not yet valid", () => {
    expect(evaluateCertificate({ ...GOOD_CERT, notBefore: "2027-01-01T00:00:00Z", notAfter: "2028-01-01T00:00:00Z" }, NOW).errors).toContain(
      "The signing certificate is not yet valid.",
    );
  });

  it("warns when the certificate expires soon", () => {
    const r = evaluateCertificate({ ...GOOD_CERT, notAfter: "2026-12-10T00:00:00Z" }, NOW);
    expect(r.errors).toEqual([]);
    expect(r.warnings[0]).toMatch(/renew/);
  });

  it("rejects missing private key, wrong usage and an untrusted chain", () => {
    const r = evaluateCertificate({ ...GOOD_CERT, hasPrivateKey: false, codeSigningEku: false, chainValid: false, chainStatus: ["UntrustedRoot"] }, NOW);
    expect(r.errors).toEqual([
      "The certificate has no private key.",
      "The certificate is not valid for code signing.",
      "The certificate chain is not trusted (UntrustedRoot).",
    ]);
  });

  it("rejects unreadable dates", () => {
    expect(evaluateCertificate({ ...GOOD_CERT, notAfter: "garbage" }, NOW).errors[0]).toMatch(/could not be read/);
  });

  const good = { status: "Valid", hasTimestamp: true, digestAlgorithm: "sha256", signerNotAfter: "2027-01-01T00:00:00Z" };

  it("accepts a valid, timestamped SHA-256 signature for a signed build", () => {
    expect(evaluateSignature(good, { requireSigned: true, now: NOW }).errors).toEqual([]);
  });

  it("rejects every signed-build defect", () => {
    const opts = { requireSigned: true, now: NOW };
    expect(evaluateSignature({ ...good, status: "NotSigned" }, opts).errors[0]).toMatch(/NotSigned/);
    expect(evaluateSignature({ ...good, status: "UnknownError" }, opts).errors[0]).toMatch(/UnknownError/);
    expect(evaluateSignature({ ...good, status: "HashMismatch" }, opts).errors[0]).toMatch(/HashMismatch/);
    expect(evaluateSignature({ ...good, hasTimestamp: false }, opts).errors).toContain("The signature has no trusted timestamp.");
    expect(evaluateSignature({ ...good, digestAlgorithm: "sha1" }, opts).errors[0]).toMatch(/sha1/);
    expect(evaluateSignature({ ...good, digestAlgorithm: null }, opts).errors[0]).toMatch(/unknown/);
  });

  it("an unsigned internal build must really be unsigned", () => {
    expect(evaluateSignature({ status: "NotSigned", hasTimestamp: false }, { requireSigned: false, now: NOW }).errors).toEqual([]);
    expect(evaluateSignature(good, { requireSigned: false, now: NOW }).errors[0]).toMatch(/unsigned internal build/);
  });

  it("notes that an expired signer is covered by its timestamp", () => {
    const r = evaluateSignature({ ...good, signerNotAfter: "2026-01-01T00:00:00Z" }, { requireSigned: true, now: NOW });
    expect(r.errors).toEqual([]);
    expect(r.warnings[0]).toMatch(/timestamp/);
  });
});

describe("package integrity", () => {
  it("detects x64 and non-PE images", () => {
    const pe = Buffer.alloc(256);
    pe.write("MZ", 0, "latin1");
    pe.writeUInt32LE(0x80, 0x3c);
    pe.write("PE\0\0", 0x80, "latin1");
    pe.writeUInt16LE(0x8664, 0x84);
    expect(peMachine(pe)).toBe(0x8664);
    pe.writeUInt16LE(0x14c, 0x84);
    expect(peMachine(pe)).toBe(0x14c);
    expect(peMachine(Buffer.from("not an executable at all, just text".repeat(4)))).toBeNull();
    expect(peMachine(Buffer.alloc(10))).toBeNull();
    const truncated = Buffer.alloc(0x50);
    truncated.write("MZ", 0, "latin1");
    truncated.writeUInt32LE(0x4000, 0x3c);
    expect(peMachine(truncated)).toBeNull();
  });

  it("requires every fuse to be in its release state", () => {
    const ok = { ...REQUIRED_FUSES };
    expect(evaluateFuses(ok)).toEqual([]);
    expect(evaluateFuses({ ...ok, RunAsNode: true })[0]).toMatch(/RunAsNode/);
    expect(evaluateFuses({ ...ok, OnlyLoadAppFromAsar: false })[0]).toMatch(/OnlyLoadAppFromAsar/);
    expect(evaluateFuses({})).toHaveLength(Object.keys(REQUIRED_FUSES).length);
  });

  it("lists missing packaged files and ignores path separators", () => {
    expect(missingFiles(["Interview Assistant.exe", "resources\\app.asar", "resources.pak", "icudtl.dat"])).toEqual([]);
    expect(missingFiles(["Interview Assistant.exe"])).toEqual(["resources/app.asar", "resources.pak", "icudtl.dat"]);
  });

  it("hashes a file, round-trips the checksum file and detects tampering", async () => {
    const dir = tmp();
    const file = join(dir, "a.bin");
    writeFileSync(file, "hello");
    const sha = await sha256File(file);
    expect(sha).toBe("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
    const text = formatSums([{ name: "a.bin", sha256: sha }]);
    expect(parseSums(text)).toEqual([{ name: "a.bin", sha256: sha }]);
    expect(compareSums(parseSums(text), { "a.bin": sha })).toEqual([]);
    expect(compareSums(parseSums(text), { "a.bin": "0".repeat(64) })).toEqual(["a.bin"]);
    expect(compareSums(parseSums(text), {})).toEqual(["a.bin"]);
  });

  it("rejects a malformed checksum file", () => {
    expect(() => parseSums("not a checksum line")).toThrow(/malformed/);
    expect(() => parseSums("ABC  file")).toThrow(/malformed/);
    expect(parseSums("\n\n")).toEqual([]);
  });

  it("an unreadable file reports an error instead of a hash", async () => {
    await expect(sha256File(join(tmp(), "missing.bin"))).rejects.toThrow();
  });
});

describe("cleaning and interruption", () => {
  it("removes stale release output and tolerates a missing folder", () => {
    const root = tmp();
    cleanReleaseDir(root);
    mkdirSync(join(root, "release", "win-unpacked"), { recursive: true });
    writeFileSync(join(root, "release", "old.exe"), "x");
    cleanReleaseDir(root);
    expect(existsSync(join(root, "release", "old.exe"))).toBe(false);
    expect(existsSync(join(root, "release", "win-unpacked"))).toBe(false);
    expect(existsSync(join(root, "release"))).toBe(true);
  });

  it("stops the child and reports exit code 130 on interruption", () => {
    const kill = vi.fn();
    const log = vi.fn();
    expect(interruptHandler({ kill }, log)("SIGTERM")).toBe(130);
    expect(kill).toHaveBeenCalledOnce();
    expect(log.mock.calls[0]?.[0]).toMatch(/not valid releases/);
  });

  it("survives a child that already exited or no child at all", () => {
    const throwing = { kill: () => { throw new Error("gone"); } };
    expect(interruptHandler(throwing, () => undefined)("SIGINT")).toBe(130);
    expect(interruptHandler(null, () => undefined)("SIGINT")).toBe(130);
  });
});

const MANIFEST_KEYS = generateManifestKeyPair();
const MANIFEST_ENV = {
  UPDATE_MANIFEST_PUBLIC_KEY: MANIFEST_KEYS.publicKey,
  UPDATE_MANIFEST_SIGNING_KEY_FILE: "C:/secure/manifest-signing.pem",
};

describe("update feed and publisher configuration (M-11)", () => {
  it("accepts only an https feed without credentials, query or fragment, and adds a trailing slash", () => {
    expect(resolveFeedUrl({})).toBeNull();
    expect(resolveFeedUrl({ UPDATE_FEED_URL: "  " })).toBeNull();
    expect(resolveFeedUrl({ UPDATE_FEED_URL: "https://updates.example.com/desktop" })).toBe("https://updates.example.com/desktop/");
    expect(resolveFeedUrl({ UPDATE_FEED_URL: "https://updates.example.com/desktop/" })).toBe("https://updates.example.com/desktop/");
    for (const bad of ["http://updates.example.com/", "ftp://u.example.com/", "not a url", "https://u:p@updates.example.com/", "https://updates.example.com/?t=1", "https://updates.example.com/#x"]) {
      expect(() => resolveFeedUrl({ UPDATE_FEED_URL: bad }), bad).toThrow();
    }
  });

  it("validates the pinned publisher name", () => {
    expect(resolvePublisherName({})).toBeNull();
    expect(resolvePublisherName({ SIGN_PUBLISHER_NAME: " Example Ltd " })).toBe("Example Ltd");
    for (const bad of ['Evil "Corp"', "back\\slash", "line\nbreak", "x".repeat(201)]) {
      expect(() => resolvePublisherName({ SIGN_PUBLISHER_NAME: bad }), bad).toThrow();
    }
  });

  it("reads the Common Name from a certificate subject", () => {
    expect(commonNameOf("CN=Example Ltd, O=Example, C=US")).toBe("Example Ltd");
    expect(commonNameOf("O=Example, CN=Example Ltd")).toBe("Example Ltd");
    expect(commonNameOf('CN="Example, Ltd", O=X')).toBe("Example, Ltd");
    expect(commonNameOf("O=No common name")).toBeNull();
    expect(commonNameOf(null)).toBeNull();
    expect(commonNameOf("XCN=Spoof")).toBeNull();
  });

  it("a signed build with a feed must also name its publisher", () => {
    const signed = { REQUIRE_SIGNING: "true", CSC_LINK: "a.pfx", CSC_KEY_PASSWORD: "p", UPDATE_FEED_URL: "https://u.example.com/d" };
    const missing = preflight(signed, "1.0.0");
    expect(missing.ok).toBe(false);
    expect(missing.errors.join(" ")).toMatch(/SIGN_PUBLISHER_NAME/);
    const withPublisher = { ...signed, SIGN_PUBLISHER_NAME: "Example Ltd" };
    // SEC-09: a signed build with a feed also needs the manifest keys (see the tests below).
    expect(preflight({ ...withPublisher, ...MANIFEST_ENV }, "1.0.0").ok).toBe(true);
  });

  it("an insecure feed fails every kind of build; an unsigned build with a feed is warned about", () => {
    expect(preflight({ UPDATE_FEED_URL: "http://u.example.com/" }, "1.0.0").ok).toBe(false);
    const unsigned = preflight({ UPDATE_FEED_URL: "https://u.example.com/d" }, "1.0.0");
    expect(unsigned.ok).toBe(true);
    expect(unsigned.warnings.join(" ")).toMatch(/can never update itself/);
  });

  it("builds the publish target and pins the publisher into the signing options", () => {
    const base = { productName: "X", win: { target: "nsis" } };
    const env = { UPDATE_FEED_URL: "https://u.example.com/d", SIGN_PUBLISHER_NAME: "Example Ltd" };
    const pfx = builderConfig(base, env, "pfx");
    expect(pfx.publish).toEqual([{ provider: "generic", url: "https://u.example.com/d/" }]);
    expect(pfx.win).toMatchObject({ signtoolOptions: { publisherName: ["Example Ltd"], signingHashAlgorithms: ["sha256"] } });
    const azure = builderConfig(base, { ...env, AZURE_TRUSTED_SIGNING_ENDPOINT: "https://e", AZURE_TRUSTED_SIGNING_ACCOUNT: "a", AZURE_TRUSTED_SIGNING_PROFILE: "p" }, "azure");
    expect(azure.win).toMatchObject({ azureSignOptions: { publisherName: "Example Ltd" } });
    const none = builderConfig(base, { UPDATE_FEED_URL: "https://u.example.com/d" }, "none");
    expect(none.publish).toBeDefined();
    expect(JSON.stringify(none.win)).not.toMatch(/publisherName/); // unsigned: nothing pinned, so it can never self-update
    expect(builderConfig(base, {}, "none").publish).toBeUndefined();
  });
});

// M-10 review: the installer's signer must match the executable's and the pinned publisher.
import {
  resolveManifestPublicKey,
  scrubbedEnv,
  signerConsistencyErrors,
} from "../scripts/release/release-lib.mjs";
import { generateManifestKeyPair } from "../src/shared/update-manifest.mjs";

describe("signerConsistencyErrors", () => {
  const ok = "CN=Example Ltd, O=Example, C=US";
  it("accepts one identity that equals the pinned publisher", () => {
    expect(signerConsistencyErrors(ok, ok, "Example Ltd")).toEqual([]);
    expect(signerConsistencyErrors(ok, ok, null)).toEqual([]);
  });
  it("rejects an installer signed by a different (even Windows-trusted) certificate", () => {
    expect(signerConsistencyErrors("CN=Other Corp", ok, "Example Ltd").length).toBeGreaterThan(0);
    expect(signerConsistencyErrors("CN=Other Corp", ok, null)).toContain(
      "The installer and the application executable are signed by different certificates.",
    );
  });
  it("rejects a pinned publisher that neither file matches", () => {
    expect(signerConsistencyErrors(ok, ok, "Someone Else").length).toBe(2);
  });
  it("fails closed when a signer cannot be read", () => {
    expect(signerConsistencyErrors(null, ok, null).length).toBeGreaterThan(0);
    expect(signerConsistencyErrors(ok, "", null).length).toBeGreaterThan(0);
  });
});

// ---- SEC-09 / SEC-17 release tooling ----

describe("manifest signing configuration (SEC-09)", () => {
  const signedWithFeed = {
    REQUIRE_SIGNING: "true",
    CSC_LINK: "a.pfx",
    CSC_KEY_PASSWORD: "p",
    UPDATE_FEED_URL: "https://u.example.com/d",
    SIGN_PUBLISHER_NAME: "Example Ltd",
  };

  it("a signed build with a feed and no manifest public key is refused", () => {
    const result = preflight({ ...signedWithFeed, UPDATE_MANIFEST_SIGNING_KEY_FILE: "k.pem" }, "1.0.0");
    expect(result.ok).toBe(false);
    expect(result.errors.join(" ")).toMatch(/UPDATE_MANIFEST_PUBLIC_KEY/);
  });

  it("a signed build with a feed and no signing key file is refused", () => {
    const result = preflight({ ...signedWithFeed, UPDATE_MANIFEST_PUBLIC_KEY: MANIFEST_KEYS.publicKey }, "1.0.0");
    expect(result.ok).toBe(false);
    expect(result.errors.join(" ")).toMatch(/UPDATE_MANIFEST_SIGNING_KEY_FILE/);
  });

  it("a malformed public key is refused without echoing it", () => {
    const result = preflight({ ...signedWithFeed, ...MANIFEST_ENV, UPDATE_MANIFEST_PUBLIC_KEY: "SECRET-looking-value" }, "1.0.0");
    expect(result.ok).toBe(false);
    expect(result.errors.join(" ")).not.toMatch(/SECRET-looking-value/);
  });

  it("a build without a feed does not need the keys", () => {
    const { UPDATE_FEED_URL: _feed, SIGN_PUBLISHER_NAME: _name, ...noFeed } = signedWithFeed;
    expect(preflight(noFeed, "1.0.0").ok).toBe(true);
  });

  it("resolveManifestPublicKey returns null when unset and rejects a non-Ed25519 key", () => {
    expect(resolveManifestPublicKey({})).toBeNull();
    expect(resolveManifestPublicKey({ UPDATE_MANIFEST_PUBLIC_KEY: MANIFEST_KEYS.publicKey })).toBe(MANIFEST_KEYS.publicKey);
    expect(() => resolveManifestPublicKey({ UPDATE_MANIFEST_PUBLIC_KEY: "AAAA" })).toThrow(/Ed25519/);
  });

  it("the packaged app receives the public key as an extra resource only when a feed is built in", () => {
    const withKey = builderConfig({ win: {} }, { UPDATE_FEED_URL: "https://u.example.com/d" }, "none", "C:/tmp/key.txt");
    expect(withKey.extraResources).toEqual([{ from: "C:/tmp/key.txt", to: "update-manifest-key.txt" }]);
    expect(builderConfig({ win: {} }, {}, "none", "C:/tmp/key.txt").extraResources).toBeUndefined();
    expect(builderConfig({ win: {} }, { UPDATE_FEED_URL: "https://u.example.com/d" }, "none").extraResources).toBeUndefined();
  });
});

describe("signing credentials are not given to non-signing steps (SEC-17)", () => {
  it("scrubbedEnv removes every signing credential and keeps ordinary variables", () => {
    const env = {
      PATH: "/bin",
      REQUIRE_SIGNING: "true",
      SIGN_PUBLISHER_NAME: "Example Ltd",
      UPDATE_FEED_URL: "https://u.example.com/d",
      UPDATE_MANIFEST_PUBLIC_KEY: "public",
      CSC_LINK: "cert.pfx",
      CSC_KEY_PASSWORD: "secret",
      WIN_CSC_LINK: "cert2.pfx",
      WIN_CSC_KEY_PASSWORD: "secret2",
      AZURE_CLIENT_SECRET: "secret3",
      AZURE_TENANT_ID: "tenant",
      UPDATE_MANIFEST_SIGNING_KEY_FILE: "k.pem",
    };
    const out = scrubbedEnv(env);
    for (const name of ["CSC_LINK", "CSC_KEY_PASSWORD", "WIN_CSC_LINK", "WIN_CSC_KEY_PASSWORD", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "UPDATE_MANIFEST_SIGNING_KEY_FILE"]) {
      expect(out, name).not.toHaveProperty(name);
    }
    expect(out).toMatchObject({ PATH: "/bin", REQUIRE_SIGNING: "true", SIGN_PUBLISHER_NAME: "Example Ltd", UPDATE_FEED_URL: "https://u.example.com/d" });
    expect(env.CSC_KEY_PASSWORD).toBe("secret"); // the original is untouched
  });

  it("the release script passes the scrubbed environment to the build and verify steps, the full one only to electron-builder", () => {
    const source = readFileSync(join(__dirname, "../scripts/release/package-release.mjs"), "utf8");
    expect(source).toContain("const plainEnv = scrubbedEnv(env);");
    expect(source).toContain('"npm run build", plainEnv)');
    expect(source).toContain('"unsigned"], plainEnv)');
    const builderLine = source.split("\n").find((line) => line.includes("package (electron-builder)")) ?? "";
    expect(builderLine).toContain("builderEnv");
    expect(builderLine).not.toContain("plainEnv");
  });
});
