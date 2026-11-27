# Desktop automatic updates (M-11, D-78)

Status (2026-11-22): **implemented and automatically tested. Not validated against a real signed release: BLOCKED on the owner actions in section 9**, because the updater is deliberately inert in unsigned builds and no signing certificate or update host exists yet. Nothing here has been published.

## 1. Design in one paragraph

The installed app asks a static **https** feed for a small `latest.yml`, validates it, and tells the user when a newer version exists. The user then chooses **Download**, and later **Restart and install**. Downloading and installing are handled by `electron-updater` (the maintained updater that matches the project's `electron-builder` packaging). Before it runs an installer, `electron-updater` checks the file's SHA-512 against the manifest and its **Authenticode publisher against the name pinned into the build**. The app adds its own gates around that, described below. There is no custom installer, no custom download code and no custom signature code.

## 2. Why unsigned builds never update themselves

`electron-updater` skips signature verification when the build has no pinned `publisherName` (verified in `node_modules/electron-updater/out/NsisUpdater.js`: `verifySignature` returns `null`, meaning accepted, when none is configured). An updater in that state would run any file whose hash matches a manifest an attacker could also replace. So the app **refuses to start the updater** unless the build contains an `app-update.yml` with an https feed **and** a pinned publisher (`evaluateUpdateConfig`). The Settings page tells a tester plainly: "This is an unsigned test build. It cannot update itself; install new versions manually."

| Build | Update behaviour |
|---|---|
| Development (`npm run dev`) | Off ("development") |
| Unsigned installer, with or without a feed | Off ("unsigned_build" or "not_configured") |
| Feed is not https, or has credentials/query | Off ("insecure_feed") |
| `INTERVIEW_DISABLE_UPDATE_CHECK=1` | Off ("disabled_by_environment") |
| Signed installer with https feed and pinned publisher | On |

## 3. Architecture

| Piece | File | Role |
|---|---|---|
| Rules (shared by app and release tooling) | `src/shared/update-manifest.mjs` | Strict manifest and config parsing, semver, safe artifact names, downgrade rule, policy |
| State machine | `src/main/updater.ts` (`UpdateManager`) | Lifecycle, retries, concurrency, watchdogs; talks only to ports |
| Library adapter | `src/main/updater-engine.ts` | The only module that imports `electron-updater`; loaded lazily; fixed safe configuration |
| OS side | `src/main/updater-io.ts` | Reads `app-update.yml`, fetches the manifest (https, no redirects, size cap, timeout), measures free disk |
| IPC | `src/main/ipc.ts`, `src/preload/index.ts` | Four argument-free channels plus one event |
| UI | `src/renderer/src/ui/UpdatePanel.tsx`, store slice | Banner and a Settings section; mirrors main's state, decides nothing |
| Build side | `scripts/release/*` | Writes the feed and pinned publisher into the build and verifies them |

## 4. Lifecycle

`disabled` (with a reason) or `idle` -> `checking` -> `none` (up to date) or `available` -> `downloading` (percent) -> `downloaded` -> `installing` -> the app quits and the installer runs. Any step can end in `error` (fixed code and fixed text; never a raw message, URL or path).

* A background check runs after a 30 second start-up delay and then about every six hours (plus up to ten minutes of jitter). It fetches only the manifest. It never downloads.
* **Download** and **Restart and install** are user actions. `autoDownload` and `autoInstallOnAppQuit` are off.
* Only one operation runs at a time. A second request while one runs returns the current state. Repeated checks inside a minimum interval (10 s manual, 30 min automatic) are ignored. Engine events that arrive out of order or twice are ignored. A downloaded update is never replaced by a new check.
* Each state is emitted once; the UI cannot show two contradictory notices.

## 5. Security controls

1. **Pinned feed, https only.** The feed URL comes from the build, not from user settings. https is required; credentials, query and fragment are refused; redirects are refused; the manifest is capped at 64 KiB; no cookies; generic User-Agent (the feed learns the app version and nothing else).
2. **Strict manifest.** A deliberately tiny reader (no YAML library; anchors, tags, block scalars, flow collections, duplicate keys and control characters are errors). Version must be valid semver; file names must be plain `.exe` names (no separators, traversal, scheme or encoded tricks); SHA-512 must be 64 bytes of base64; size must be 1 B to 500 MiB.
3. **No downgrade, no same-version reinstall, no pre-release** for a stable build. `allowDowngrade` and `allowPrerelease` are off in the engine as well.
4. **Announcement must match.** The engine's own announcement and its downloaded version must equal the validated manifest, or the update is dropped (`version_mismatch`). A silent engine is a failure, never a success.
5. **Publisher and checksum verification** by `electron-updater` before "downloaded" is reported; our code does not override `verifyUpdateCodeSignature`. A source test fails if that or `setFeedURL`, `requestHeaders`, `allowInsecure` appear.
6. **Renderer has no capability** beyond four channels that take no arguments; every transition is decided in main by state. The updater lives in the main process only.
7. **No capture.** The update code never touches audio or screenshots. Installing first runs the same cleanup as closing the window (stops listening, drops held screenshots, aborts running answers), and nothing restarts them. The restart that follows is a new process: audio and screenshot input default to off and need their normal consent (D-53, D-51).
8. **Build-time consistency** (`verify-build`): `app-update.yml` points at `UPDATE_FEED_URL`; a signed build's pinned publisher equals the signing certificate's Common Name (a mismatch would make every update fail); `latest.yml` names the installer and its SHA-512 and size match the real file.

## 6. Reliability

| Situation | Behaviour |
|---|---|
| No internet, DNS failure, connection reset | `network`; automatic checks retry at most 3 times (60 s, 120 s, 240 s plus up to 10 % jitter), then wait for the next periodic check; manual checks never auto-retry |
| Server error or 404/429/5xx | `feed_unavailable` (same bounded retries) |
| Slow server | manifest 15 s timeout (plus a 5 s outer bound), engine check 30 s; `timeout` |
| Interrupted or stalled download | no progress for 90 s: the engine is cancelled and the manager is released even if the engine ignores cancellation; `timeout`, retryable by the user |
| Not enough disk space | checked before downloading (installer x 2 + 100 MiB); `insufficient_disk_space`; the engine's own ENOSPC is mapped to the same |
| Corrupted download or wrong checksum | engine refuses; `verification_failed`; nothing installable |
| Invalid signature or wrong publisher | engine refuses; `verification_failed` |
| Invalid manifest or malformed version | `manifest_invalid`; never retried; the engine is not asked |
| Server unavailable | as above |
| Restart or installer launch fails | 45 s watchdog or a thrown error: `install_failed`; the user can retry **without downloading again** |
| Stopping capture before install fails | the install does not proceed (`install_failed`) |
| Application shutdown during download | the partial download is the library's cache file, not the installed app; the next run starts clean |
| Concurrent update events | ignored by state guards |

## 7. Settings and user data

Updates do not touch `settings.json`, `refresh-token.bin` or any session data. The project sets `nsis.deleteAppDataOnUninstall: true`; during an update the installer calls the old uninstaller with `--updated`, which makes the NSIS template skip that deletion. `tests/updater-guards.test.ts` reads the installed `app-builder-lib` template and fails if a library upgrade changes this, and checks the project setting still exists (so the guard keeps meaning something). **Not yet confirmed on a real update**: see section 9.

## 8. Rollback and recovery (what is and is not guaranteed)

* **There is no automatic rollback.** NSIS replaces the installed files; `electron-updater` cannot restore the previous version if an installer fails part-way. This document does not claim otherwise.
* **What is protected:** the downloaded file is verified before it is run; the current installation is not modified until the installer runs; a failed download, verification or restart leaves the installed version in place and reports an error.
* **If an installation fails or the app will not start:** reinstall the previous installer (keep every released installer and `SHA256SUMS.txt`); installing an older version over a newer one is allowed by the installer. User data is not removed by an install-over.
* **If a bad release is published:** remove it from the feed (delete or replace `latest.yml`), publish a fixed, higher version. Clients that already downloaded the bad version still hold it until they restart or check again.
* **Disable updates fleet-wide:** remove `latest.yml` from the feed (clients report "feed unavailable" and keep working), or ship a build without a feed.
* **Disable on one computer:** set the environment variable `INTERVIEW_DISABLE_UPDATE_CHECK=1`.

## 9. Owner actions and what is BLOCKED

Required before the updater can be exercised for real:

1. A signing method and a first signed release (`docs/DESKTOP_RELEASE.md` section 0.6).
2. An https static host for the feed. Publish `latest.yml`, the installer and its `.blockmap` to one folder; restrict write access to the release process; keep an audit trail. The release workflow does **not** upload anything: the owner publishes the verified artifact.
3. Generate the manifest-signing key pair and set the variables and secret in section 11. Repository variables for the release workflow: `UPDATE_FEED_URL` (for example `https://updates.example.com/interview-assistant/`) and `SIGN_PUBLISHER_NAME` (exactly the certificate's Common Name).
4. Then, on a clean Windows VM: install version N, publish N+1, press **Check for updates**, **Download**, **Restart and install**; confirm the app restarts on N+1, you are still signed in, settings are unchanged, and nothing is recording. Repeat with a tampered installer and with a feed that returns a lower version, and confirm both are refused.

| Check | Status |
|---|---|
| Policy, manifest, lifecycle, retries, concurrency, disk, checksum and signature failures, install failure, restart failure | Automated-tested with a fake engine and clock (`updater.test.ts`, `update-manifest.test.ts`) |
| IPC shape, installing stops listening first and never restarts it | Automated-tested (`audio-ipc.test.ts`, `updater-guards.test.ts`) |
| Real `latest.yml` / `app-update.yml` produced by electron-builder parse and verify | Verified on a real unsigned build (2026-11-22) |
| Release verifier rejects a mismatched feed, checksum, size, version, missing manifest | Automated-tested (`release-scripts.test.ts`) |
| Real download, real signature verification, real installation and restart | **BLOCKED** (needs a signed build and an https host) |
| Settings and sign-in preserved across a real update | **BLOCKED** (design verified from the installer template only) |
| Tampered installer refused by the real updater | **BLOCKED** (the refusal is the library's; our side is tested with a fake) |
| Insufficient disk, interrupted download, crash during install on a real machine | **Not exercised** (simulated only) |
| Update button and banner in the real UI | **Not exercised manually** (state and wiring tested; no component test) |

## 10. Known limitations

* No persisted on/off setting yet; the only switch is the environment variable. A Settings toggle would change the settings schema and was left out to avoid touching every settings consumer in this milestone.
* Update checks are metadata-only but do reveal the computer's IP address and the app version to the feed host. Choose a host accordingly; there is no telemetry.
* Differential (blockmap) downloads are left at the library default; they are covered by the same SHA-512 and signature checks.
* The updater ignores channels; there is a single `latest` feed.

## 11. Manifest authentication and rollback protection (SEC-09, D-82)

**The gap this closes.** `latest.yml` states the new version and the installer's SHA-512, but it is plain text on an https host. Whoever could write to the feed could announce version `99.0.0` for an older installer that is still validly code-signed (a rollback to a vulnerable build): the library checks the SHA-512 and publisher, both taken from the same unauthenticated feed.

**What was added.**

1. The release tool signs the exact bytes of `latest.yml` with a dedicated **Ed25519** key (separate from the code-signing certificate, so compromising one does not compromise the other) and publishes `latest.yml.sig` next to it. The signed message carries a fixed domain prefix (`interview-assistant-update-manifest-v1`), so the signature cannot be reused for anything else.
2. The installer carries the matching **public key** in `resources/update-manifest-key.txt`.
3. On every check the app fetches both files and verifies the signature **before reading a single field**. A missing, malformed or wrong signature ends in the fixed "could not be verified" state and the engine is never asked to look at the feed.
4. The version is then checked as before (newer only; same version and older are never offered). Because the version is now authenticated, an attacker cannot make an old build look new, and replaying an old signed manifest only shows an older version, which is ignored.
5. After the download the app hashes the installer file the library saved and compares it with the SHA-512 in the **signed** manifest. A mismatch (for example an older signed installer served under the new version) is refused: the state becomes an error and installing is not possible. (The rejected file is not deleted by the app, whose file-system use is deliberately limited to a short allow-list; it can never be installed and the next verified download replaces it.)
6. A signed build with a feed but **no** manifest key never self-updates (fail closed), and a signed release cannot be built without both keys (preflight and the build verifier check, including that the signature verifies against the key inside the packaged app).

**Recovery without downgrades.** There is still no automatic rollback and none is claimed. To withdraw a bad release, publish a **newer** version (for example 1.2.1 after a bad 1.2.0), signed as usual. Downgrades are never offered.

**What it does not protect against.** Compromise of the manifest-signing private key (rotate by shipping a build that pins a new public key: users on old builds need a manual install); a freeze attack (serving an old but genuine signed manifest to stop users learning of a new version); a compromised release pipeline that signs a malicious build. The code-signing publisher check remains the check on the installer itself.

**Owner actions (BLOCKED until done).**

1. `node scripts/release/manifest-keys.mjs generate <private-key-file>` (from `apps/desktop`, on a trusted machine). It writes the private key to the file (it never prints it) and prints the public key.
2. Store the private key as the GitHub environment secret `UPDATE_MANIFEST_SIGNING_KEY` (environment `desktop-release`) and keep an offline backup in your secret manager; set the public key as the repository variable `UPDATE_MANIFEST_PUBLIC_KEY`. Delete the local private-key file.
3. Publish `latest.yml.sig` with the other files (the workflow already uploads it as an artifact; the feed upload is a manual step today).
4. Validate on real infrastructure (NOT TESTED until then): a signed N to N+1 update succeeds; a feed with a tampered `latest.yml`, a missing or wrong `latest.yml.sig`, and an older signed installer under a newer version are each refused by the real updater.

**Evidence in this repository (2026-11-23).** 19 regression tests in `updater.test.ts` (tampered manifest, tampered hash, wrong key, swapped signature, malformed signatures, missing signature file, replay of older and current manifests, rollback installer, missing or unreadable file, legitimate path, recovery by newer version), 6 signature primitive tests in `update-manifest.test.ts`, release tooling tests in `release-lib.test.ts`, `release-workflow.test.ts` and `manifest-keys.test.ts`. Mutation check: switching the signature check off fails 8 tests and switching the hash check off fails 3. A real `electron-builder` directory build placed the public key in `resources/update-manifest-key.txt` byte for byte. The real updater against a real feed was not run.
