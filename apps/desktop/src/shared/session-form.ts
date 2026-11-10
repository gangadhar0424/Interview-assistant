/**
 * Session-creation form rules (Phase 11.5), shared by the renderer (live feedback) and the main
 * process (enforcement). Pure: no Electron, no network, no storage.
 *
 * Limits mirror the documented backend defaults (D-34/D-35: company/title 200, description 20,000,
 * extra instructions 1,000, at most 10 documents). They are UX hints and a first line of defence:
 * the backend re-validates everything and stays the authority (an operator may configure other
 * limits, in which case the server's message is shown). Text is only trimmed here, never rewritten
 * or truncated; cleaning stays on the backend.
 */

export const SESSION_LIMITS = {
  name: 200, // company name and job title
  description: 20_000,
  extra: 1_000,
  maxDocuments: 10,
} as const;

export type SessionDraft = {
  company: string;
  title: string;
  description: string;
  resumeId: string | null;
  documentIds: string[];
  language: string;
  questionType: string;
  format: string;
  length: string;
  tone: string;
  extra: string;
};

export type SessionOptions = {
  languages: string[];
  question_types: string[];
  formats: string[];
  lengths: string[];
  tones: string[];
};

export type DraftErrors = Partial<Record<keyof SessionDraft, string>>;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ENUM_VALUE = /^[a-z][a-z0-9_]{0,63}$/;

/** Characters as the server counts them (code points, not UTF-16 units). */
export function charCount(text: string): number {
  return Array.from(text).length;
}

function pick(values: string[] | undefined, preferred: string): string {
  if (!values || values.length === 0) return preferred;
  return values.includes(preferred) ? preferred : (values[0] ?? preferred);
}

/** The web wizard's defaults, falling back to the first value the server actually offers. */
export function defaultDraft(options: SessionOptions | null): SessionDraft {
  return {
    company: "",
    title: "",
    description: "",
    resumeId: null,
    documentIds: [],
    language: pick(options?.languages, "en"),
    questionType: pick(options?.question_types, "auto"),
    format: pick(options?.formats, "full_script"),
    length: pick(options?.lengths, "balanced"),
    tone: pick(options?.tones, "simple"),
    extra: "",
  };
}

const LANGUAGE_NAMES: Record<string, string> = { en: "English", hi: "Hindi", te: "Telugu" };

