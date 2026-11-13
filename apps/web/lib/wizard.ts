// Pure logic of the 2-step session wizard (Phase 7D): defaults, validation, import autofill,
// request payload, error mapping. No React, no network, no path aliases, so it is unit-tested
// with Node's built-in test runner (tests/wizard.test.ts).
//
// All limits below are UX hints that mirror the server defaults (docs/DECISIONS.md D-34/D-35).
// The API is the authority and re-validates everything; its messages are shown to the user.

import type { AiOptions, FileItem, JobImportResult } from "./sessions.ts";
import { isReady } from "./sessions.ts";

export const LIMITS = {
  name: 200, // company name and job title
  jobDescription: 20000,
  extraInstructions: 1000,
  maxDocuments: 10,
  url: 2048,
} as const;

export type WizardForm = {
  jobUrl: string;
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
  provider: string; // reasoning provider; "" = not chosen yet (mandatory, D-57)
  sttProvider: string; // voice-listening provider; "" = not chosen yet (mandatory, D-57)
  extra: string;
};

export type FormErrors = Partial<Record<keyof WizardForm, string>>;

export const STEP_ONE_FIELDS: (keyof WizardForm)[] = [
  "jobUrl",
  "company",
  "title",
  "description",
  "resumeId",
  "documentIds",
];

// Number of characters as the server counts them (code points, not UTF-16 units).
export function charCount(text: string): number {
  return Array.from(text).length;
}

function pick(values: string[] | undefined, preferred: string): string {
  if (!values || values.length === 0) return preferred;
  return values.includes(preferred) ? preferred : (values[0] ?? preferred);
}

// A selection is preselected only when the server offers exactly one choice; otherwise the user
// must pick explicitly (nothing is chosen on their behalf).
function onlyChoice(ids: string[] | undefined): string {
  return ids !== undefined && ids.length === 1 ? (ids[0] ?? "") : "";
}

export function emptyForm(options: AiOptions | null): WizardForm {
  return {
    jobUrl: "",
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
    provider: onlyChoice(options?.providers.map((p) => p.id)),
    sttProvider: onlyChoice(options?.voice_providers.map((p) => p.id)),
    extra: "",
  };
}

export function isHttpUrl(value: string): boolean {
  if (value.length === 0 || value.length > LIMITS.url || /\s/.test(value)) return false;
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && url.hostname.length > 0;
  } catch {
    return false;
  }
}

export function validateStepOne(form: WizardForm): FormErrors {
  const errors: FormErrors = {};
  if (form.jobUrl.trim() !== "" && !isHttpUrl(form.jobUrl.trim())) {
    errors.jobUrl = "Enter a valid web link starting with http:// or https://.";
  }
  const company = form.company.trim();
  if (company === "") errors.company = "Enter the company name.";
  else if (charCount(company) > LIMITS.name)
    errors.company = `Company name must be at most ${LIMITS.name} characters.`;
  if (charCount(form.title.trim()) > LIMITS.name)
    errors.title = `Job title must be at most ${LIMITS.name} characters.`;
  const description = form.description.trim();
  if (description === "") errors.description = "Enter the job description.";
  else if (charCount(description) > LIMITS.jobDescription)
    errors.description = `The job description must be at most ${LIMITS.jobDescription} characters.`;
  if (form.resumeId === null || form.resumeId === "") errors.resumeId = "Choose a resume.";
  if (form.documentIds.length > LIMITS.maxDocuments)
    errors.documentIds = `Choose at most ${LIMITS.maxDocuments} documents.`;
  else if (new Set(form.documentIds).size !== form.documentIds.length)
    errors.documentIds = "Each document can only be chosen once.";
  return errors;
}

export function validateStepTwo(form: WizardForm, options: AiOptions | null): FormErrors {
  const errors: FormErrors = {};
  const mustBeOffered = (value: string, offered: string[] | undefined): boolean =>
    offered !== undefined && offered.includes(value);
  if (!mustBeOffered(form.language, options?.languages)) errors.language = "Choose a language.";
  if (!mustBeOffered(form.questionType, options?.question_types))
    errors.questionType = "Choose a question type.";
  if (!mustBeOffered(form.format, options?.formats)) errors.format = "Choose an answer format.";
  if (!mustBeOffered(form.length, options?.lengths)) errors.length = "Choose an answer length.";
  if (!mustBeOffered(form.tone, options?.tones)) errors.tone = "Choose a tone.";
  if (charCount(form.extra.trim()) > LIMITS.extraInstructions)
    errors.extra = `Extra instructions must be at most ${LIMITS.extraInstructions} characters.`;
  // D-57: both selections are mandatory and must be offered by the server.
  if (!mustBeOffered(form.provider, options?.providers.map((p) => p.id)))
    errors.provider = "Choose a reasoning model.";
  if (!mustBeOffered(form.sttProvider, options?.voice_providers.map((p) => p.id)))
    errors.sttProvider = "Choose a voice listening model.";
  return errors;
}

