// Release logic (M-10, D-77). The only file-system write is `cleanReleaseDir`; there is no process
// spawning in this module, so every rule is unit-testable. Scripts in this folder compose these functions.
//
// Nothing here ever prints or returns a secret: environment checks report WHICH variable is missing,
// never its value.

import { createHash } from "node:crypto";
import { createReadStream, existsSync, readdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { parseManifestPublicKey } from "../../src/shared/update-manifest.mjs";

export const DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com";
export const MIN_CERT_VALID_DAYS_WARNING = 30;
export const X64_MACHINE = 0x8664;
const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;

/** @param {string} value */
export function isValidSemver(value) {
  return typeof value === "string" && SEMVER.test(value);
}

/** True only for the exact string "true": any other value never turns signing enforcement on or off by accident. */
export function signingRequired(env) {
  return env.REQUIRE_SIGNING === "true";
}

const present = (env, name) => typeof env[name] === "string" && env[name].trim() !== "";

/**
 * Which signing configuration the environment describes.
 * `pfx`: a certificate file/URL/base64 (CSC_LINK) and its password (CSC_KEY_PASSWORD).
 * `azure`: a managed cloud-signing account (the key never leaves the provider).
 * Returns the problems found (variable NAMES only).
 * @returns {{ mode: "pfx" | "azure" | "none", problems: string[] }}
 */
export function resolveSigningMode(env) {
  const pfxVars = ["CSC_LINK", "CSC_KEY_PASSWORD"];
  const azureVars = [
    "AZURE_TRUSTED_SIGNING_ENDPOINT",
    "AZURE_TRUSTED_SIGNING_ACCOUNT",
    "AZURE_TRUSTED_SIGNING_PROFILE",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
  ];
  const pfxSet = pfxVars.filter((n) => present(env, n));
  const azureSet = azureVars.filter((n) => present(env, n));
  if (pfxSet.length > 0 && azureSet.length > 0) {
    return {
      mode: "none",
      problems: ["Both certificate (CSC_*) and Azure signing variables are set; configure exactly one signing method."],
    };
  }
  if (pfxSet.length > 0) {
    const missing = pfxVars.filter((n) => !present(env, n));
    return { mode: "pfx", problems: missing.map((n) => `${n} is not set.`) };
  }
  if (azureSet.length > 0) {
    const missing = azureVars.filter((n) => !present(env, n));
    return { mode: "azure", problems: missing.map((n) => `${n} is not set.`) };
  }
  return { mode: "none", problems: [] };
}

/** The RFC 3161 timestamp service. Must be an http(s) URL without credentials. */
export function resolveTimestampUrl(env) {
  const raw = present(env, "SIGN_TIMESTAMP_URL") ? env.SIGN_TIMESTAMP_URL.trim() : DEFAULT_TIMESTAMP_URL;
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("SIGN_TIMESTAMP_URL is not a valid URL.");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("SIGN_TIMESTAMP_URL must be an http or https URL.");
  }
  if (url.username !== "" || url.password !== "") {
    throw new Error("SIGN_TIMESTAMP_URL must not contain credentials.");
  }
  return url.toString();
}

/** The auto-update feed (M-11): an https URL without credentials, query or fragment; null when unset. */
export function resolveFeedUrl(env) {
  if (!present(env, "UPDATE_FEED_URL")) return null;
  let url;
  try {
    url = new URL(env.UPDATE_FEED_URL.trim());
  } catch {
    throw new Error("UPDATE_FEED_URL is not a valid URL.");
  }
  if (url.protocol !== "https:") throw new Error("UPDATE_FEED_URL must be an https URL.");
  if (url.username !== "" || url.password !== "" || url.search !== "" || url.hash !== "") {
    throw new Error("UPDATE_FEED_URL must not contain credentials, a query or a fragment.");
  }
  return url.toString().endsWith("/") ? url.toString() : `${url.toString()}/`;
}