/** A readable label for a value returned by `/ai/options` (unknown values are humanised). */
export function optionLabel(value: string): string {
  if (LANGUAGE_NAMES[value]) return LANGUAGE_NAMES[value] as string;
  const spaced = value.replaceAll("_", " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function styleSummary(draft: SessionDraft): string {
  return [draft.language, draft.questionType, draft.format, draft.length, draft.tone]
    .map(optionLabel)
    .join(" · ");
}

/** Validate a draft. `readyResumeIds`/`readyDocumentIds` are what the user may actually choose. */
export function validateDraft(
  draft: SessionDraft,
  options: SessionOptions | null,
  readyResumeIds?: ReadonlySet<string>,
  readyDocumentIds?: ReadonlySet<string>,
): DraftErrors {
  const errors: DraftErrors = {};
  const company = draft.company.trim();
  if (company === "") errors.company = "Enter the company name.";
  else if (charCount(company) > SESSION_LIMITS.name) {
    errors.company = `Company name must be at most ${SESSION_LIMITS.name} characters.`;
  }
  if (charCount(draft.title.trim()) > SESSION_LIMITS.name) {
    errors.title = `Job title must be at most ${SESSION_LIMITS.name} characters.`;
  }
  const description = draft.description.trim();
  if (description === "") errors.description = "Enter the job description.";
  else if (charCount(description) > SESSION_LIMITS.description) {
    errors.description = `The job description must be at most ${SESSION_LIMITS.description.toLocaleString("en-US")} characters.`;
  }
  if (draft.resumeId === null || draft.resumeId === "") errors.resumeId = "Choose a resume.";
  else if (!UUID.test(draft.resumeId)) errors.resumeId = "Choose a valid resume.";
  else if (readyResumeIds && !readyResumeIds.has(draft.resumeId)) {
    errors.resumeId = "Choose a resume that is ready.";
  }
  if (draft.documentIds.length > SESSION_LIMITS.maxDocuments) {
    errors.documentIds = `Choose at most ${SESSION_LIMITS.maxDocuments} documents.`;
  } else if (new Set(draft.documentIds).size !== draft.documentIds.length) {
    errors.documentIds = "Each document can only be chosen once.";
  } else if (draft.documentIds.some((id) => !UUID.test(id))) {
    errors.documentIds = "Choose valid documents.";
  } else if (readyDocumentIds && draft.documentIds.some((id) => !readyDocumentIds.has(id))) {
    errors.documentIds = "Choose only documents that are ready.";
  }
  const offered = (value: string, list: string[] | undefined) =>
    ENUM_VALUE.test(value) && (list === undefined || list.includes(value));
  if (!offered(draft.language, options?.languages)) errors.language = "Choose a language.";
  if (!offered(draft.questionType, options?.question_types)) errors.questionType = "Choose a question type.";
  if (!offered(draft.format, options?.formats)) errors.format = "Choose an answer format.";
  if (!offered(draft.length, options?.lengths)) errors.length = "Choose an answer length.";
  if (!offered(draft.tone, options?.tones)) errors.tone = "Choose a tone.";
  if (charCount(draft.extra.trim()) > SESSION_LIMITS.extra) {
    errors.extra = `Extra instructions must be at most ${SESSION_LIMITS.extra} characters.`;
  }
  return errors;
}

export function hasErrors(errors: DraftErrors): boolean {
  return Object.keys(errors).length > 0;
}

/** The exact request body for `POST /api/v1/sessions` (identity and status are never sent). */
export function toCreateBody(draft: SessionDraft): Record<string, unknown> {
  const body: Record<string, unknown> = {
    type: "interview",
    company_name: draft.company.trim(),
    job_description: draft.description.trim(),
    resume_id: draft.resumeId,
    document_ids: draft.documentIds,
    preferences: {
      language: draft.language,
      question_type: draft.questionType,
      answer_format: draft.format,
      answer_length: draft.length,
      answer_tone: draft.tone,
      ...(draft.extra.trim() ? { extra_instructions: draft.extra.trim() } : {}),
    },
  };
  if (draft.title.trim()) body.title = draft.title.trim();
  return body;
}

const DRAFT_KEYS: Array<keyof SessionDraft> = [
  "company", "title", "description", "resumeId", "documentIds",
  "language", "questionType", "format", "length", "tone", "extra",
];

/** Strictly parse an untrusted draft (from IPC): exact keys, exact types. */
export function parseDraft(value: unknown): SessionDraft {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Fill in the session details.");
  }
  const record = value as Record<string, unknown>;
  if (Object.keys(record).some((key) => !DRAFT_KEYS.includes(key as keyof SessionDraft))) {
    throw new Error("The session details are not valid.");
  }
  const text = (key: keyof SessionDraft): string => {
    const item = record[key];
    if (typeof item !== "string") throw new Error("The session details are not valid.");
    return item;
  };
  const ids = record.documentIds;
  if (!Array.isArray(ids) || ids.some((id) => typeof id !== "string")) {
    throw new Error("The session details are not valid.");
  }
  const resume = record.resumeId;
  if (resume !== null && typeof resume !== "string") throw new Error("The session details are not valid.");
  return {
    company: text("company"),
    title: text("title"),
    description: text("description"),
    resumeId: resume,
    documentIds: ids as string[],
    language: text("language"),
    questionType: text("questionType"),
    format: text("format"),
    length: text("length"),
    tone: text("tone"),
    extra: text("extra"),
  };
}

/**
 * Where a user without a ready resume is sent. The desktop has no browser-launch capability, so
 * this is a link to show and copy. It is derived only for a local (loopback) backend, where the web
 * app's address is known; otherwise null (the UI then explains how to find the web app).
 */
export function webAppCreateLink(backendBaseUrl: string): string | null {
  try {
    const url = new URL(backendBaseUrl);
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    return loopback ? "http://localhost:3000/sessions/new" : null;
  } catch {
    return null;
  }
}
