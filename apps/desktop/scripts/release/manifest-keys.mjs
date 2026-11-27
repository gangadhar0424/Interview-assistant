// Generate the Ed25519 key pair that authenticates update manifests (SEC-09).
//
//   node scripts/release/manifest-keys.mjs generate <private-key-file>
//
// The private key is written to <private-key-file> (refuses to overwrite an existing file) and is NEVER
// printed. Store its contents as the GitHub secret UPDATE_MANIFEST_SIGNING_KEY and keep an offline
// backup: losing it means shipping a new build that pins a new key. The PUBLIC key is printed; set it
// as the GitHub variable UPDATE_MANIFEST_PUBLIC_KEY. Keep this key separate from the code-signing
// certificate: compromising one must not compromise the other.

import { existsSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { generateManifestKeyPair } from "../../src/shared/update-manifest.mjs";

export function generateToFile(file) {
  const target = resolve(file);
  if (existsSync(target)) throw new Error("That file already exists; it will not be overwritten.");
  const { publicKey, privateKeyPem } = generateManifestKeyPair();
  writeFileSync(target, privateKeyPem, { encoding: "utf8", mode: 0o600, flag: "wx" });
  return publicKey;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [command, file] = process.argv.slice(2);
  if (command !== "generate" || !file) {
    console.error("Usage: node scripts/release/manifest-keys.mjs generate <private-key-file>");
    process.exit(2);
  }
  try {
    const publicKey = generateToFile(file);
    console.log("Private key written (not shown). Public key (set as UPDATE_MANIFEST_PUBLIC_KEY):");
    console.log(publicKey);
  } catch (error) {
    console.error(error instanceof Error ? error.message : "Could not generate the key pair.");
    process.exit(1);
  }
}
