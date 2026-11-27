import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Static guards on the CI and release workflows (M-10, D-77). They cannot prove that GitHub accepts
// the files (run actionlint and a real dispatch for that); they pin the security properties.
const WORKFLOWS = resolve(__dirname, "..", "..", "..", ".github", "workflows");
const read = (name: string): string => readFileSync(join(WORKFLOWS, name), "utf8");
const all = readdirSync(WORKFLOWS).filter((f) => f.endsWith(".yml"));

describe("every workflow", () => {
  it.each(all)("%s pins every action to a full commit SHA", (file) => {
    const uses = [...read(file).matchAll(/^\s*-?\s*uses:\s*(\S+)/gm)].map((m) => m[1] as string);
    expect(uses.length).toBeGreaterThan(0);
    for (const ref of uses) expect(ref, `${file}: ${ref}`).toMatch(/^[\w.-]+\/[\w./-]+@[0-9a-f]{40}$/);
  });

  it.each(all)("%s declares least-privilege permissions at the top level", (file) => {
    const text = read(file);
    expect(text).toMatch(/^permissions:\s*\n\s+contents:\s*read\s*$/m);
    expect(text).not.toMatch(/^permissions:\s*write-all/m);
  });
});

describe("ci.yml", () => {
  const ci = read("ci.yml");

  it("never references a secret (it runs for pull requests)", () => {
    expect(ci).not.toMatch(/secrets\./);
    expect(ci).not.toMatch(/pull_request_target/);
  });

  it("builds and verifies the Windows package unsigned, and labels the artifact", () => {
    expect(ci).toMatch(/runs-on:\s*windows-latest/);
    expect(ci).toMatch(/scripts\/release\/package-release\.mjs/);
    expect(ci).toMatch(/REQUIRE_SIGNING:\s*"false"/);
    expect(ci).toMatch(/name:\s*desktop-UNSIGNED-internal-/);
    expect(ci).toMatch(/--check-sums/);
  });

  it("installs from lockfiles", () => {
    expect(ci).toMatch(/npm ci/);
    expect(ci).toMatch(/uv sync --frozen/);
  });
});

describe("desktop-release.yml", () => {
  const release = read("desktop-release.yml");

  it("can only be started by hand, never by a push or a pull request", () => {
    const on = /^on:\s*\n([\s\S]*?)^\S/m.exec(release)?.[1] ?? "";
    expect(on).toMatch(/workflow_dispatch:/);
    expect(on).not.toMatch(/push:|pull_request|pull_request_target|schedule:|workflow_run/);
  });

  it("requires an allow-listed actor, the main branch and a matching version", () => {
    expect(release).toMatch(/github\.ref != 'refs\/heads\/main'/);
    expect(release).toMatch(/DESKTOP_RELEASE_ACTORS/);
    expect(release).toMatch(/nobody is authorized/);
    expect(release).toMatch(/Requested version does not equal/);
  });

  it("keeps signing secrets behind a protected environment, only in the signing job", () => {
    expect(release).toMatch(/environment:\s*desktop-release/);
    const authorize = /^  authorize:[\s\S]*?(?=^  build-sign:)/m.exec(release)?.[0] ?? "";
    expect(authorize.length).toBeGreaterThan(0);
    expect(authorize).not.toMatch(/secrets\./);
    expect(release).not.toMatch(/\$\{\{\s*secrets\.[A-Z_]+\s*\}\}[^\n]*echo/i);
  });

  it("enforces signing, re-verifies checksums, removes the certificate and never publishes", () => {
    expect(release).toMatch(/REQUIRE_SIGNING:\s*"true"/);
    expect(release).toMatch(/--check-sums/);
    expect(release).toMatch(/if:\s*always\(\)[\s\S]*Remove-Item/);
    expect(release).toMatch(/if-no-files-found:\s*error/);
    expect(release).not.toMatch(/gh release|create-release|softprops|action-gh-release|contents:\s*write/);
  });

  it("does not let a cancelled signing run be cancelled half-way by a newer one", () => {
    expect(release).toMatch(/cancel-in-progress:\s*false/);
  });

  it("does not persist git credentials in the checkout", () => {
    expect(release).toMatch(/persist-credentials:\s*false/);
  });
});

// ---- SEC-09: the release workflow supplies the manifest keys and cleans the private key up ----

describe("manifest signing in the release workflow (SEC-09)", () => {
  const text = readFileSync(join(__dirname, "../../../.github/workflows/desktop-release.yml"), "utf8");

  it("takes the private key from a secret, the public key from a variable", () => {
    expect(text).toContain("secrets.UPDATE_MANIFEST_SIGNING_KEY");
    expect(text).toContain("vars.UPDATE_MANIFEST_PUBLIC_KEY");
    expect(text).not.toMatch(/vars\.UPDATE_MANIFEST_SIGNING_KEY/);
  });

  it("deletes the temporary private key even when the build fails and publishes the signature", () => {
    const cleanup = text.slice(text.indexOf("Delete the temporary certificate"));
    expect(cleanup).toContain("if: always()");
    expect(cleanup).toContain("manifest-signing.pem");
    expect(text).toContain("apps/desktop/release/latest.yml.sig");
  });
});