/** The publisher name pinned into the build; must equal the signing certificate's CN. */
export function resolvePublisherName(env) {
  if (!present(env, "SIGN_PUBLISHER_NAME")) return null;
  const name = env.SIGN_PUBLISHER_NAME.trim();
  if (/[\x00-\x1f"\\]/.test(name) || name.length > 200) throw new Error("SIGN_PUBLISHER_NAME contains unsupported characters.");
  return name;
}

/**
 * The update-manifest signing public key (SEC-09), base64 SPKI Ed25519, or null when not set.
 * Throws a message that never contains the value when it is set but is not such a key.
 */
export function resolveManifestPublicKey(env) {
  if (!present(env, "UPDATE_MANIFEST_PUBLIC_KEY")) return null;
  const value = env.UPDATE_MANIFEST_PUBLIC_KEY.trim();
  if (parseManifestPublicKey(value) === null) {
    throw new Error("UPDATE_MANIFEST_PUBLIC_KEY is not a base64 Ed25519 public key.");
  }
  return value;
}

/**
 * The environment for build steps that do NOT sign (type-check, bundling, verification). Signing
 * credentials are removed, so a plugin or script in those steps can never read them (SEC-17); only
 * the electron-builder step receives them.
 */
export function scrubbedEnv(env) {
  const out = {};
  for (const [name, value] of Object.entries(env)) {
    if (/^(CSC_|WIN_CSC_|AZURE_|UPDATE_MANIFEST_SIGNING_KEY_FILE$)/i.test(name)) continue;
    out[name] = value;
  }
  return out;
}

/** Common Name from an X.500 subject such as `CN=Example Ltd, O=Example, C=US`. */
export function commonNameOf(subject) {
  const match = /(?:^|,\s*)CN=("([^"]+)"|[^,]+)/i.exec(String(subject ?? ""));
  return match ? (match[2] ?? match[1]).trim() : null;
}

/**
 * A signed release is signed by ONE identity, and that identity is the pinned publisher when one is
 * configured. Compares the installer's and the application executable's signer Common Names (and the
 * pinned name, if given). Returns error strings; empty means consistent. Pure.
 */
export function signerConsistencyErrors(installerSubject, appSubject, publisherName) {
  const errors = [];
  const installerCn = commonNameOf(installerSubject);
  const appCn = commonNameOf(appSubject);
  if (installerCn === null) errors.push("The installer's signer Common Name could not be read.");
  if (appCn === null) errors.push("The application executable's signer Common Name could not be read.");
  if (installerCn !== null && appCn !== null && installerCn !== appCn) {
    errors.push("The installer and the application executable are signed by different certificates.");
  }
  if (publisherName) {
    if (installerCn !== null && installerCn !== publisherName) {
      errors.push("The installer is not signed by the configured publisher (SIGN_PUBLISHER_NAME).");
    }
    if (appCn !== null && appCn !== publisherName) {
      errors.push("The application executable is not signed by the configured publisher (SIGN_PUBLISHER_NAME).");
    }
  }
  return errors;
}

/**
 * Release preflight: everything that can be decided before packaging starts.
 * `signed` builds must have a complete, single signing configuration.
 * @returns {{ ok: boolean, kind: "signed" | "unsigned-internal", mode: string, errors: string[], warnings: string[] }}
 */
export function preflight(env, packageVersion) {
  const errors = [];
  const warnings = [];
  if (!isValidSemver(packageVersion)) {
    errors.push(`package.json version "${String(packageVersion)}" is not a valid semantic version.`);
  }
  const required = signingRequired(env);
  const { mode, problems } = resolveSigningMode(env);
  try {
    const feed = resolveFeedUrl(env);
    const publisher = resolvePublisherName(env);
    if (required && feed !== null && publisher === null) {
      errors.push("UPDATE_FEED_URL is set for a signed build, so SIGN_PUBLISHER_NAME (the certificate's Common Name) must be set: updates are verified against it.");
    }
    if (required && feed !== null) {
      // SEC-09: a signed build with a feed must be able to authenticate its update manifests.
      if (resolveManifestPublicKey(env) === null) {
        errors.push("UPDATE_FEED_URL is set for a signed build, so UPDATE_MANIFEST_PUBLIC_KEY (the Ed25519 public key the app pins to authenticate update manifests) must be set.");
      }
      if (!present(env, "UPDATE_MANIFEST_SIGNING_KEY_FILE")) {
        errors.push("UPDATE_FEED_URL is set for a signed build, so UPDATE_MANIFEST_SIGNING_KEY_FILE (the private key used to sign latest.yml) must be set.");
      }
    }
    if (!required && feed !== null) warnings.push("UPDATE_FEED_URL is set on an unsigned build: it will be built with the feed but can never update itself.");
  } catch (error) {
    errors.push(error instanceof Error ? error.message : "Invalid update configuration.");
  }
  try {
    resolveTimestampUrl(env);
  } catch (error) {
    errors.push(error instanceof Error ? error.message : "Invalid timestamp URL.");
  }
  if (required) {
    if (mode === "none" && problems.length === 0) {
      errors.push("REQUIRE_SIGNING=true but no signing configuration is available (set CSC_LINK + CSC_KEY_PASSWORD, or the Azure signing variables).");
    }
    errors.push(...problems);
  } else {
    if (mode !== "none") warnings.push("Signing variables are set but REQUIRE_SIGNING is not \"true\": the build will be signed if electron-builder accepts them, but it is not enforced.");
    warnings.push("UNSIGNED internal build: not for distribution.");
  }
  const ok = errors.length === 0;
  return { ok, kind: required ? "signed" : "unsigned-internal", mode, errors, warnings };
}

/**
 * Judge a certificate (from PowerShell, as plain data) for code signing.
 * @param {{ notBefore: string, notAfter: string, hasPrivateKey: boolean, codeSigningEku: boolean, chainValid: boolean, chainStatus?: string[] }} cert
 * @param {Date} now
 */
export function evaluateCertificate(cert, now) {
  const errors = [];
  const warnings = [];
  const notBefore = new Date(cert.notBefore);
  const notAfter = new Date(cert.notAfter);
  if (Number.isNaN(notBefore.getTime()) || Number.isNaN(notAfter.getTime())) {
    errors.push("The certificate validity dates could not be read.");
    return { errors, warnings };
  }
  if (now < notBefore) errors.push("The signing certificate is not yet valid.");
  if (now >= notAfter) errors.push("The signing certificate has expired.");
  else if ((notAfter.getTime() - now.getTime()) / 86_400_000 < MIN_CERT_VALID_DAYS_WARNING) {
    warnings.push(`The signing certificate expires in fewer than ${MIN_CERT_VALID_DAYS_WARNING} days: renew it.`);
  }
  if (!cert.hasPrivateKey) errors.push("The certificate has no private key.");
  if (!cert.codeSigningEku) errors.push("The certificate is not valid for code signing.");
  if (!cert.chainValid) {
    errors.push(`The certificate chain is not trusted${cert.chainStatus?.length ? ` (${cert.chainStatus.join(", ")})` : ""}.`);
  }
  return { errors, warnings };
}

/**
 * Judge one file's Authenticode result. `info` comes from `Get-AuthenticodeSignature` and signtool.
 * @param {{ status: string, signerNotAfter?: string, hasTimestamp: boolean, digestAlgorithm?: string | null }} info
 * @param {{ requireSigned: boolean, now: Date }} options
 */
export function evaluateSignature(info, options) {
  const errors = [];
  const warnings = [];
  if (!options.requireSigned) {
    if (info.status !== "NotSigned") {
      errors.push(`Expected an unsigned internal build but the file reports "${info.status}".`);
    }
    return { errors, warnings };
  }
  if (info.status !== "Valid") errors.push(`Authenticode status is "${info.status}", not "Valid".`);
  if (!info.hasTimestamp) errors.push("The signature has no trusted timestamp.");
  if ((info.digestAlgorithm ?? "").toLowerCase() !== "sha256") {
    errors.push(`The signature digest is "${info.digestAlgorithm ?? "unknown"}", not sha256.`);
  }
  if (info.signerNotAfter) {
    const notAfter = new Date(info.signerNotAfter);
    if (Number.isNaN(notAfter.getTime())) errors.push("The signer certificate expiry could not be read.");
    else if (notAfter <= options.now) warnings.push("The signer certificate has since expired; the timestamp keeps the signature valid.");
  }
  return { errors, warnings };
}

/** The executable's FileVersion equals the package version (Windows may append a fourth ".0"). */
export function fileVersionMatches(fileVersion, version) {
  const found = String(fileVersion ?? "").trim();
  return found === version || found === `${version}.0`;
}

/** Machine type from a PE image header (0x8664 = x64). Returns null for a non-PE buffer. */
export function peMachine(buffer) {
  if (buffer.length < 0x40 || buffer.readUInt16LE(0) !== 0x5a4d) return null;
  const peOffset = buffer.readUInt32LE(0x3c);
  if (peOffset + 6 > buffer.length || buffer.toString("latin1", peOffset, peOffset + 4) !== "PE\0\0") return null;
  return buffer.readUInt16LE(peOffset + 4);
}

/** SHA-512 as base64, the form `latest.yml` uses. */
export function sha512Base64File(path) {
  return new Promise((resolve, reject) => {
    const hash = createHash("sha512");
    const stream = createReadStream(path);
    stream.on("error", reject);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolve(hash.digest("base64")));
  });
}

