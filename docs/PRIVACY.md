# Privacy and Data Handling

## Data handled

The application stores account email addresses, password hashes, refresh-token hashes, uploaded
resumes/documents, extracted text and embeddings, job descriptions and company details in sessions,
and questions and generated answers. Uploaded files live on the configured persistent filesystem;
metadata, text, embeddings, sessions, and Q&A live in PostgreSQL. Embeddings can disclose
information about their source and are treated as user data, not anonymous telemetry.

Logs and metrics are operational data. They may contain request IDs, resource IDs, route/status,
provider IDs, fixed error codes, durations, and normalized WebSocket close classes. They must not
contain credentials, password values/hashes, provider tokens, prompts, document or answer content,
job-page bodies, screenshots, or raw audio. Metrics labels are low-cardinality and must never
include user/session IDs or request-provided text.

## Retention and deletion

- Resume/document files and their extracted text, chunks, and embeddings remain until the user
  deletes the file. The delete path removes the stored file and related database records.
- A session can be deleted by its owner; its session-owned questions and answers cascade. Selected
  resumes/documents are references and are not deleted with the session.
- There is no automatic expiration policy for uploaded files, sessions, or Q&A at present.
- **Session export (M-9, D-74):** a user can download one of their own sessions (details, the job
  description they entered, preferences, questions and answers) as JSON or Markdown. It is
  produced on request, never stored or sent anywhere by the service, and excludes resume and
  document text, credentials and account data. The desktop app writes it only to a path the user
  chooses in a native dialog after a warning; the web app saves it through the browser after a
  warning. A saved file is outside the service's control and is not removed when the session is
  deleted. No transcripts, audio or screenshots are stored, so none can be exported.
- Refresh tokens are expired/revoked by application behavior; a scheduled cleanup policy for old
  token rows is not currently implemented.
- Account-wide deletion, data export, and automated cleanup/reconciliation of orphaned files are
  not implemented. Define and approve the deletion SLA and backup aging policy before a public
  launch.
- Backups retain deleted information until the backup expires. Restore procedures must preserve
  access controls and must not silently restore deleted records into a live account.

The above describes current implementation and does not constitute a legal retention promise.
Retention periods, account deletion, and applicable GDPR/DPDP or other obligations require owner
and legal review.

## Storage, transport, and encryption

- Use TLS for browser/API, WebSocket, PostgreSQL, and Redis connections outside local development.
- Encrypt persistent storage volumes and backups using platform-managed encryption and tightly
  scoped access. The application does not currently encrypt document text or answer fields with
  an application-level envelope key.
- Use an encrypted persistent volume for `FILE_STORAGE_ROOT`; restrict filesystem permissions to
  the API/worker service identities. Back up files together with PostgreSQL.
- Keep JWT, database, Redis, and AI-provider credentials in a secret manager or protected runtime
  environment. Rotate them using deployment-specific procedures; never put them in the web bundle.

## Tenant isolation

The API derives ownership from the authenticated user, uses user-scoped repositories, and returns
not-found responses for foreign-owned resources. PostgreSQL RLS is additionally enabled and forced
for user-owned tables by migration `0008_tenant_rls`. Runtime queries set a transaction-local
tenant identity under the restricted `interview_app_runtime` role. Cross-tenant maintenance uses
separate credentials and must not be used by request handlers. Validate actual role grants in each
environment; an incorrectly privileged database login can undermine the intended defense.

## Third-party AI handling

When a real LLM provider is enabled, the application sends the prompt and selected supporting
context needed to answer the user's question. The API key remains server-side. The system cannot
guarantee provider-side retention, training, or deletion behavior; review the specific account's
contract, data-processing terms, region, retention controls, and training settings before sending
real interview material. Do not enable a provider until the data owner approves those terms.
Local fake providers do not establish the privacy behavior or quality of a live provider.

## Audio, transcription, and screenshots

Screenshot input (Phase 11A, backend only so far): the API can convert a user-submitted PNG into question text. The image is held in memory for the request only, is never written to disk, a database, a log, a metric or an error message, and is discarded after extraction; the extracted text is not stored until the user asks it as a question. Only a deterministic fake vision provider exists; a real vision provider would receive everything visible in the screenshot, so it needs its own data-use, retention and cost review before it is enabled (`VISION_ENABLED_PROVIDERS` is empty by default). The desktop capture (Phase 11B) is opt-in (off by default, native confirmation to enable), starts only on an explicit Capture with a cancellable countdown, shows a preview before anything is sent, keeps the image in memory only (wiped after extraction, cancellation, a minute of inactivity, sign-out, session change, hiding the window and quit) and never logs or stores it. Audio capture is not implemented. No raw audio is
currently stored. Any future audio feature must require explicit user initiation, a persistent
visible capture indicator, and clear consent from all people whose speech is captured, consistent
with local law and interview-platform rules. Prefer ephemeral audio processing and do not retain raw
audio by default. Any future transcript/screenshot feature must specify purpose, storage, access,
retention, deletion, third-party processing, and user consent before implementation.

