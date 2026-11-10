# Windows Desktop Release Process

Status: **process documented in Phase 10B; no signed or distributed release exists.** Code signing and auto-update are **not implemented** and need owner approval (see "Decisions requiring owner approval" below). All commands run from `apps/desktop` in PowerShell.

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
| Signing (future) | Build machine only: `CSC_LINK`/`CSC_KEY_PASSWORD` or a cloud-signing configuration | Never committed. Not configured today. |

Windows secure storage (DPAPI via Electron `safeStorage`) must be available; otherwise the app refuses to persist the refresh token and the user must sign in each launch.

## 4. Unsigned versus signed

| | Unsigned (today) | Signed (future, needs approval) |
|---|---|---|
| Authenticode | `NotSigned` (verified) | Valid publisher signature + timestamp |
| SmartScreen / browsers | "Windows protected your PC" / unknown publisher warnings | Warnings reduce as reputation builds; depends on certificate type |
| Allowed use | Developer and owner testing; a small trusted group that has been told to expect the warning | Any distribution beyond that |
| Auto-update | Not possible to do safely | Prerequisite (the updater must verify the publisher) |

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