/** @param {string} path */
export function sha256File(path) {
  return new Promise((resolve, reject) => {
    const hash = createHash("sha256");
    const stream = createReadStream(path);
    stream.on("error", reject);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolve(hash.digest("hex")));
  });
}

/** `SHA256SUMS` text: "<hex>  <name>" per line (the format `sha256sum -c` reads). */
export function formatSums(entries) {
  return entries.map(({ name, sha256 }) => `${sha256}  ${name}`).join("\n") + "\n";
}

export function parseSums(text) {
  const entries = [];
  for (const line of text.split(/\r?\n/)) {
    if (line.trim() === "") continue;
    const match = /^([0-9a-f]{64}) {2}(.+)$/.exec(line);
    if (!match) throw new Error("The checksum file is malformed.");
    entries.push({ sha256: match[1], name: match[2] });
  }
  return entries;
}

/** Names whose actual hash differs from the recorded one (or that are missing). */
export function compareSums(expected, actualByName) {
  const bad = [];
  for (const { name, sha256 } of expected) {
    if (actualByName[name] !== sha256) bad.push(name);
  }
  return bad;
}

/** Fuse state as the release requires it (docs/DESKTOP_RELEASE.md). Keys are `FuseV1Options` names. */
export const REQUIRED_FUSES = Object.freeze({
  RunAsNode: false,
  EnableCookieEncryption: true,
  EnableNodeOptionsEnvironmentVariable: false,
  EnableNodeCliInspectArguments: false,
  EnableEmbeddedAsarIntegrityValidation: true,
  OnlyLoadAppFromAsar: true,
});

