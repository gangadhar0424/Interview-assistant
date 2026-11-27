// Update metadata rules (M-11, D-78). One implementation shared by the app's updater and by the release
// verifier, so the build and the client agree on what a valid update looks like.
//
// The update feed is untrusted input: a manifest is parsed with a deliberately tiny, strict reader
// (no YAML library, no anchors, no tags), size-capped, and every field is validated before the
// updater is allowed to download anything.

import { createPrivateKey, createPublicKey, generateKeyPairSync, sign, verify } from "node:crypto";

export const MAX_MANIFEST_BYTES = 64 * 1024;
export const MAX_INSTALLER_BYTES = 500 * 1024 * 1024;
export const MIN_FREE_BYTES_MARGIN = 100 * 1024 * 1024;

const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;
const SHA512_B64 = /^[A-Za-z0-9+/]{86}==$/; // 64 bytes, base64

/** @returns {{ major: number, minor: number, patch: number, pre: string[] } | null} */
export function parseSemver(value) {
  if (typeof value !== "string") return null;
  const m = SEMVER.exec(value);
  if (!m) return null;
  return { major: Number(m[1]), minor: Number(m[2]), patch: Number(m[3]), pre: m[4] ? m[4].split(".") : [] };
}

/** -1, 0 or 1 (semver precedence; a version with a pre-release is lower than the same without). Null if either is invalid. */
export function compareSemver(a, b) {
  const x = parseSemver(a);
  const y = parseSemver(b);
  if (!x || !y) return null;
  for (const key of ["major", "minor", "patch"]) {
    if (x[key] !== y[key]) return x[key] < y[key] ? -1 : 1;
  }
  if (x.pre.length === 0 && y.pre.length === 0) return 0;
  if (x.pre.length === 0) return 1;
  if (y.pre.length === 0) return -1;
  const length = Math.max(x.pre.length, y.pre.length);
  for (let i = 0; i < length; i += 1) {
    const l = x.pre[i];
    const r = y.pre[i];
    if (l === undefined) return -1;
    if (r === undefined) return 1;
    if (l === r) continue;
    const ln = /^\d+$/.test(l);
    const rn = /^\d+$/.test(r);
    if (ln && rn) return Number(l) < Number(r) ? -1 : 1;
    if (ln) return -1;
    if (rn) return 1;
    return l < r ? -1 : 1;
  }
  return 0;
}

