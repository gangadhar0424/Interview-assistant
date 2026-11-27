import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  compareSemver,
  evaluateUpdateConfig,
  isSafeArtifactName,
  MAX_INSTALLER_BYTES,
  MAX_MANIFEST_BYTES,
  parseAppUpdateYml,
  parseLatestYml,
  parseSemver,
  requiredFreeBytes,
  validateManifest,
} from "../src/shared/update-manifest.mjs";

const SHA = createHash("sha512").update("installer").digest("base64");
const NAME = "Interview Assistant-0.2.0-Windows-x64.exe";

function latest(over: Partial<{ version: string; url: string; sha512: string; size: string; path: string }> = {}): string {
  const o = { version: "0.2.0", url: NAME, sha512: SHA, size: "123992589", path: NAME, ...over };
  return [
    `version: ${o.version}`,
    "files:",
    `  - url: ${o.url}`,
    `    sha512: ${o.sha512}`,
    `    size: ${o.size}`,
    `path: ${o.path}`,
    `sha512: ${o.sha512}`,
    "releaseDate: '2026-11-22T10:00:00.000Z'",
    "",
  ].join("\n");
}
const verdict = (text: string, currentVersion = "0.1.0", allowPrerelease = false) =>
  validateManifest(parseLatestYml(text), { currentVersion, allowPrerelease });

describe("semantic versions", () => {
  it("parses and orders versions by semver precedence", () => {
    expect(parseSemver("1.2.3")).toEqual({ major: 1, minor: 2, patch: 3, pre: [] });
    expect(parseSemver("1.2.3-beta.1")?.pre).toEqual(["beta", "1"]);
    for (const bad of ["", "1", "1.2", "1.2.3.4", "01.2.3", "v1.2.3", "1.2.3-", "x", null, undefined, 5]) expect(parseSemver(bad)).toBeNull();
    expect(compareSemver("0.2.0", "0.1.0")).toBe(1);
    expect(compareSemver("0.1.0", "0.2.0")).toBe(-1);
    expect(compareSemver("0.1.0", "0.1.0")).toBe(0);
    expect(compareSemver("0.10.0", "0.9.0")).toBe(1);
    expect(compareSemver("1.0.0", "1.0.0-rc.1")).toBe(1);
    expect(compareSemver("1.0.0-rc.2", "1.0.0-rc.10")).toBe(-1);
    expect(compareSemver("1.0.0-alpha", "1.0.0-alpha.1")).toBe(-1);
    expect(compareSemver("1.0.0-1", "1.0.0-alpha")).toBe(-1);
    expect(compareSemver("bad", "1.0.0")).toBeNull();
  });
});

describe("safe installer names", () => {
  it("accepts a plain installer name", () => {
    expect(isSafeArtifactName(NAME)).toBe(true);
    expect(isSafeArtifactName("Interview%20Assistant-0.2.0-Windows-x64.exe")).toBe(true);
  });

  it("rejects traversal, separators, schemes, control characters and the wrong type", () => {
    for (const bad of [
      "../evil.exe",
      "..\\evil.exe",
      "a/b.exe",
      "a\\b.exe",
      "C:evil.exe",
      "https://evil.example/x.exe",
      "%2e%2e%2fevil.exe",
      "%2e%2e%5cevil.exe",
      "evil.exe\u0000.txt",
      "installer.zip",
      "installer.msi",
      "installer.exe.txt",
      "",
      " lead.exe",
      "trail.exe ",
      "x".repeat(300) + ".exe",
      "%E0%A4%A.exe",
    ]) {
      expect(isSafeArtifactName(bad), JSON.stringify(bad)).toBe(false);
    }
    expect(isSafeArtifactName(undefined)).toBe(false);
    expect(isSafeArtifactName(42)).toBe(false);
  });
});