/** @param {Record<string, boolean>} actual fuse name -> enabled */
export function evaluateFuses(actual) {
  const errors = [];
  for (const [name, wanted] of Object.entries(REQUIRED_FUSES)) {
    if (actual[name] !== wanted) errors.push(`Fuse ${name} is ${String(actual[name])}, expected ${String(wanted)}.`);
  }
  return errors;
}

export const REQUIRED_UNPACKED_FILES = Object.freeze([
  "Interview Assistant.exe",
  "resources/app.asar",
  "resources.pak",
  "icudtl.dat",
]);

/** @param {string[]} present relative paths (forward slashes) found in win-unpacked */
export function missingFiles(present) {
  const have = new Set(present.map((p) => p.replace(/\\/g, "/")));
  return REQUIRED_UNPACKED_FILES.filter((f) => !have.has(f));
}

/** Installer name electron-builder produces for this version (see build.win.artifactName). */
export function expectedInstallerName(productName, version) {
  return `${productName}-${version}-Windows-x64.exe`;
}

/**
 * Stop a child cleanly on interruption and report what to clean. Pure: callers pass the kill function.
 * @param {{ kill: () => void } | null} child
 */
export function interruptHandler(child, log) {
  return (signal) => {
    log(`Interrupted by ${signal}: stopping the build; partial artifacts are not valid releases.`);
    try {
      child?.kill();
    } catch {
      /* the child already exited */
    }
    return 130;
  };
}

/** Remove artifacts of earlier builds so verification can never pass on a stale file. */
export function cleanReleaseDir(root) {
  const dir = join(root, "release");
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir)) rmSync(join(dir, entry), { recursive: true, force: true });
}

/** electron-builder configuration for this run: package.json's `build` plus the signing settings. */
export function builderConfig(pkgBuild, env, mode, manifestKeyFile = null) {
  const config = JSON.parse(JSON.stringify(pkgBuild));
  const timestamp = resolveTimestampUrl(env);
  const feed = resolveFeedUrl(env);
  const publisher = resolvePublisherName(env);
  if (mode === "pfx") {
    config.win = {
      ...config.win,
      forceCodeSigning: true,
      signtoolOptions: {
        signingHashAlgorithms: ["sha256"],
        rfc3161TimeStampServer: timestamp,
        ...(publisher ? { publisherName: [publisher] } : {}),
      },
    };
  } else if (mode === "azure") {
    config.win = {
      ...config.win,
      forceCodeSigning: true,
      azureSignOptions: {
        endpoint: env.AZURE_TRUSTED_SIGNING_ENDPOINT,
        codeSigningAccountName: env.AZURE_TRUSTED_SIGNING_ACCOUNT,
        certificateProfileName: env.AZURE_TRUSTED_SIGNING_PROFILE,
        fileDigest: "SHA256",
        timestampDigest: "SHA256",
        timestampRfc3161: env.SIGN_TIMESTAMP_URL ? timestamp : "http://timestamp.acs.microsoft.com",
        ...(publisher ? { publisherName: publisher } : {}),
      },
    };
  }
  // The update feed (M-11): a static https host. The built app reads it from app-update.yml; the
  // pinned publisher above is what the updater verifies installers against.
  if (feed !== null) config.publish = [{ provider: "generic", url: feed }];
  // SEC-09: ship the manifest-signing public key inside the app's resources (next to app-update.yml).
  if (feed !== null && manifestKeyFile !== null) {
    config.extraResources = [...(config.extraResources ?? []), { from: manifestKeyFile, to: "update-manifest-key.txt" }];
  }
  return config;
}