export type CreatePayload = {
  type: "interview";
  company_name: string;
  title?: string;
  job_url?: string;
  job_description: string;
  resume_id: string;
  document_ids: string[];
  preferences: {
    language: string;
    question_type: string;
    answer_format: string;
    answer_length: string;
    answer_tone: string;
    extra_instructions?: string;
    provider: string;
    stt_provider: string;
  };
};

// The request body of POST /api/v1/sessions. Built from an explicit whitelist: nothing about
// ownership (user id), status, storage or any other server-controlled field can be sent, and
// optional values that were left empty are omitted; the two provider choices are mandatory.
export function toCreatePayload(form: WizardForm): CreatePayload {
  if (form.resumeId === null || form.resumeId === "") throw new Error("A resume is required.");
  const payload: CreatePayload = {
    type: "interview",
    company_name: form.company.trim(),
    job_description: form.description.trim(),
    resume_id: form.resumeId,
    document_ids: [...form.documentIds],
    preferences: {
      language: form.language,
      question_type: form.questionType,
      answer_format: form.format,
      answer_length: form.length,
      answer_tone: form.tone,
      // Mandatory and exclusive (D-57). No model is sent: the server uses each provider's
      // configured model, so changing a model never needs a client or database change.
      provider: form.provider,
      stt_provider: form.sttProvider,
    },
  };
  if (form.title.trim() !== "") payload.title = form.title.trim();
  if (form.jobUrl.trim() !== "") payload.job_url = form.jobUrl.trim();
  if (form.extra.trim() !== "") payload.preferences.extra_instructions = form.extra.trim();
  return payload;
}

// Only files that are fully ready (extracted AND indexed) may be selected.
export function readySplit(items: FileItem[]): { ready: FileItem[]; notReady: FileItem[] } {
  const ready: FileItem[] = [];
  const notReady: FileItem[] = [];
  for (const item of items) (isReady(item) ? ready : notReady).push(item);
  return { ready, notReady };
}

export function toggleDocument(ids: string[], id: string, max: number = LIMITS.maxDocuments): string[] {
  if (ids.includes(id)) return ids.filter((existing) => existing !== id);
  return ids.length < max ? [...ids, id] : ids;
}

export type ImportOutcome = {
  form: WizardForm;
  applied: string[]; // fields filled from the page
  kept: string[]; // fields the page could fill, but the user had already typed something
};

// Autofill from an import result. Imported text is untrusted page content: it is only ever put
// into editable text fields (rendered as plain text by React), and it NEVER replaces something
// the user already typed. A failed import changes nothing.
export function applyImport(form: WizardForm, result: JobImportResult): ImportOutcome {
  if (result.status === "failed") return { form, applied: [], kept: [] };
  const next: WizardForm = { ...form };
  const applied: string[] = [];
  const kept: string[] = [];
  const fill = (field: "company" | "title" | "description", label: string, value: string | null) => {
    if (value === null || value.trim() === "") return;
    if (form[field].trim() === "") {
      next[field] = value;
      applied.push(label);
    } else {
      kept.push(label);
    }
  };
  fill("company", "company", result.company);
  fill("title", "job title", result.title);
  fill("description", "job description", result.description);
  return { form: next, applied, kept };
}

export function importNotice(result: JobImportResult, outcome: ImportOutcome): string {
  if (result.status === "failed") return result.message;
  const parts = [result.message];
  if (outcome.applied.length > 0) parts.push(`Filled in: ${outcome.applied.join(", ")}.`);
  if (outcome.kept.length > 0) parts.push(`Kept what you typed for: ${outcome.kept.join(", ")}.`);
  return parts.join(" ");
}

export type ErrorInfo = {
  status: number;
  code: string;
  message: string;
  details: { field: string; message: string }[];
};

// Turns an API error into a user message and the wizard step that should be shown to fix it.
export function describeCreateError(err: ErrorInfo): { message: string; step: 1 | 2 } {
  switch (err.code) {
    case "material_not_found":
      return {
        message:
          "A selected resume or document no longer exists. Refresh the file lists and choose again.",
        step: 1,
      };
    case "material_not_ready":
      return {
        message: "A selected file is not ready yet. Choose only files marked Ready.",
        step: 1,
      };
    case "duplicate_material":
      return { message: "Each document can only be chosen once.", step: 1 };
    case "too_many_documents":
      return { message: err.message, step: 1 };
    case "invalid_provider_selection":
      return {
        message:
          "That reasoning or voice provider is not available. Choose another option.",
        step: 2,
      };
    case "validation_error": {
      const detail = err.details.map((d) => (d.field ? `${d.field}: ${d.message}` : d.message));
      const message = detail.length > 0 ? detail.join(" ") : err.message;
      const stepTwoField = /preferences|language|question|answer_|extra_|provider|model/;
      const onStepTwo = /preferences|extra_instructions|provider|model/.test(err.message);
      return { message, step: onStepTwo || detail.some((d) => stepTwoField.test(d)) ? 2 : 1 };
    }
    default:
      return { message: err.message, step: 1 };
  }
}
