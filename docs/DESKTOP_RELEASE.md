# Windows Desktop Release Process

Status: **M-10 signing and verification pipeline implemented (section 0); no signed or distributed release exists because no certificate has been provided.** Auto-update is covered by M-11. The sections after 0 are the earlier Phase 10B process and the D-50 options. All commands run from `apps/desktop` in PowerShell.

## 0. M-10 release pipeline (D-77): signing, verification and CI

Status (2026-11-22): **pipeline implemented and tested; no certificate exists, so no signed release has been produced.** An unsigned build is verified end to end. Signing with real credentials is **BLOCKED on the owner actions in section 0.6**. Nothing is published by any script or workflow.

### 0.1 Commands (from `apps/desktop`)

| Command | What it does |
|---|---|
| `npm run package:win:release` | Preflight, build, package and verify. With `REQUIRE_SIGNING=true` it is a **signed** release build; otherwise an **UNSIGNED internal** build. |
| `npm run verify:release` | Verifies an existing `release/` folder (add `-- --mode signed` for a signed build) and writes `release/SHA256SUMS.txt` and `release/BUILD-KIND.txt` on success. |
| `node scripts/release/verify-build.mjs --check-sums` | Re-hashes `release/` against `SHA256SUMS.txt`; non-zero on any mismatch or missing file. |
| `npm run package:win` | The older unsigned path (no verification). Internal testing only. |

### 0.2 What `package-release` does and refuses

