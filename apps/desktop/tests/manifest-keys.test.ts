import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { generateToFile } from "../scripts/release/manifest-keys.mjs";
import { parseManifestPublicKey, publicKeyOfPrivate } from "../src/shared/update-manifest.mjs";

describe("manifest key generation (SEC-09)", () => {
  it("writes a private key file, returns the matching public key and never overwrites", () => {
    const dir = mkdtempSync(join(tmpdir(), "ia-keys-"));
    try {
      const file = join(dir, "manifest.pem");
      const publicKey = generateToFile(file);
      expect(parseManifestPublicKey(publicKey)).not.toBeNull();
      expect(publicKeyOfPrivate(readFileSync(file, "utf8"))).toBe(publicKey);
      expect(() => generateToFile(file)).toThrow(/already exists/);
      expect(publicKeyOfPrivate(readFileSync(file, "utf8"))).toBe(publicKey); // untouched
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
    expect(existsSync(dir)).toBe(false);
  });
});
