# SEC-08 manual runbook: browser to installed desktop handoff (with challenge binding)

Prepared 2026-11-24. **Nothing in this document has been run.** Every result cell is NOT TESTED until a person performs the row with a real browser, the real `interviewassistant://` handler and the installed app, and writes the result. The automated checks (desktop, web and API tests; `handoff-e2e.mjs` 11 of 11) do not count towards this.

Why it is still needed: the automated checks use fakes or scripted clients. They cannot show that Windows routes the link to the installed app, that the browser's "Open app?" prompt behaves, or that the app reads the challenge from the real loopback beacon. See D-83 and `docs/SECURITY_ASSESSMENT_PHASE_E.md`.

This runbook covers the **binding** added for SEC-08. The general web-to-desktop scenarios (account switching, capture-active switching, sign-out) stay in `docs/DESKTOP_MANUAL_CHECKLIST.md`, section "Web to desktop handoff"; run them too, but they do not exercise the challenge.

## 0. Setup (about 20 minutes)

| # | Step | Done |
|---|---|---|
| S1 | Build the installer from the current tree (`apps/desktop`: `npm run package:win`), uninstall any older build, install the new one. Older builds do not send a challenge. | ☐ |
| S2 | Start Postgres and Redis (`docker compose up -d --wait`), apply migrations, start the API and the worker, and start the web app. | ☐ |
| S3 | **Run the API twice, once per mode, and say which mode each row used.** Mode **U** (default for local development): `HANDOFF_REQUIRE_CHALLENGE=false`. Mode **R** (the production default): `HANDOFF_REQUIRE_CHALLENGE=true`. Restart the API after changing it. | ☐ |
| S4 | Chrome or Edge with DevTools (Network tab, Preserve log). Two accounts, A and B, each with a ready session. | ☐ |
| S5 | A folder `SEC08-evidence` for screenshots, named by row ID (for example `H4-replay.png`). Never save a screenshot that shows a full handoff code. | ☐ |

Terms: the **link** is `interviewassistant://start?h=<43 characters>&c=<43 characters>`. `h` is the one-time code (lives 60 s). `c` is the challenge (a SHA-256 hash). The **verifier** is the secret the desktop keeps in memory; it must never appear in any URL, log, file, DevTools panel or screen.

## 1. Test rows

Run each row in mode **R** unless it says otherwise.

