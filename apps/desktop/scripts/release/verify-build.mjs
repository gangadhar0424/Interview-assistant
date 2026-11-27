// Post-packaging verification (M-10, D-77). Fails (exit 1) unless the packaged Windows build matches
// package.json and, for a signed build, carries a valid timestamped SHA-256 Authenticode signature.
// On success it writes release/SHA256SUMS.txt and release/BUILD-KIND.txt (signed | unsigned-internal).
//
//   node scripts/release/verify-build.mjs [--mode signed|unsigned]    (default: REQUIRE_SIGNING)
//   node scripts/release/verify-build.mjs --check-sums                 (re-hash release/ against SHA256SUMS.txt)

import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import {
  compareSums,
  evaluateFuses,
  evaluateSignature,
  fileVersionMatches,
  expectedInstallerName,
  formatSums,
  isValidSemver,
  missingFiles,
  parseSums,
  peMachine,
  REQUIRED_FUSES,
  commonNameOf,
  signerConsistencyErrors,
  resolvePublisherName,
  resolveManifestPublicKey,
  resolveFeedUrl,
  sha256File,
  sha512Base64File,
  signingRequired,
  X64_MACHINE,
} from "./release-lib.mjs";
import { inspectSignature, inspectVersionInfo } from "./windows-inspect.mjs";
import {
  evaluateUpdateConfig,
  MANIFEST_KEY_FILE,
  MANIFEST_SIGNATURE_FILE,
  verifyManifestSignature,
  parseAppUpdateYml,
  parseLatestYml,
  validateManifest,
} from "../../src/shared/update-manifest.mjs";

const here = dirname(fileURLToPath(import.meta.url));
export const DESKTOP_ROOT = resolve(here, "..", "..");
const ENABLE = 49; // FuseState.ENABLE in @electron/fuses

function listFiles(dir, base = dir) {
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...listFiles(full, base));
    else found.push(relative(base, full).replace(/\\/g, "/"));
  }
  return found;
}

function readPeMachine(file) {
  const fd = readFileSync(file).subarray(0, 4096);
  return peMachine(fd);
}

async function fuseState(exe) {
  const { getCurrentFuseWire, FuseV1Options } = await import("@electron/fuses");
  const wire = await getCurrentFuseWire(exe);
  const actual = {};
  for (const name of Object.keys(REQUIRED_FUSES)) actual[name] = wire[FuseV1Options[name]] === ENABLE;
  return actual;
}

/**
 * @param {{ root?: string, requireSigned: boolean, now?: Date }} options
 * @returns {Promise<{ ok: boolean, checks: { name: string, ok: boolean, detail: string }[], warnings: string[], installer: string | null }>}
 */