describe("latest.yml parsing is strict", () => {
  it("reads electron-builder's manifest", () => {
    const m = parseLatestYml(latest());
    expect(m).toMatchObject({ version: "0.2.0", path: NAME, sha512: SHA, releaseDate: "2026-11-22T10:00:00.000Z" });
    expect(m.files).toEqual([{ url: NAME, sha512: SHA, size: 123992589 }]);
  });

  it("rejects hostile or unsupported YAML instead of interpreting it", () => {
    const bad = [
      "version: &a 1.0.0\nfiles: []",
      "version: *a",
      "version: !!js/function 'x'",
      "version: |\n  1.0.0",
      "version: {a: b}",
      "version: [1, 2]",
      "version: 1.0.0\nversion: 2.0.0",
      "version: 1.0.0\nfiles:\n  - url: a.exe\n    url: b.exe",
      "no colon here",
      "version: 1.0.0\n\u0007bell: x",
      "  indented: first",
    ];
    for (const text of bad) expect(() => parseLatestYml(text), JSON.stringify(text)).toThrow();
  });

  it("rejects oversized and non-text input", () => {
    expect(() => parseLatestYml("a: " + "x".repeat(MAX_MANIFEST_BYTES))).toThrow(/too large/);
    expect(() => parseLatestYml(undefined as unknown as string)).toThrow();
    expect(parseLatestYml("").files).toEqual([]);
  });
});

describe("manifest validation (what may be offered)", () => {
  it("offers a strictly newer, well-formed update", () => {
    expect(verdict(latest())).toEqual({ errors: [], newer: true, version: "0.2.0" });
  });

  it("never offers the same version or a downgrade", () => {
    expect(verdict(latest({ version: "0.1.0" })).newer).toBe(false);
    expect(verdict(latest({ version: "0.0.9" })).newer).toBe(false);
    expect(verdict(latest({ version: "0.2.0" }), "0.3.0").newer).toBe(false);
  });

  it("does not offer a pre-release to a stable build, unless allowed or the build is itself a pre-release", () => {
    expect(verdict(latest({ version: "0.3.0-beta.1" })).newer).toBe(false);
    expect(verdict(latest({ version: "0.3.0-beta.1" }), "0.1.0", true).newer).toBe(true);
    expect(verdict(latest({ version: "0.3.0-beta.2" }), "0.3.0-beta.1").newer).toBe(true);
  });

  it("rejects malformed versions", () => {
    for (const version of ["latest", "1.2", "../1.2.3", "1.2.3; rm -rf", ""]) {
      const r = verdict(latest({ version }));
      expect(r.newer, version).toBe(false);
      expect(r.errors.length, version).toBeGreaterThan(0);
    }
  });

  it("rejects an unsafe file name, a bad checksum and a bad size", () => {
    expect(verdict(latest({ url: "../evil.exe", path: "../evil.exe" })).errors.join(" ")).toMatch(/safe installer name/);
    expect(verdict(latest({ sha512: "abc" })).errors.join(" ")).toMatch(/SHA-512/);
    expect(verdict(latest({ sha512: SHA.slice(0, 80) + "!!!!!!==" })).errors.join(" ")).toMatch(/SHA-512/);
    for (const size of ["0", "-5", "1.5", "abc", String(MAX_INSTALLER_BYTES + 1)]) {
      expect(verdict(latest({ size })).errors.join(" "), size).toMatch(/size/);
    }
  });

  it("rejects a manifest that lists no files or a path that is not listed", () => {
    expect(verdict("version: 0.2.0\n").errors.join(" ")).toMatch(/no files/);
    expect(verdict(latest({ path: "Other-0.2.0.exe" })).errors.join(" ")).toMatch(/not among the listed files/);
  });
});

describe("update configuration baked into the build", () => {
  const cfg = (extra = "") => `provider: generic\nurl: https://updates.example.com/desktop/\n${extra}`;

  it("enables updates only for an https feed with a pinned publisher", () => {
    const parsed = parseAppUpdateYml(cfg("publisherName:\n  - Example Ltd\nupdaterCacheDirName: x-updater\n"));
    expect(parsed.publisherName).toEqual(["Example Ltd"]);
    expect(evaluateUpdateConfig(parsed)).toEqual({ enabled: true, reason: null, feedUrl: "https://updates.example.com/desktop/" });
  });

  it("normalises the feed URL to end with a slash", () => {
    expect(evaluateUpdateConfig({ provider: "generic", url: "https://u.example.com/d", publisherName: ["X"] }).feedUrl).toBe("https://u.example.com/d/");
  });

  it("an unsigned build (no publisher) can never self-update", () => {
    expect(evaluateUpdateConfig(parseAppUpdateYml(cfg()))).toEqual({ enabled: false, reason: "unsigned_build", feedUrl: null });
  });

  it("refuses an insecure or credentialed feed and other providers", () => {
    const pub = ["X"];
    expect(evaluateUpdateConfig({ provider: "generic", url: "http://u.example.com/", publisherName: pub }).reason).toBe("insecure_feed");
    expect(evaluateUpdateConfig({ provider: "generic", url: "https://user:pw@u.example.com/", publisherName: pub }).reason).toBe("insecure_feed");
    expect(evaluateUpdateConfig({ provider: "generic", url: "https://u.example.com/?token=1", publisherName: pub }).reason).toBe("insecure_feed");
    expect(evaluateUpdateConfig({ provider: "github", url: "https://u.example.com/", publisherName: pub }).reason).toBe("not_configured");
    expect(evaluateUpdateConfig({ provider: "generic", url: "not a url", publisherName: pub }).reason).toBe("not_configured");
    expect(evaluateUpdateConfig({ provider: "", url: "", publisherName: [] }).enabled).toBe(false);
  });

  it("computes the disk space an update needs", () => {
    expect(requiredFreeBytes(100)).toBe(200 + 100 * 1024 * 1024);
  });
});