| ID | Scenario | Steps | You must observe | Result (mode, date, who) |
|---|---|---|---|---|
| H1 | Desktop already running, signed in or not | Launch the installed app and leave it open. On the web (signed in as A) press **Start Interview** on a ready session. Allow the browser prompt. | The app comes to the front and shows READY for that session. DevTools shows one request for `…/ia-presence/challenge` to `127.0.0.1:47821` (200, body only `{challenge, expires_in}`) and one `POST …/handoff` whose request body contains a `code_challenge`. | NOT TESTED |
| H2 | Window states while running | Repeat H1 with the window visible, hidden to the tray and minimised. | The same single window is restored and focused each time. One tray icon. | NOT TESTED |
| H3a | Desktop closed, mode **U** | Quit the app from the tray menu. Press **Start Interview**. | The page shows the "not detected" notice. Choose **open it anyway**: Windows launches the app and it shows READY (unbound flow, no challenge). | NOT TESTED |
| H3b | Desktop closed, mode **R** | Quit the app from the tray. Press **Start Interview**, then **open it anyway**. | **Expected from reading the code, to be confirmed here:** the app cannot supply a challenge because it is not running, so the server refuses the unbound request (422) and the page says "Please update the desktop app", which would be misleading. Record exactly what you see. If confirmed, this is finding SEC-34: cold start does not work when binding is required. Workaround while open: start the app first, then press Start Interview (H1). | NOT TESTED |
| H4 | Replay of a used link | After a successful H1, copy the link from DevTools (Network, the navigation or the request payload only for your own account) and open it again from Win+R. | The app shows the generic "invalid, expired or already used" notice. Nobody is signed in or switched because of it. | NOT TESTED |
| H5 | Expired link | Press **Start Interview** and leave the browser's "Open app?" prompt unanswered for more than 60 seconds (the code lifetime), then allow it. Repeat once leaving it for more than 120 seconds (the verifier lifetime). | The generic "invalid, expired or already used" notice in both cases. Nobody is signed in because of it. | NOT TESTED |
| H6 | Invalid challenge in the link | In Win+R type a link with a valid-looking `h` copied from a fresh Start Interview but with the `c` value changed by one character. Then one with `c` removed. | Both are refused with the generic notice. With `c` changed, the code is not redeemed at all (no sign-in change). With `c` removed in mode R the server refuses the unbound redemption. | NOT TESTED |
| H7 | Link built by someone else | In Win+R type `interviewassistant://start?h=<43 random characters>&c=<43 random characters>`. Then a link with extra parameters, with an overlong value, and with a bad character. | Each is ignored with the generic notice. The app does not crash or change state. | NOT TESTED |
| H8 | Cancel and retry | With **capture running** (Settings, Audio input on, Listen: microphone), press **Start Interview** for a second session and choose **Keep current capture** in the native dialog. Then press **Start Interview** again and choose **Switch session**. | Keeping: recording continues and the declined notice shows. Retrying with a fresh press works: READY for the new session, nothing starts recording. The declined attempt never silently signs in. | NOT TESTED |
| H9 | Interrupted attempts | Press **Start Interview** five times quickly, allowing only the last prompt. | Only the last attempt takes effect (the others are superseded or fail with the generic notice); no stuck state; no duplicate windows. | NOT TESTED |
| H10 | Another web page cannot read your session | While the app is running, open a page from a different site, such as a local test page you make, and run `fetch("http://127.0.0.1:47821/ia-presence/challenge")` from its console. Do it 40 times. | You get `{challenge, expires_in}` for the first requests, then 429 (30 a minute). You never see a verifier, token, user or version. While you flood it, a legitimate **Start Interview** may fail once and works after a minute: this is SEC-32 (interruption, not takeover). | NOT TESTED |
| H11 | Sensitive values absent (see section 2) | During H1 and H4: inspect every place listed in section 2. | None of the forbidden values appear. | NOT TESTED |
| H12 | Server refuses unbound codes (mode **R** only) | In DevTools, replay the `POST …/handoff` request without `code_challenge` (Copy as fetch, remove the field). | `422`. No code is issued. | NOT TESTED |

## 2. What must not appear (row H11)

| Place | Allowed | Forbidden |
|---|---|---|
| Browser address bar and history | the page URL only | the code, the challenge or any `interviewassistant://` text after the launch |
| DevTools Network | the one-time code (response of `POST …/handoff`) and the challenge hash | the **verifier** (anywhere), any access or refresh token in a URL |
| Desktop **Settings, Support, Copy diagnostics** (paste into a text editor) | event names | the code, the challenge, the verifier, tokens |
| API console and log file | request path and status, IDs | the code, the challenge, the verifier, passwords, tokens (search the log for the first 8 characters of the code you used) |
| Files in `%APPDATA%\Interview Assistant` | settings and the sign-in file written as before | the verifier or a handoff code (search the folder for the first 8 characters of the code) |
| Windows clipboard and Run dialog history after the test | nothing from the app | a code you did not paste yourself |

Searching tip (PowerShell): `Select-String -Path "$env:APPDATA\Interview Assistant\*" -Pattern "<first 8 characters>" -Recurse`. Use the first 8 characters only; do not paste a full code into notes.

## 3. Recording the outcome

1. Fill in the Result column and keep the evidence folder.
2. If H3b shows the misleading message, record it as confirmed SEC-34 and decide between the options in `docs/SECURITY_ASSESSMENT_PHASE_E.md` (section "Post-M15 closure").
3. SEC-08 may be marked closed only when H1, H3a, H4, H5, H6, H8, H10, H11 and H12 are recorded as passed and H3b has been decided. Anything not run stays NOT TESTED.
