# Product Requirements Document (PRD)

## 1. Product Overview
An internal-use platform that helps a user prepare for and run interview sessions with AI assistance. The user uploads a resume and supporting documents, creates an interview session with job context and answer preferences, and (in later phases) uses a desktop floating client for live support: typed questions, screenshots, and real-time transcription with detected questions and streamed, personalized answers grounded in their own materials.

Components: **Web application** (account, content, sessions, history) and **Desktop client** (live session UI; later phase). One backend serves both.

## 2. Target User
Internal users (employees/approved testers) preparing for job interviews who want answers tailored to their own resume/documents and a specific job. Assumed technical comfort: moderate. Languages: English first; Hindi and Telugu prioritized next.

Usage must be authorized and user-controlled: the user is responsible for complying with the rules of any interview they attend and applicable recording-consent laws (see RISKS §Privacy/Policy).

## 3. In-Scope Features
1. Email/password sign-up, login, logout.
2. Dashboard with: Call Sessions, Resumes, Documents, Create Session.
3. Resume management: upload, list, view status, select for sessions, delete.
4. Document management: upload, list, view status, select for sessions, delete.
5. Async ingestion + RAG (validate → store → extract → chunk → embed → index) with retry/failure handling.
6. Create Session (2 steps): details (type=Interview, job link, company, job description, resume, documents) and preferences (language, provider/model, question type, format, length, tone, extra instructions).
7. Session history with status and basic metrics.
8. Multi-provider AI abstraction (LLM, embeddings; later STT/vision).
9. *(Later phases)* Desktop client, typed questions, screenshot input, real-time transcription, question detection, transcript storage.

## 4. Explicitly Out of Scope
Resume builder · Mock interview product · Headshot generation · General calling/telephony · Social/sharing features · Billing/payments · Team/admin multi-tenant management (initial) · Mobile apps · Hidden/stealth capture or any feature intended to evade proctoring/detection · Proprietary scraping or reverse engineering of third-party systems.

## 5. User Journeys
**J1 Onboard:** Sign up → land on dashboard (empty states) → upload resume.
**J2 Content:** Upload resume/documents → see status (Uploaded → Processing → Ready / Failed) → retry or delete.
**J3 Create session:** Create Session → Step 1: paste job link (optional autofill; manual fallback) / company / JD / pick or upload resume / pick or upload docs → Step 2: language, provider/model, question type, format, length, tone, extra instructions → Create → session appears in Call Sessions.
**J4 Review:** Open session → see metadata, selected materials, status, later transcript/questions/answers/latency.
**J5 Live (later):** Launch desktop client → sign in → pick session → floating window: type question or capture screenshot or enable audio → streamed answer.
**J6 Cleanup:** Delete resume/document → file, chunks, embeddings removed; sessions referencing it retain a "deleted" placeholder.

## 6. Functional Requirements
| ID | Requirement |
|---|---|
| FR-AUTH-1 | Sign up with email+password; email unique (case-insensitive); password policy enforced. |
| FR-AUTH-2 | Login returns access + refresh credentials; logout revokes refresh token. |
| FR-AUTH-3 | Every data access is scoped to the authenticated user; cross-user access returns 404. |
| FR-RES-1 | Upload PDF/DOCX (and TXT) up to configured size (default 10 MB); validated by extension, MIME, and magic bytes. |
| FR-RES-2 | List/view/delete own resumes; show ingestion status. |
| FR-DOC-1 | Upload PDF/DOCX/TXT/MD (limit default 20 MB); list/view/delete own documents. |
| FR-ING-1 | Ingestion runs asynchronously with status, retries with backoff, and a terminal failure state with reason; user can retry failed items. |
| FR-ING-2 | Every chunk stores `user_id`, `source_type`, `source_id`; delete cascades to chunks/vectors/files. |
| FR-RAG-1 | Retrieval restricted to `user_id` AND the session's selected resume/doc IDs; job context supplied directly, not retrieved from others' data. |
| FR-SES-1 | Create session with type=Interview, company, JD (required), job URL (optional), one resume, 0..N documents. |
| FR-SES-2 | Job URL fetch is best-effort, SSRF-protected, public pages only; failure never blocks manual entry. |
| FR-SES-3 | Preferences: language (en/hi/te, extensible), provider/model (from allowed list), question type, format, length, tone, extra instructions. |
| FR-SES-4 | Preferences compile into a structured `GenerationConfig` consumed by a single prompt builder. |
| FR-AI-1 | Provider/model selection validated against server policy and configured credentials; fallback routing on failure. |
| FR-HIS-1 | Dashboard lists own sessions with status, company, created date. |
| FR-LIVE-* | *(Later)* Typed Q&A, screenshot, WebSocket streaming, transcription, question detection, transcript/answer/latency storage. |

## 7. Non-Functional Requirements
- **Security/Privacy:** per SECURITY section of ARCHITECTURE; strict isolation; encrypted in transit; uploaded files kept in private local file storage (never publicly served), per D-26.
- **Performance:** realistic targets in ARCHITECTURE §Latency Budget (targets, not guarantees; provider-dependent).
- **Reliability:** ingestion idempotent and retryable; no lost jobs on worker restart.
- **Scalability:** stateless API; horizontal worker scaling; storage/vector swap behind interfaces.
- **Observability:** structured logs with request IDs; metrics for latency, queue depth, failures.
- **Maintainability:** typed code, contract-first API, layered tests.
- **Accessibility/UX:** keyboard accessible web UI; desktop overlay controllable and always visibly indicating capture state.
- **i18n:** UI strings externalized; content language independent of UI language.

## 8. Acceptance Criteria (product level)
1. A new user can sign up, log in, upload a resume, see it reach Ready, create an Interview session with preferences, and see it on the dashboard.
2. User B cannot read, list, retrieve, or infer any data of User A through any endpoint or retrieval path (verified by automated isolation tests).
3. Deleting a resume/document removes its stored file, chunks and vectors, and it cannot be selected or retrieved afterwards.
4. A failed ingestion shows a clear status and can be retried; transient failures auto-retry.
5. Job URL failure leaves the form usable with manual entry.
6. Changing question type/format/length/tone measurably changes the compiled `GenerationConfig` and resulting prompt (verified by prompt-builder snapshot tests).
7. Switching provider requires configuration only, not application logic changes (verified by running contract tests against ≥2 adapters).
8. *(Later)* Typed question returns a streamed answer using only selected materials; first-token latency measured and recorded.