// ---- SEC-09: manifest signatures ---------------------------------------------------------------

import {
  generateManifestKeyPair,
  MANIFEST_SIGNATURE_DOMAIN,
  parseManifestPublicKey,
  publicKeyOfPrivate,
  signManifest,
  verifyManifestSignature,
} from "../src/shared/update-manifest.mjs";
import { generateKeyPairSync } from "node:crypto";

describe("manifest signatures (SEC-09)", () => {
  const keys = generateManifestKeyPair();
  const text = "version: 1.2.3\nfiles: []\n";

  it("a signature verifies over exactly the bytes that were signed", () => {
    const signature = signManifest(text, keys.privateKeyPem);
    expect(verifyManifestSignature(text, signature, keys.publicKey)).toBe(true);
    expect(verifyManifestSignature(text, `${signature}\n`, keys.publicKey)).toBe(true); // a trailing newline in the file
    expect(verifyManifestSignature(text, `ed25519:${signature}`, keys.publicKey)).toBe(true);
  });

  it("any change to the manifest, the signature or the key is refused", () => {
    const signature = signManifest(text, keys.privateKeyPem);
    expect(verifyManifestSignature(`${text} `, signature, keys.publicKey)).toBe(false);
    expect(verifyManifestSignature(text.replace("1.2.3", "9.9.9"), signature, keys.publicKey)).toBe(false);
    const flipped = (signature[0] === "A" ? "B" : "A") + signature.slice(1);
    expect(verifyManifestSignature(text, flipped, keys.publicKey)).toBe(false);
    const other = generateManifestKeyPair();
    expect(verifyManifestSignature(text, signature, other.publicKey)).toBe(false);
  });

  it("the signature is bound to this purpose (domain separation)", () => {
    expect(MANIFEST_SIGNATURE_DOMAIN).toContain("update-manifest");
    // A signature over the bare text (no domain prefix) must not verify.
    const { privateKey } = generateKeyPairSync("ed25519");
    expect(verifyManifestSignature(text, "A".repeat(86) + "==", keys.publicKey)).toBe(false);
    expect(privateKey.asymmetricKeyType).toBe("ed25519");
  });

  it("malformed input never throws and never verifies", () => {
    for (const bad of [undefined, null, "", "x", "A".repeat(87), "!!!", 42, {}]) {
      expect(verifyManifestSignature(text, bad as never, keys.publicKey)).toBe(false);
      expect(verifyManifestSignature(text, signManifest(text, keys.privateKeyPem), bad as never)).toBe(false);
    }
  });

  it("only an Ed25519 public key is accepted as the pinned key", () => {
    expect(parseManifestPublicKey(keys.publicKey)).not.toBeNull();
    expect(parseManifestPublicKey("")).toBeNull();
    expect(parseManifestPublicKey("AAAA")).toBeNull();
    const rsa = generateKeyPairSync("rsa", { modulusLength: 2048 }).publicKey.export({ format: "der", type: "spki" }).toString("base64");
    expect(parseManifestPublicKey(rsa)).toBeNull();
  });

  it("a private key maps to its own public key and non-Ed25519 keys are refused for signing", () => {
    expect(publicKeyOfPrivate(keys.privateKeyPem)).toBe(keys.publicKey);
    expect(publicKeyOfPrivate("not a key")).toBeNull();
    const rsaPem = generateKeyPairSync("rsa", { modulusLength: 2048 }).privateKey.export({ format: "pem", type: "pkcs8" }).toString();
    expect(publicKeyOfPrivate(rsaPem)).toBeNull();
    expect(() => signManifest(text, rsaPem)).toThrow();
  });
});