## Acceptable-use acknowledgement (M-1, D-68)

Before the desktop app will listen to audio or capture a screenshot, the signed-in user must accept an
acceptable-use notice: they are responsible for following the rules of any interview, meeting or
assessment where they use the app; they will capture or process audio and screenshots only with
permission (including that of other people whose voice or screen may be captured) and as the law
requires; and they will not use the app in ways those rules prohibit. The notice also states that the
app always shows when it is listening or capturing, does not hide itself and must not be used to avoid
monitoring. The wording is a policy statement for the owner to approve, not legal advice.

- **Enforcement is in the Electron main process.** `AudioService` and `CaptureService` refuse to start
  (and the screenshot display picker refuses) with the code `acknowledgement_required` until it exists,
  whatever the renderer shows. Acceptance is recorded only after a native dialog opened by the main
  process; the renderer has no channel that can record it (the settings channel rejects it).
- **Stored locally only**, in the existing `settings.json` in the app's user-data folder, as
  `acceptableUse: { version, userId, acceptedAt }`: the wording version, the local account id and the
  time. It is never sent to the backend, to an AI provider or to any other service, and is not logged.
- **Scope:** one record per device. It counts only for the account that accepted it and only for the
  current wording version; a different account on the same device, or a new wording version, must
  accept again. It survives restarts, sign-out and later settings changes. A damaged entry counts as
  "not accepted".
- It adds to, and does not replace, the existing consent: audio and screenshot input are still off by
  default with their own native confirmation, start only on an explicit press, show visible indicators,
  and screenshots keep their countdown and preview.
- Not built: withdrawing the acknowledgement from the UI (delete the `acceptableUse` entry or raise the
  wording version), a server-side record, per-session acknowledgement.

## Screen-capture privacy setting (M-15, D-80)

- Optional and **off by default**. Turning it on needs the acceptable-use notice (version 2, which describes this setting) and a native confirmation. It asks Windows to leave this app's own window out of supported screen capture. It is not a guarantee, it does not hide the app from the taskbar, the tray, the process list or monitoring, security or proctoring software, and in a Windows remote session the window is not excluded.
- The choice is stored on this device only (`capturePrivacy` in `settings.json`) and is never sent anywhere. A saved choice is re-applied after restart or window recovery with the in-window indicator shown; nothing turns it on automatically.
- You remain responsible for the rules of any interview, meeting or assessment. Details and limits: `docs/WINDOW_CAPTURE_PRIVACY.md`.

## Crash recovery and local diagnostics (M-5, D-70)

When the desktop window's renderer process crashes, the main process stops listening, wipes any held screenshot and cancels running answers, reloads the window once, and tells the user. Nothing is restarted for the user: audio, screenshots and sessions need the user's own explicit action again, with every consent, indicator and countdown.

Diagnostics are **local and optional**:
- **What is kept:** a bounded in-memory list of events from a fixed set of types and reasons (startup, window stopped with Electron's reason and exit code, reload attempts and outcomes, button choices, diagnostics copied), plus on-demand typed state: app, Electron and OS versions and architecture, uptime, on/off states of audio and screenshot features, whether signed in and whether the notice is accepted (yes/no), whether the backend is local or remote, shortcut statuses, recovery counters and the M-2 performance summaries.
- **Never included:** transcripts, prompts, answers, audio, screenshots, resume or document text, API keys, passwords, tokens or authorization headers, email addresses, account or session ids, the backend's address, or file paths. Events cannot hold free text by construction, and the finished report is also scrubbed for secret-shaped strings, email addresses and Windows user folders.
- **Storage:** memory only. It is lost when the app closes, and nothing is written to disk (the main process may write only the settings and sign-in files).
- **Leaves the machine only if the user sends it.** The "Copy diagnostics" button places the text on the user's clipboard, written by the main process; the renderer receives only the number of characters. There is no automatic upload, no remote crash reporter and no telemetry.
- **Bounds:** at most 200 events kept, the newest 40 reported, 16,000 characters per report.
- **Limits:** only renderer crashes are recovered; a crash of the whole app is not. Pattern scrubbing cannot recognise plain prose, which is why prose can never enter the log. See `docs/DESKTOP_TROUBLESHOOTING.md`.

## Operational privacy controls

- **Web → desktop handoff (D-55, D-56):** the link opened by the web app contains only an opaque, single-use code that expires in 60 seconds. It carries no session content, URL or credential. Redeeming it signs the desktop in as the same user (a separate desktop login session, no password involved), so the code is treated as a credential: hashed server-side, rate-limited, never logged, kept by the desktop in memory only and never shown to the web page or the desktop interface. Handoff selects a prepared session and starts no recording.

- Restrict `/metrics` to trusted monitoring infrastructure; it is not application-authenticated.
- Restrict production logs and backups by role, encrypt them in transit/at rest, and define their
  retention separately from user content.
- Use synthetic fixtures in tests and evaluation; never add real resumes, transcripts, or API
  credentials to the repository.
- Treat job-page content and uploaded documents as untrusted. Do not copy their contents into logs
  or observability labels.