export async function verifyBuild(options) {
  const root = options.root ?? DESKTOP_ROOT;
  const now = options.now ?? new Date();
  const checks = [];
  const warnings = [];
  const check = (name, errors, okDetail = "ok") => {
    checks.push({ name, ok: errors.length === 0, detail: errors.length === 0 ? okDetail : errors.join(" ") });
  };

  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  const productName = pkg.build?.productName ?? "";
  const version = pkg.version;
  check("version is a valid semantic version", isValidSemver(version) ? [] : [`"${String(version)}" is not valid.`], version);

  const releaseDir = join(root, "release");
  const installerName = expectedInstallerName(productName, version);
  const installer = join(releaseDir, installerName);
  const unpacked = join(releaseDir, "win-unpacked");
  const exe = join(unpacked, `${productName}.exe`);

  check("installer exists for this version", existsSync(installer) && statSync(installer).size > 1_000_000 ? [] : [`${installerName} is missing or too small.`], installerName);
  check("installer blockmap exists", existsSync(`${installer}.blockmap`) ? [] : ["The .blockmap file is missing."]);
  const stale = existsSync(releaseDir)
    ? readdirSync(releaseDir).filter((f) => /-Windows-x64\.exe$/.test(f) && f !== installerName)
    : [];
  check("no installers of another version in release/", stale.length === 0 ? [] : [`Stale artifacts: ${stale.join(", ")}.`]);

  if (!existsSync(unpacked)) {
    check("win-unpacked exists", ["release/win-unpacked is missing."]);
    return { ok: false, checks, warnings, installer: null };
  }
  const missing = missingFiles(listFiles(unpacked));
  check("expected packaged files", missing.length === 0 ? [] : [`Missing: ${missing.join(", ")}.`]);

  if (existsSync(exe) && existsSync(installer)) {
    // The NSIS installer is a small 32-bit stub by design; the application it installs must be x64.
    check("application executable is an x64 PE image", readPeMachine(exe) === X64_MACHINE ? [] : ["The application executable is not an x64 PE image."]);
    check("installer is a Windows PE executable", readPeMachine(installer) !== null ? [] : ["The installer is not a valid PE file."]);
    const info = await inspectVersionInfo(exe);
    const metaErrors = [];
    if (info.productName !== productName) metaErrors.push(`ProductName is "${info.productName}".`);
    if (!fileVersionMatches(info.fileVersion, version)) metaErrors.push(`FileVersion is "${info.fileVersion}", expected ${version}.`);
    check("application metadata matches package.json", metaErrors, `${info.productName} ${info.fileVersion}`);
    check("Electron fuses", evaluateFuses(await fuseState(exe)));

    let appSigner = null;
    let installerSigner = null;
    for (const [label, file] of [["installer", installer], ["application executable", exe]]) {
      const signature = await inspectSignature(file);
      if (file === exe) appSigner = signature.signerSubject ?? null;
      else installerSigner = signature.signerSubject ?? null;
      const verdict = evaluateSignature(signature, { requireSigned: options.requireSigned, now });
      warnings.push(...verdict.warnings.map((w) => `${label}: ${w}`));
      check(`${label} signature (${options.requireSigned ? "signed build" : "unsigned internal build"})`, verdict.errors, signature.status);
    }
    if (options.requireSigned) {
      // One identity signs the whole release, and it is the configured publisher when one is set.
      let pinned = null;
      try {
        pinned = resolvePublisherName(options.env ?? process.env);
      } catch {
        /* an invalid name is reported by the preflight */
      }
      check("installer and application share the configured signer", signerConsistencyErrors(installerSigner, appSigner, pinned));
    }
  }

  // Update metadata (M-11): present whenever a feed was built in; consistent with the real artifacts.
  const updateYml = join(unpacked, "resources", "app-update.yml");
  const latestYml = join(releaseDir, "latest.yml");
  let expectFeed = null;
  try {
    expectFeed = resolveFeedUrl(options.env ?? process.env);
  } catch {
    check("update feed configuration", ["UPDATE_FEED_URL is invalid."]);
  }
  const hasUpdateMetadata = existsSync(updateYml);
  if (expectFeed !== null && !hasUpdateMetadata) {
    check("update metadata is packaged", ["UPDATE_FEED_URL is set but app-update.yml is missing from the app."]);
  }
  if (hasUpdateMetadata) {
    const cfgErrors = [];
    let config = null;
    try {
      config = parseAppUpdateYml(readFileSync(updateYml, "utf8"));
    } catch {
      cfgErrors.push("app-update.yml is malformed.");
    }
    if (config) {
      const verdict = evaluateUpdateConfig(config);
      if (expectFeed !== null) {
        // Compare the packaged URL itself: an unsigned build has no "enabled" feed to compare.
        let packaged = null;
        try {
          const u = new URL(config.url);
          packaged = u.toString().endsWith("/") ? u.toString() : `${u.toString()}/`;
        } catch {
          /* an unparseable URL is reported as a mismatch below */
        }
        if (packaged !== expectFeed) cfgErrors.push("app-update.yml points at a different feed than UPDATE_FEED_URL.");
      }
      if (options.requireSigned && !verdict.enabled) cfgErrors.push(`A signed build must be able to update itself (${verdict.reason}).`);
      if (!options.requireSigned && verdict.enabled) cfgErrors.push("An unsigned build must not carry a pinned publisher.");
      if (options.requireSigned && verdict.enabled) {
        const cn = commonNameOf(appSigner);
        if (cn === null || !config.publisherName.includes(cn)) {
          cfgErrors.push("The pinned publisher name does not equal the signing certificate's Common Name: every update would fail verification.");
        }
      }
    }
    check("update configuration in the app", cfgErrors, "app-update.yml");
    const manErrors = [];
    if (!existsSync(latestYml)) manErrors.push("latest.yml is missing from release/.");
    else {
      try {
        const manifest = parseLatestYml(readFileSync(latestYml, "utf8"));
        const verdict = validateManifest(manifest, { currentVersion: "0.0.0", allowPrerelease: true });
        manErrors.push(...verdict.errors);
        if (manifest.version !== version) manErrors.push(`latest.yml version ${manifest.version} is not ${version}.`);
        if (manifest.path !== installerName && manifest.files[0]?.url !== installerName) manErrors.push("latest.yml does not name this installer.");
        if (existsSync(installer)) {
          const actual = await sha512Base64File(installer);
          const entry = manifest.files[0];
          if (manifest.sha512 !== actual || entry?.sha512 !== actual) manErrors.push("latest.yml SHA-512 does not match the installer.");
          if (entry?.size !== statSync(installer).size) manErrors.push("latest.yml size does not match the installer.");
        }
      } catch {
        manErrors.push("latest.yml is malformed.");
      }
    }
    check("update manifest matches the installer", manErrors, "latest.yml");
  }

  // SEC-09: a signed build with a feed must carry the manifest key and ship a signature that verifies.
  const sigFile = join(releaseDir, MANIFEST_SIGNATURE_FILE);
  if (options.requireSigned && expectFeed !== null) {
    const sigErrors = [];
    let pinned = null;
    try {
      pinned = resolveManifestPublicKey(options.env ?? process.env);
    } catch {
      /* reported below as a missing key */
    }
    if (pinned === null) sigErrors.push("UPDATE_MANIFEST_PUBLIC_KEY is not set to a valid Ed25519 public key.");
    const keyInApp = join(unpacked, "resources", MANIFEST_KEY_FILE);
    if (!existsSync(keyInApp)) sigErrors.push(`${MANIFEST_KEY_FILE} is missing from the packaged app: it could never authenticate an update.`);
    else if (pinned !== null && readFileSync(keyInApp, "utf8").trim() !== pinned) {
      sigErrors.push("The manifest key inside the app is not the configured UPDATE_MANIFEST_PUBLIC_KEY.");
    }
    if (!existsSync(sigFile) || !existsSync(latestYml)) sigErrors.push(`${MANIFEST_SIGNATURE_FILE} or latest.yml is missing from release/.`);
    else if (pinned !== null && !verifyManifestSignature(readFileSync(latestYml, "utf8"), readFileSync(sigFile, "utf8"), pinned)) {
      sigErrors.push("The signature does not verify latest.yml with the configured manifest key.");
    }
    check("update manifest is signed with the pinned key", sigErrors, MANIFEST_SIGNATURE_FILE);
  }

  const index = join(root, "out", "renderer", "index.html");
  if (existsSync(index)) {
    const html = readFileSync(index, "utf8");
    const csp = /style-src[^;"]*/.exec(html)?.[0] ?? "";
    check("renderer CSP has no unsafe-inline styles", csp.includes("'self'") && !csp.includes("unsafe-inline") ? [] : ["style-src is missing or relaxed."]);
  } else {
    check("renderer CSP has no unsafe-inline styles", ["out/renderer/index.html is missing (run the build)."]);
  }

  const ok = checks.every((c) => c.ok);
  if (ok) {
    const names = [installerName, `${installerName}.blockmap`];
    if (existsSync(latestYml)) names.push("latest.yml");
    if (existsSync(sigFile)) names.push(MANIFEST_SIGNATURE_FILE);
    const entries = [];
    for (const name of names) entries.push({ name, sha256: await sha256File(join(releaseDir, name)) });
    writeFileSync(join(releaseDir, "SHA256SUMS.txt"), formatSums(entries), "utf8");
    writeFileSync(join(releaseDir, "BUILD-KIND.txt"), options.requireSigned ? "signed\n" : "unsigned-internal\n", "utf8");
  }
  return { ok, checks, warnings, installer: ok ? installer : null };
}

/** Re-hash the files named in SHA256SUMS.txt; returns the names that differ or are missing. */
export async function checkSums(root = DESKTOP_ROOT) {
  const releaseDir = join(root, "release");
  const file = join(releaseDir, "SHA256SUMS.txt");
  if (!existsSync(file)) throw new Error("release/SHA256SUMS.txt is missing.");
  const expected = parseSums(readFileSync(file, "utf8"));
  if (expected.length === 0) throw new Error("release/SHA256SUMS.txt lists no files.");
  const actual = {};
  for (const { name } of expected) {
    const path = join(releaseDir, name);
    actual[name] = existsSync(path) ? await sha256File(path) : undefined;
  }
  return compareSums(expected, actual);
}

async function main(argv) {
  if (argv.includes("--check-sums")) {
    const bad = await checkSums();
    if (bad.length > 0) {
      console.error(`CHECKSUM MISMATCH: ${bad.join(", ")}`);
      return 1;
    }
    console.log("Checksums match.");
    return 0;
  }
  const modeIndex = argv.indexOf("--mode");
  const mode = modeIndex >= 0 ? argv[modeIndex + 1] : signingRequired(process.env) ? "signed" : "unsigned";
  if (mode !== "signed" && mode !== "unsigned") {
    console.error('--mode must be "signed" or "unsigned".');
    return 2;
  }
  const result = await verifyBuild({ requireSigned: mode === "signed" });
  for (const c of result.checks) console.log(`${c.ok ? "PASS" : "FAIL"}  ${c.name}: ${c.detail}`);
  for (const w of result.warnings) console.warn(`WARN  ${w}`);
  if (!result.ok) {
    console.error("\nBuild verification FAILED. This build must not be released.");
    return 1;
  }
  console.log(mode === "signed" ? "\nSigned build verified. SHA256SUMS.txt written." : "\nUNSIGNED internal build verified (not for distribution). SHA256SUMS.txt written.");
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (error) => {
      console.error(`Verification could not run: ${error instanceof Error ? error.message : "unknown error"}`);
      process.exit(1);
    },
  );
}