function unquote(raw) {
  const value = raw.trim();
  const quoted = /^(['"])(.*)\1$/.exec(value);
  if (quoted) return quoted[2];
  // Anchors, aliases, tags, block scalars and flow collections are not part of the subset.
  if (/^[&*!|>{[]/.test(value)) throw new Error("The metadata uses unsupported YAML features.");
  return value;
}

/**
 * Parse the small YAML subset electron-builder writes (`latest.yml`, `app-update.yml`): top-level
 * `key: value` lines and one level of lists of `- key: value` maps or plain `- value` scalars.
 * Anything else throws. Never executes or resolves anything.
 */
function parseSimpleYaml(text) {
  if (typeof text !== "string") throw new Error("The metadata is not text.");
  if (Buffer.byteLength(text, "utf8") > MAX_MANIFEST_BYTES) throw new Error("The metadata is too large.");
  const result = {};
  let listKey = null;
  let current = null;
  for (const rawLine of text.split(/\r?\n/)) {
    if (rawLine.trim() === "" || rawLine.trim().startsWith("#")) continue;
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(rawLine)) throw new Error("The metadata contains control characters.");
    const top = /^([A-Za-z][A-Za-z0-9_]*):(?:\s+(.*))?$/.exec(rawLine);
    if (top) {
      listKey = null;
      current = null;
      const [, key, value] = top;
      if (Object.prototype.hasOwnProperty.call(result, key)) throw new Error("The metadata repeats a key.");
      if (value === undefined || value.trim() === "") {
        result[key] = [];
        listKey = key;
      } else {
        result[key] = unquote(value);
      }
      continue;
    }
    const item = /^\s{2}-\s+(?:([A-Za-z][A-Za-z0-9_]*):\s*(.*)|(.+))$/.exec(rawLine);
    if (item && listKey) {
      if (item[1] !== undefined) {
        current = { [item[1]]: unquote(item[2] ?? "") };
        result[listKey].push(current);
      } else {
        current = null;
        result[listKey].push(unquote(item[3] ?? ""));
      }
      continue;
    }
    const cont = /^\s{4}([A-Za-z][A-Za-z0-9_]*):\s*(.*)$/.exec(rawLine);
    if (cont && current) {
      if (Object.prototype.hasOwnProperty.call(current, cont[1])) throw new Error("The metadata repeats a key.");
      current[cont[1]] = unquote(cont[2] ?? "");
      continue;
    }
    throw new Error("The metadata is not in the expected format.");
  }
  return result;
}

/** A safe installer file name: no separators, no traversal, no scheme, no control characters, .exe only. */
export function isSafeArtifactName(name) {
  if (typeof name !== "string" || name.length === 0 || name.length > 200) return false;
  let decoded;
  try {
    decoded = decodeURIComponent(name);
  } catch {
    return false;
  }
  for (const candidate of [name, decoded]) {
    if (/[\\/]/.test(candidate) || candidate.includes("..") || candidate.includes(":")) return false;
    if (/[\x00-\x1f]/.test(candidate)) return false;
    if (/^\s|\s$/.test(candidate)) return false;
  }
  return /\.exe$/i.test(decoded);
}

/**
 * Parse `latest.yml`. Throws for malformed metadata.
 * @returns {{ version: string, path: string, sha512: string, releaseDate: string | null, files: { url: string, sha512: string, size: number }[] }}
 */
export function parseLatestYml(text) {
  const raw = parseSimpleYaml(text);
  const files = Array.isArray(raw.files)
    ? raw.files.map((f) => {
        if (typeof f !== "object" || f === null) throw new Error("A file entry is malformed.");
        return { url: String(f.url ?? ""), sha512: String(f.sha512 ?? ""), size: Number(f.size) };
      })
    : [];
  return {
    version: String(raw.version ?? ""),
    path: String(raw.path ?? ""),
    sha512: String(raw.sha512 ?? ""),
    releaseDate: typeof raw.releaseDate === "string" ? raw.releaseDate : null,
    files,
  };
}

/**
 * Judge a parsed manifest against the running version. Never throws.
 * `newer` is true only when every check passed AND the version is strictly higher (no downgrade, no
 * re-install of the same version, no pre-release unless the running build is itself a pre-release).
 * @returns {{ errors: string[], newer: boolean, version: string | null }}
 */
export function validateManifest(manifest, options) {
  const errors = [];
  const current = parseSemver(options.currentVersion);
  const next = parseSemver(manifest.version);
  if (!current) errors.push("The running version is not a valid semantic version.");
  if (!next) errors.push("The update version is not a valid semantic version.");
  if (manifest.files.length === 0) errors.push("The update lists no files.");
  for (const file of manifest.files) {
    if (!isSafeArtifactName(file.url)) errors.push("The update file name is not a safe installer name.");
    if (!SHA512_B64.test(file.sha512)) errors.push("The update checksum is not a valid SHA-512.");
    if (!Number.isInteger(file.size) || file.size <= 0 || file.size > MAX_INSTALLER_BYTES) {
      errors.push("The update size is not valid.");
    }
  }
  if (manifest.path !== "" && !isSafeArtifactName(manifest.path)) errors.push("The update path is not a safe installer name.");
  if (manifest.sha512 !== "" && !SHA512_B64.test(manifest.sha512)) errors.push("The update checksum is not a valid SHA-512.");
  if (manifest.path !== "" && manifest.files.length > 0 && !manifest.files.some((f) => f.url === manifest.path)) {
    errors.push("The update path is not among the listed files.");
  }
  if (errors.length > 0) return { errors: [...new Set(errors)], newer: false, version: next ? manifest.version : null };
  const order = compareSemver(manifest.version, options.currentVersion);
  const preAllowed = options.allowPrerelease === true || (current?.pre.length ?? 0) > 0;
  const prerelease = (next?.pre.length ?? 0) > 0;
  const newer = order === 1 && (!prerelease || preAllowed);
  return { errors: [], newer, version: manifest.version };
}

/**
 * Parse `app-update.yml` (written into the packaged app by electron-builder when a publish target
 * exists). Throws for malformed metadata.
 */
export function parseAppUpdateYml(text) {
  const raw = parseSimpleYaml(text);
  const publisher = raw.publisherName;
  return {
    provider: String(raw.provider ?? ""),
    url: String(raw.url ?? ""),
    publisherName: Array.isArray(publisher) ? publisher.map(String).filter((p) => p !== "") : [],
  };
}

/**
 * Whether a packaged build may use the updater: HTTPS generic feed without credentials, and a pinned
 * publisher (the updater then verifies the installer's Authenticode publisher). An unsigned build
 * has no publisher, so it can never self-update.
 * @returns {{ enabled: boolean, reason: string | null, feedUrl: string | null }}
 */
export function evaluateUpdateConfig(config) {
  if (config.provider !== "generic") return { enabled: false, reason: "not_configured", feedUrl: null };
  let url;
  try {
    url = new URL(config.url);
  } catch {
    return { enabled: false, reason: "not_configured", feedUrl: null };
  }
  if (url.protocol !== "https:" || url.username !== "" || url.password !== "" || url.search !== "" || url.hash !== "") {
    return { enabled: false, reason: "insecure_feed", feedUrl: null };
  }
  if (config.publisherName.length === 0) return { enabled: false, reason: "unsigned_build", feedUrl: null };
  const base = url.toString().endsWith("/") ? url.toString() : `${url.toString()}/`;
  return { enabled: true, reason: null, feedUrl: base };
}

/** Free space the download needs: the installer plus working space and a safety margin. */
export function requiredFreeBytes(installerSize) {
  return installerSize * 2 + MIN_FREE_BYTES_MARGIN;
}

// ---- Manifest authentication (SEC-09) ---------------------------------------------------------
//
// `latest.yml` carries the version and the installer's SHA-512 but is plain text on an https host, so
// whoever can write to the feed could announce a newer version number for an OLDER, still validly
// code-signed installer (a rollback). The release tool therefore signs the exact bytes of `latest.yml`
// with a dedicated Ed25519 key (kept apart from the code-signing certificate); the app carries the
// matching public key and refuses a manifest whose signature does not verify. The installer is then
// bound to that signed manifest by comparing its SHA-512 after download.

export const MANIFEST_SIGNATURE_DOMAIN = "interview-assistant-update-manifest-v1\n";
export const MANIFEST_SIGNATURE_FILE = "latest.yml.sig";
export const MANIFEST_KEY_FILE = "update-manifest-key.txt";
const SIGNATURE_B64 = /^[A-Za-z0-9+/]{86}==$/; // 64 bytes, base64

function signingPayload(manifestText) {
  return Buffer.concat([Buffer.from(MANIFEST_SIGNATURE_DOMAIN, "utf8"), Buffer.from(String(manifestText), "utf8")]);
}

/** Parse a base64 SPKI Ed25519 public key. @returns the key object, or null when it is not one. */
export function parseManifestPublicKey(text) {
  try {
    const trimmed = String(text ?? "").trim();
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(trimmed) || trimmed.length > 200) return null;
    const key = createPublicKey({ key: Buffer.from(trimmed, "base64"), format: "der", type: "spki" });
    return key.asymmetricKeyType === "ed25519" ? key : null;
  } catch {
    return null;
  }
}

/** True only for a well-formed signature that verifies over exactly these manifest bytes. Never throws. */
export function verifyManifestSignature(manifestText, signatureText, publicKeyText) {
  try {
    const key = parseManifestPublicKey(publicKeyText);
    const signature = String(signatureText ?? "").trim().replace(/^ed25519:/, "");
    if (key === null || !SIGNATURE_B64.test(signature)) return false;
    return verify(null, signingPayload(manifestText), key, Buffer.from(signature, "base64"));
  } catch {
    return false;
  }
}

/** Sign the manifest text (release tooling only). @returns base64 signature. */
export function signManifest(manifestText, privateKeyPem) {
  const key = createPrivateKey(privateKeyPem);
  if (key.asymmetricKeyType !== "ed25519") throw new Error("The manifest signing key must be Ed25519.");
  return sign(null, signingPayload(manifestText), key).toString("base64");
}

/** A fresh key pair. `publicKey` is base64 SPKI (ship it in the app); `privateKeyPem` stays secret. */
export function generateManifestKeyPair() {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  return {
    publicKey: publicKey.export({ format: "der", type: "spki" }).toString("base64"),
    privateKeyPem: privateKey.export({ format: "pem", type: "pkcs8" }).toString(),
  };
}

/** The public key (base64 SPKI) that belongs to a private key PEM, or null when it is not Ed25519. */
export function publicKeyOfPrivate(privateKeyPem) {
  try {
    const key = createPrivateKey(privateKeyPem);
    if (key.asymmetricKeyType !== "ed25519") return null;
    return createPublicKey(key).export({ format: "der", type: "spki" }).toString("base64");
  } catch {
    return null;
  }
}