1. **Preflight (before anything is touched):** `package.json` version is valid semver; the timestamp URL is a valid `http(s)` URL without credentials; with `REQUIRE_SIGNING=true`, exactly one complete signing configuration exists (certificate: `CSC_LINK` + `CSC_KEY_PASSWORD`; or Azure managed signing: `AZURE_TRUSTED_SIGNING_ENDPOINT`, `_ACCOUNT`, `_PROFILE`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`). A missing variable is reported **by name only**, never by value. Both methods at once is refused.
2. **Certificate check (certificate method, file path):** the PFX must open with the password, be inside its validity period, have a private key, be valid for code signing (EKU 1.3.6.1.5.5.7.3.3), and chain to a **trusted** root. A self-signed, expired or wrong-password certificate stops the release (exit 2). Expiry within 30 days produces a renewal warning.
3. **Clean, build, package:** old `release/` output is deleted first so a stale artifact can never pass verification. Signed builds use electron-builder with `forceCodeSigning`, **SHA-256** digests and an **RFC 3161 timestamp** (`SIGN_TIMESTAMP_URL`, default `http://timestamp.digicert.com`; the timestamp token is itself signed, so plain HTTP is the service's normal endpoint). Tools run through `node` with fixed arguments.
4. **Verify (`verify-build`), all must pass:** valid version; installer and blockmap exist for exactly this version; no installer of another version in `release/`; required packaged files; the application executable is an **x64** PE image (the NSIS installer is a 32-bit stub by design, checked only as a valid PE); product name and file version equal `package.json`; all six Electron **fuses** in their release state; renderer CSP has no `unsafe-inline` styles; Authenticode of installer and application executable: signed build => `Valid` + timestamped + `sha256` (digest read with `signtool verify /pa /v`), unsigned build => `NotSigned`.
5. **On success only:** `SHA256SUMS.txt` (installer and blockmap) and `BUILD-KIND.txt` (`signed` or `unsigned-internal`). A failed verification writes neither.

### 0.3 Signed versus unsigned is explicit

`BUILD-KIND.txt` records the kind; CI names its artifact `desktop-UNSIGNED-internal-<sha>`; the release workflow names its artifact `desktop-signed-<version>` and can only produce it if verification passed in signed mode. Unsigned output prints `UNSIGNED internal build (not for distribution)`.

### 0.4 CI and the protected release workflow

* `.github/workflows/ci.yml`: unchanged triggers (push to `main`, pull requests); every action is now pinned to a full commit SHA; new job `desktop-windows` (`windows-latest`): `npm ci`, typecheck, full `npm test`, **unsigned** `package-release`, checksum re-verification, and an artifact retained for 7 days. It never references a secret.
* `.github/workflows/desktop-release.yml`: **manual only**. Gates: the run must be on `main`; the actor must be in the repository variable `DESKTOP_RELEASE_ACTORS` (JSON array of logins; **unset means nobody can run it**); the version input must equal `apps/desktop/package.json`; the signing job runs in the GitHub **environment** `desktop-release` (configure required reviewers there; the secrets live there, not at repository level). It sets `REQUIRE_SIGNING=true`, deletes the temporary certificate file even on failure, re-verifies checksums, uploads the artifact (30 days) and **publishes nothing**; the owner publishes by hand after reviewing the artifact. Concurrency never cancels a signing run half-way.
* `.github/workflows/desktop-dev-release.yml` (D-86): on every push to `main` that changes `apps/desktop/**` or `packages/protocol/**` (or by hand), builds the **unsigned** installer exactly as `ci.yml` does and replaces the assets of the GitHub **pre-release** `dev-latest`. Always-latest link: `https://github.com/gangadhar0424/Interview-assistant/releases/download/dev-latest/InterviewAssistant-Setup.exe` (checksum in `InterviewAssistant-Setup.exe.sha256`). No secrets, no signing, no update feed; only its publish job has `contents: write`. Development only: SmartScreen warns, and dev builds do not auto-update. This replaces the manual copy into `apps/web/public/downloads` for the shared link.
* `.github/workflows/security.yml` (existing): CodeQL, gitleaks, `pip-audit`, `npm audit` (production dependencies gate at high/critical).
* Tests that pin these properties: `tests/release-workflow.test.ts`.

### 0.5 Failure behaviour (all automated unless marked)

| Situation | Result | Test |
|---|---|---|
| No certificate / missing variable / both methods / bad timestamp URL | exit 2 before any build; names the variable only | `release-scripts.test.ts`, `release-lib.test.ts` |
| Certificate file missing | exit 2 | `release-scripts.test.ts` |
| Wrong certificate password | exit 2, password never echoed | real PFX, Windows |
| Expired certificate | exit 2 | real PFX, Windows |
| Untrusted (self-signed) chain | exit 2 | real PFX, Windows |
| Invalid version metadata | preflight and verify fail | unit |
| Packaging or build failure | non-zero exit, no artifacts claimed; stale output already removed | by construction (exit codes are propagated); not injected |
| Release artifact missing / stale installer / missing packaged files | verification fails, no checksums written | `release-scripts.test.ts` |
| Checksum mismatch / file missing / malformed checksum file | `--check-sums` exits 1 | `release-scripts.test.ts` |
| Signature not Valid, no timestamp, SHA-1 digest, unsigned artifact in a signed build | verification fails | unit |
| Timestamp server unreachable during signing | electron-builder fails the signing step, the job fails | **not injected** (needs the real signing step) |
| Interruption / CI cancellation | child process stopped, exit 130, partial artifacts are never accepted because the next run cleans and verifies; handler logic unit-tested, a real signal on Windows was not exercised | unit only |
| Unauthorized release dispatch | job fails in `authorize` | **not testable without GitHub**; workflow guards are pinned by a static test |

### 0.6 Owner actions required before a signed release can exist

1. Choose and obtain a signing method (D-50 options): Azure managed signing (recommended in D-50 if eligible), or an OV/EV certificate whose key may be used from CI (modern CAs require a hardware or cloud HSM, which changes the CI step; the workflow's PFX path assumes an exportable certificate).
2. Create the GitHub environment `desktop-release` with **required reviewers**, and store there: `WINDOWS_SIGNING_PFX_BASE64` and `WINDOWS_SIGNING_PFX_PASSWORD` (certificate method), or `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` (secrets) and `AZURE_TRUSTED_SIGNING_ENDPOINT`, `_ACCOUNT`, `_PROFILE` (variables). Configure exactly one method.
3. Set the repository variable `DESKTOP_RELEASE_ACTORS` (for example `["login1","login2"]`) and optionally `SIGN_TIMESTAMP_URL`.
4. Run the workflow once and confirm `Get-AuthenticodeSignature` reports `Valid` with a timestamp on the downloaded installer, then run the clean-VM install, upgrade-over and uninstall checks in section 5.
5. Protect `main` (required reviews and status checks) so only reviewed code reaches the signing job.
6. **Update-manifest keys (SEC-09).** For a signed build with an update feed, generate the Ed25519 pair (`node scripts/release/manifest-keys.mjs generate <file>`), store the private key as the `desktop-release` environment secret `UPDATE_MANIFEST_SIGNING_KEY` and the public key as the repository variable `UPDATE_MANIFEST_PUBLIC_KEY`. `package-release` refuses a signed build with a feed unless both exist and match, signs `latest.yml` into `latest.yml.sig`, and `verify-build` checks that the signature verifies against the key shipped inside the app. Details and limits: `docs/DESKTOP_UPDATES.md` section 11.
7. **Signing secrets are scoped (SEC-17).** The build and verification steps run with the signing credentials removed from their environment; only the electron-builder step receives them.

### 0.7 Certificate renewal and expiry

Track the certificate's `NotAfter` date: preflight warns at 30 days and refuses an expired certificate. Renew before expiry (certificate methods need a new PFX in the environment secret; Azure methods renew in the service). **Already-released installers stay valid after the certificate expires because of the RFC 3161 timestamp**; new builds need a valid certificate. On suspected key compromise: revoke with the CA/service, remove the secret, rotate, rebuild and publish a higher version, and withdraw the old installer.

### 0.8 What was verified in this repository (2026-11-22)

* Unsigned end-to-end run of `npm run package:win:release` on Windows: all verification checks passed.
* Signing machinery demonstrated with a **throw-away self-signed certificate** (removed afterwards): electron-builder signed the installer and the application executable with SHA-256 and a real RFC 3161 timestamp from the default service; Windows reported `UnknownError` (untrusted root) and the verifier correctly **rejected** it as a signed build. A trusted `Valid` result was **not** obtained (no certificate): **BLOCKED**.
* GitHub workflows were YAML-parsed and checked by `tests/release-workflow.test.ts`; they have **not** been run on GitHub and actionlint was not run.

## 1. Production build

```powershell
npm ci
npm run typecheck
npm test
npm run build          # tsc --noEmit + electron-vite build -> out/
```

`out/renderer/index.html` must contain `style-src 'self'` (no `'unsafe-inline'`). The relaxed policy exists only in the Vite dev server (`electron.vite.config.ts`, `apply: "serve"`).

## 2. Create the package

```powershell
# Unsigned (development / internal testing only):
$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
npm run package:win
```

Output in `release/` (git-ignored): `Interview Assistant-<version>-Windows-x64.exe` (NSIS installer), its `.blockmap`, and `win-unpacked/` (the same `app.asar` the installer contains; useful for smoke tests without installing).

`electron-builder` applies the Electron fuses from `package.json` (`build.electronFuses`) after packaging. Verify them on every release candidate:

```powershell
npx @electron/fuses read --app "release/win-unpacked/Interview Assistant.exe"
```

Expected: `RunAsNode` Disabled, `EnableCookieEncryption` Enabled, `EnableNodeOptionsEnvironmentVariable` Disabled, `EnableNodeCliInspectArguments` Disabled, `EnableEmbeddedAsarIntegrityValidation` Enabled, `OnlyLoadAppFromAsar` Enabled. `GrantFileProtocolExtraPrivileges` stays **Enabled** (turning it off breaks startup; see the Phase 10B report).

The app icon is generated from project-owned artwork by `scripts/generate-icon.mjs` (`npm run icon`) into `build/icon.png` and `build/icon.ico`; there are no third-party assets.

## 3. Required environment configuration

| Item | Where | Notes |
|---|---|---|
| Backend URL | Desktop **Settings**, or `INTERVIEW_API_URL` in the user's environment before first launch | Default `http://127.0.0.1:8000`. Any non-loopback URL **must be `https://`** (enforced). Changing it signs the user out. |
| AI provider keys | Backend only | The desktop app never holds provider keys. |
| Backend CORS / ticket settings | Backend `.env` | The desktop main process calls the REST/SSE API directly; no renderer origin is involved. |
| Signing | CI environment secrets or the build machine environment only: `CSC_LINK` + `CSC_KEY_PASSWORD`, or the `AZURE_*` variables (section 0) | Never committed; `REQUIRE_SIGNING=true` makes a missing configuration fail the build. |

Windows secure storage (DPAPI via Electron `safeStorage`) must be available; otherwise the app refuses to persist the refresh token and the user must sign in each launch.

## 4. Unsigned versus signed

| | Unsigned (today) | Signed (future, needs approval) |
|---|---|---|
| Authenticode | `NotSigned` (verified) | Valid publisher signature + timestamp |
| SmartScreen / browsers | "Windows protected your PC" / unknown publisher warnings | Warnings reduce as reputation builds; depends on certificate type |
| Allowed use | Developer and owner testing; a small trusted group that has been told to expect the warning | Any distribution beyond that |
| Auto-update | Off by design: the app refuses to start the updater without a pinned publisher (`docs/DESKTOP_UPDATES.md`) | Possible once signed with `UPDATE_FEED_URL` and `SIGN_PUBLISHER_NAME` set |

Do not distribute the unsigned installer outside trusted testers.

## 5. Installer testing (run on a clean Windows VM or spare account)

Status: **the NSIS installer has not been executed by Phase 10A or 10B.** Only `win-unpacked` was launched.

1. Record the SHA-256 of the installer; confirm `Get-AuthenticodeSignature` status matches expectation.
2. Install per-user (the NSIS config is `perMachine: false`, `oneClick: false`, directory selectable). Confirm Start Menu and Desktop shortcuts, icon, and an entry in *Apps & features*.
3. Launch from the shortcut: window appears, tray icon appears, "Backend connected" with the API reachable.
4. Run `docs/DESKTOP_MANUAL_CHECKLIST.md`.
5. Install over the previous version; confirm settings and sign-in survive.
6. Uninstall from *Apps & features*; confirm shortcuts and the install directory are removed.

## 6. Rollback and uninstall considerations

- **Rollback = reinstall the previous installer.** Keep every released installer and its SHA-256. Installing an older version over a newer one is allowed by the NSIS installer; there is no in-app rollback.
- **User data survives uninstall.** The app data folder (by default `%APPDATA%\interview-assistant-desktop`, verify on the packaged build) holds `settings.json` and `refresh-token.bin` (DPAPI-encrypted, only readable by the same Windows user). The default NSIS uninstaller does not delete it. Users handing off a machine should **sign out first** (deletes the token file) or delete that folder.
- Settings are validated on load; an invalid `settings.json` produces the "could not start" dialog rather than a silent reset. A rollback to an older build must not change the settings schema (it has not so far).
- Before any release: back-end API compatibility must be maintained for the previous desktop version for as long as that version may still be installed (there is no forced-update mechanism).

---

# Decisions requiring owner approval

Both items below are **proposals**. Nothing here is implemented, and nothing here is binding until the owner accepts it in `docs/DECISIONS.md` (D-50 tracks them as *Proposed*).

## A. Windows code signing

### Options

| Option | What it is | Pros | Cons / prerequisites |
|---|---|---|---|
| A1. OV (organization-validated) code-signing certificate | Certificate from a public CA, validated against the legal entity | Lowest cost of the CA options; works with `signtool`/electron-builder | Business identity verification; since 2023 CAs require the private key on FIPS-validated hardware (token or cloud HSM), so CI signing needs a cloud key service or a physical token; SmartScreen reputation builds gradually |
| A2. EV (extended-validation) certificate | Stricter vetting, hardware-bound key | Strongest identity assurance | Highest cost and process overhead; do not assume it still removes SmartScreen warnings immediately (Microsoft's policy has changed; confirm current behaviour before paying for it) |
| A3. Managed cloud signing from Microsoft (Azure "Trusted Signing" / its successor name) | Microsoft-hosted signing with short-lived certificates; the key is never exported | No hardware token, good CI fit, usually the cheapest | Eligibility depends on organization/region and identity validation; vendor lock-in to the service; confirm current availability and name |
| A4. Stay unsigned | Status quo | No cost | Warnings for every user; blocks safe auto-update; unacceptable for external distribution |

### Prerequisites (all options)
A legal entity or verifiable individual identity; a decision on who owns the private key; a CI secret-management plan (the key or the signing-service credential never enters the repository); RFC 3161 timestamping so signatures outlive the certificate; sign both the application executable and the installer/uninstaller (electron-builder does this when configured), after the fuses are written.

### Security implications
Whoever can sign can ship code to every installed user, so signing credentials are the most sensitive release asset: restrict to the release pipeline, require review before tagging, log every signing operation, and have a revocation/rotation runbook. Signing proves publisher identity and integrity of the file; it does not prove the code is safe.

### Recommended approach
1. Keep **unsigned builds for internal testing only** until the owner approves a path.
2. Prefer **A3 (managed cloud signing)** if the organization is eligible, because the key never leaves the provider and CI can sign without a token; otherwise **A1 (OV) with a cloud-HSM-held key**. Revisit EV only if the SmartScreen benefit is confirmed against current Microsoft documentation.
3. Add signing to the pipeline with `electron-builder`'s Windows signing configuration, verify with `Get-AuthenticodeSignature` in the release checklist, and add a "signed and timestamped" gate before publishing.

**Owner decisions needed:** legal entity/identity, budget, who holds signing access, and approval to enrol with a provider.

## B. Auto-update architecture

### Options

| Option | What it is | Pros | Cons / prerequisites |
|---|---|---|---|
| B1. Manual update (no updater) | Users download and run the new installer | Smallest attack surface; nothing new to build | Users lag behind security fixes; relies on signed installer + published hashes |
| B2. `electron-updater` against a static HTTPS host | Installer-based updates; metadata file (`latest.yml`) and installers served over HTTPS | Standard with electron-builder (blockmap differential downloads already generated); simple hosting | **Requires signed builds** (the Windows updater verifies the publisher signature); the update host becomes a high-value target; needs hosting, retention and release automation |
| B3. GitHub Releases as the feed | `electron-updater` GitHub provider | No hosting to run | Private repos need a distributed token (not acceptable in a desktop client); public repos expose release artifacts; ties distribution to GitHub |
| B4. MSIX / Microsoft Store | OS-managed updates | Store handles signing and updates | New packaging format, Store onboarding, policy review; a larger change than this phase |

### Security implications
An update channel is a remote-code-execution path to every client. Required controls: HTTPS only; publisher-signature verification by the updater (never disable it); no downgrade to an older version unless explicitly chosen; the feed host has restricted write access with an audit trail; release artifacts are built by the pipeline, not by hand; a way to withdraw a bad release quickly (remove it from the feed, publish a fixed higher version); update checks must not send interview content, tokens or identifiers beyond the app version. The updater must run in the main process only; the renderer gets no update capability beyond an optional "update available" notice through the existing bridge pattern.

### Recommended approach
1. Do **B1 first** (signed installer, published SHA-256 and release notes) once signing is approved.
2. Then add **B2** on a dedicated HTTPS static host, only with signed builds, with a staged rollout (internal testers first, then everyone), a documented withdrawal procedure, and an automated test that an unsigned or tampered update is rejected.
3. Defer B3 and B4 unless the organization already uses them.

**Owner decisions needed:** signing path (A) first, hosting location and who administers it, update cadence, and whether a minimum-supported-version check against the backend is wanted (this would be a small backend change and needs its own approval).
