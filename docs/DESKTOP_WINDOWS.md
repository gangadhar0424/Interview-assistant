# Windows Desktop Development

## Requirements

- Windows 10/11 x64
- Node.js 22+ and npm
- The existing backend running and reachable
- A user account and a prepared, ready session in the web app

The desktop MVP does not require an API/provider key on the desktop. Configure AI provider credentials only on the backend.

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
npm run package:win
```

The NSIS installer is written under `apps/desktop/release/`. It is an unsigned development package and must not be distributed as a production release. Code signing and auto-update are deferred.

## Authentication and data handling

- Sign in using the existing backend account. The app can also create an account through the existing signup endpoint.
- Access credentials remain in the Electron main process. The refresh credential is encrypted by Electron `safeStorage` using Windows OS-backed protection.
- The renderer does not receive tokens and cannot make arbitrary network requests through the preload bridge.
- Questions and selected-session materials are sent to the configured backend. Do not configure an untrusted backend.
- No audio, screenshot, or other background capture is included.

## Dependency audit note

The checked production dependency set has no npm audit findings. The development/build dependency tree currently reports six **moderate** transitive advisories in the Electron Builder/minimatch chain; there are no high or critical findings. The suggested automated fix would downgrade Electron Builder across a major version, so it was not applied blindly. Reassess that dependency chain before distributing installers.

