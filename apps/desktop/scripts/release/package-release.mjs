// Release packaging entry point (M-10, D-77).
//
//   REQUIRE_SIGNING=true  -> signed release: preflight must pass, electron-builder runs with forced
//                            code signing, and the result must verify as signed. Any failure exits non-zero.
//   otherwise             -> UNSIGNED internal build (never for distribution), verified as unsigned.
//
// Secrets (CSC_LINK, CSC_KEY_PASSWORD, AZURE_*) come from the environment only. Nothing is read from,
// or written to, a file in the repository, and nothing secret is printed.

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import {
  builderConfig,
  cleanReleaseDir,
  evaluateCertificate,
  interruptHandler,
  preflight,
  resolveFeedUrl,
  resolveManifestPublicKey,
  scrubbedEnv,
} from "./release-lib.mjs";
import {
  MANIFEST_SIGNATURE_FILE,
  publicKeyOfPrivate,
  signManifest,
  verifyManifestSignature,
} from "../../src/shared/update-manifest.mjs";
import { inspectCertificate } from "./windows-inspect.mjs";
import { DESKTOP_ROOT } from "./verify-build.mjs";

const log = (message) => console.log(`[release] ${message}`);
const fail = (message, code = 2) => {
  console.error(`[release] ERROR: ${message}`);
  return code;
};

let activeChild = null;
const onSignal = (signal) => {
  const code = interruptHandler(activeChild, log)(signal);
  process.exit(code);
};
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.on("SIGINT", () => onSignal("SIGINT"));
  process.on("SIGTERM", () => onSignal("SIGTERM"));
}

function runShellStep(label, commandLine, env) {
  return runStep(label, commandLine, [], env, true);
}

function runStep(label, command, args, env, shell = false) {
  return new Promise((resolveStep) => {
    log(`${label}...`);
    const child = spawn(command, args, { cwd: DESKTOP_ROOT, env, stdio: "inherit", shell, windowsHide: true });
    activeChild = child;
    child.on("error", (error) => {
      activeChild = null;
      console.error(`[release] ${label} could not start: ${error.message}`);
      resolveStep(1);
    });
    child.on("exit", (code, signal) => {
      activeChild = null;
      resolveStep(signal ? 1 : (code ?? 1));
    });
  });
}

async function main() {
  const pkg = JSON.parse(readFileSync(join(DESKTOP_ROOT, "package.json"), "utf8"));
  const env = { ...process.env };
  const plan = preflight(env, pkg.version);
  for (const warning of plan.warnings) console.warn(`[release] WARNING: ${warning}`);
  if (!plan.ok) {
    for (const error of plan.errors) console.error(`[release] ERROR: ${error}`);
    return 2;
  }
  const signed = plan.kind === "signed";

  if (signed && plan.mode === "pfx") {
    const file = env.CSC_LINK;
    if (file && !/^(https?:|data:)/i.test(file) && existsSync(file)) {
      let facts;
      try {
        facts = await inspectCertificate(resolve(file), env.CSC_KEY_PASSWORD ?? "");
      } catch {
        return fail("The signing certificate could not be inspected.");
      }
      if (facts.error) return fail("The signing certificate could not be opened (wrong password or corrupt file).");
      const verdict = evaluateCertificate(facts, new Date());
      for (const warning of verdict.warnings) console.warn(`[release] WARNING: ${warning}`);
      if (verdict.errors.length > 0) {
        for (const error of verdict.errors) console.error(`[release] ERROR: ${error}`);
        return 2;
      }
    } else if (file && !/^(https?:|data:)/i.test(file) && !/^[A-Za-z0-9+/=\r\n]{200,}$/.test(file)) {
      return fail("CSC_LINK does not point to an existing certificate file.");
    }
  }

  cleanReleaseDir(DESKTOP_ROOT);
  const nodeBin = process.execPath;
  // The project's own build script (type-check + electron-vite build). A fixed command string with
  // no external input, so running it through a shell is safe; it is required for npm on Windows.
  // Signing credentials are NOT given to the build or verification steps (SEC-17): only the
  // electron-builder step below receives them.
  const plainEnv = scrubbedEnv(env);
  let code = await runShellStep("build (type-check + electron-vite)", "npm run build", plainEnv);
  if (code !== 0) return fail("Build failed.", code);

  // SEC-09: with a feed on a signed build, the app must carry the key that authenticates its update
  // manifests, and the release must be able to sign latest.yml with the matching private key.
  const feed = resolveFeedUrl(env);
  const publicKey = signed && feed !== null ? resolveManifestPublicKey(env) : null;
  let manifestPrivateKey = null;
  if (publicKey !== null) {
    try {
      manifestPrivateKey = readFileSync(resolve(env.UPDATE_MANIFEST_SIGNING_KEY_FILE ?? ""), "utf8");
    } catch {
      return fail("UPDATE_MANIFEST_SIGNING_KEY_FILE could not be read.");
    }
    if (publicKeyOfPrivate(manifestPrivateKey) !== publicKey) {
      return fail("UPDATE_MANIFEST_SIGNING_KEY_FILE does not match UPDATE_MANIFEST_PUBLIC_KEY (or is not an Ed25519 key).");
    }
  }

  const tmp = mkdtempSync(join(tmpdir(), "ia-builder-"));
  try {
    const configFile = join(tmp, "builder-config.json");
    let manifestKeyFile = null;
    if (publicKey !== null) {
      manifestKeyFile = join(tmp, "update-manifest-key.txt");
      writeFileSync(manifestKeyFile, `${publicKey}\n`, "utf8");
    }
    writeFileSync(configFile, JSON.stringify(builderConfig(pkg.build, env, plan.mode, manifestKeyFile)), "utf8");
    const builderEnv = { ...env };
    if (!signed) builderEnv.CSC_IDENTITY_AUTO_DISCOVERY = "false";
    const builderCli = join(DESKTOP_ROOT, "node_modules", "electron-builder", "cli.js");
    code = await runStep("package (electron-builder)", nodeBin, [builderCli, "--win", "nsis", "--config", configFile], builderEnv);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
  if (code !== 0) return fail("Packaging failed.", code);

  if (manifestPrivateKey !== null && publicKey !== null) {
    const latest = join(DESKTOP_ROOT, "release", "latest.yml");
    if (!existsSync(latest)) return fail("release/latest.yml was not produced, so it cannot be signed.");
    const manifestText = readFileSync(latest, "utf8");
    const signature = signManifest(manifestText, manifestPrivateKey);
    if (!verifyManifestSignature(manifestText, signature, publicKey)) return fail("The manifest signature did not verify.");
    writeFileSync(join(DESKTOP_ROOT, "release", MANIFEST_SIGNATURE_FILE), `${signature}\n`, "utf8");
    log("latest.yml signed (Ed25519).");
  }

  const verify = join(DESKTOP_ROOT, "scripts", "release", "verify-build.mjs");
  code = await runStep("verify build", nodeBin, [verify, "--mode", signed ? "signed" : "unsigned"], plainEnv);
  if (code !== 0) return fail("Build verification failed. Do not release these artifacts.", code);
  log(signed ? "Signed release build complete and verified." : "UNSIGNED internal build complete and verified (not for distribution).");
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then(
    (code) => process.exit(code),
    (error) => {
      console.error(`[release] ERROR: ${error instanceof Error ? error.message : "unexpected failure"}`);
      process.exit(1);
    },
  );
}
