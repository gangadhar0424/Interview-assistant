// Types and tiny pure helpers for the sessions UI (Phase 7D). These mirror the API contract in
// docs/API.md; the server is always the authority (every rule here is a UX convenience).
//
// This file is imported by Node's test runner (tests/*.test.ts) as well as by the app, so it must
// stay plain, erasable TypeScript: no enums, no path aliases, relative imports with ".ts".

export type IngestionStatus = "pending" | "processing" | "completed" | "failed";

export type Ingestion = {
  status: IngestionStatus;
  error_code: string | null;
  error_message: string | null;
};

// GET /api/v1/resumes and /documents (docs/API.md sections 2-3).
export type FileItem = {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
  ingestion: Ingestion | null; // text extraction
  indexing: Ingestion | null; // chunking + embeddings
};

export type ModelOption = {
  id: string;
  label: string;
  capabilities: string[];
  languages: string[];
};

export type ProviderOption = { id: string; models: ModelOption[] };

// GET /api/v1/ai/options
export type VoiceProviderOption = { id: string; models: { id: string; label: string }[] };

export type AiOptions = {
  providers: ProviderOption[]; // reasoning (answer) providers
  voice_providers: VoiceProviderOption[]; // voice-listening providers (D-57)
  languages: string[];
  question_types: string[];
  formats: string[];
  lengths: string[];
  tones: string[];
};

// POST /api/v1/sessions/job-import
export type JobImportResult = {
  status: "succeeded" | "partial" | "failed";
  company: string | null;
  title: string | null;
  description: string | null;
  message: string;
};

// GET /api/v1/sessions
export type SessionSummary = {
  id: string;
  type: string;
  title: string | null;
  company_name: string;
  status: string;
  created_at: string;
  updated_at: string;
  resume: { id: string | null; filename: string };
  document_count: number;
};

export type SessionList = { items: SessionSummary[]; next_cursor: string | null };

// A file can be selected for a session only when it is BOTH extracted and indexed. This is the
// same rule the files panel shows as "Ready" and the API enforces when a session is created.
export function isReady(file: FileItem): boolean {
  return file.ingestion?.status === "completed" && file.indexing?.status === "completed";
}

const LABEL_OVERRIDES: Record<string, string> = {
  en: "English",
  hi: "Hindi",
  te: "Telugu",
  how_do_you: "How do you...?",
  tell_me_about_yourself: "Tell me about yourself",
  experience_check: "Experience check",
  system_design: "System design",
  technical_knowledge: "Technical knowledge",
  full_script: "Full script",
  script_bullets: "Script as bullets",
  bullets: "Key-point bullets",
};

// Human-readable label for an enum value returned by /ai/options (unknown values are humanised).
export function labelFor(value: string): string {
  const known = LABEL_OVERRIDES[value];
  if (known !== undefined) return known;
  const spaced = value.replace(/_/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function statusLabel(status: string): string {
  return status === "ready" ? "Ready" : labelFor(status);
}
