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

Audio capture and screenshot capture are not implemented in the current product. No raw audio is
currently stored. Any future audio feature must require explicit user initiation, a persistent
visible capture indicator, and clear consent from all people whose speech is captured, consistent
with local law and interview-platform rules. Prefer ephemeral audio processing and do not retain raw
audio by default. Any future transcript/screenshot feature must specify purpose, storage, access,
retention, deletion, third-party processing, and user consent before implementation.

## Operational privacy controls

- Restrict `/metrics` to trusted monitoring infrastructure; it is not application-authenticated.
- Restrict production logs and backups by role, encrypt them in transit/at rest, and define their
  retention separately from user content.
- Use synthetic fixtures in tests and evaluation; never add real resumes, transcripts, or API
  credentials to the repository.
- Treat job-page content and uploaded documents as untrusted. Do not copy their contents into logs
  or observability labels.
