# Windows Desktop Development

## Requirements

- Windows 10/11 x64
- Node.js 22+ and npm
- The existing backend running and reachable
- A user account and a prepared, ready session in the web app

The desktop MVP does not require an API/provider key on the desktop. Configure AI provider credentials only on the backend.

> **Troubleshooting:** if the app exits immediately with `bad option: --remote-debugging-port` (or similar), the environment variable `ELECTRON_RUN_AS_NODE=1` is set (it is inherited from some VS Code-hosted terminals) and makes Electron behave like plain Node. Unset it (`Remove-Item Env:ELECTRON_RUN_AS_NODE` in PowerShell) before launching.

## Run in development

From the repository root in PowerShell:

```powershell
cd apps/desktop
npm ci
npm run dev
```

If the API is local, the default backend URL is `http://127.0.0.1:8000`. Otherwise open **Settings** in the desktop app and enter the API origin. Production and non-loopback backend URLs require HTTPS. The setting is stored locally; changing it clears the desktop sign-in.

Run desktop checks from the same directory:

```powershell
npm run typecheck
npm test
npm run build
```

## Create a local Windows package

```powershell
cd apps/desktop
npm ci
$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"   # unsigned local build
npm run package:win
```

The NSIS installer is written under `apps/desktop/release/`. It is an unsigned development package and must not be distributed as a production release. Code signing and auto-update are deferred pending owner approval. The full release process, fuse verification, installer testing and rollback notes are in [DESKTOP_RELEASE.md](./DESKTOP_RELEASE.md).

Release builds apply Electron fuses (see D-49). `ELECTRON_RUN_AS_NODE`, `NODE_OPTIONS` and `--inspect` are not honoured by the packaged app. `npm run dev` is unaffected, but note that the dev server (only) allows inline styles; the packaged CSP is `style-src 'self'`.

## Authentication and data handling

- Sign in using the existing backend account. The app can also create an account through the existing signup endpoint.
- Access credentials remain in the Electron main process. The refresh credential is encrypted by Electron `safeStorage` using Windows OS-backed protection.
- The renderer does not receive tokens and cannot make arbitrary network requests through the preload bridge.
- Questions and selected-session materials are sent to the configured backend. Do not configure an untrusted backend.
- No audio, screenshot, or other background capture is included.

## Dependency audit note

The checked production dependency set has no npm audit findings. The development/build dependency tree reports six **moderate** transitive findings that are one advisory (GHSA-g3hv-75wg-m249, `minimatch` DoS) propagated through the Electron Builder chain; there are no high or critical findings and none affect the packaged app. `npm audit fix --force` would downgrade Electron Builder across a major version, and a `minimatch@11` override cascades into packages that need the minimatch 3 API, so neither was applied. Re-run `npm audit` after each Electron Builder update. Details: [PHASE_10B_REPORT.md](./PHASE_10B_REPORT.md).
