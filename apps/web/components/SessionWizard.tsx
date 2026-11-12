"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { isUuid } from "@/lib/qa";
import {
  isReady,
  labelFor,
  type AiOptions,
  type FileItem,
  type JobImportResult,
} from "@/lib/sessions";
import {
  LIMITS,
  applyImport,
  charCount,
  describeCreateError,
  emptyForm,
  importNotice,
  isHttpUrl,
  readySplit,
  toCreatePayload,
  toggleDocument,
  validateStepOne,
  validateStepTwo,
  type FormErrors,
  type WizardForm,
} from "@/lib/wizard";

const ui: Record<string, CSSProperties> = {
  form: { display: "grid", gap: "1rem", width: "100%", maxWidth: "40rem" },
  field: { display: "grid", gap: "0.25rem", border: "none", padding: 0, margin: 0 },
  label: { fontWeight: 600 },
  input: { padding: "0.5rem", font: "inherit", width: "100%", boxSizing: "border-box" },
  hint: { color: "#666", fontSize: "0.85rem", margin: 0 },
  error: { color: "#b00020", fontSize: "0.9rem", margin: 0 },
  row: { display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" },
  option: { display: "flex", gap: "0.5rem", alignItems: "baseline", overflowWrap: "anywhere" },
};

type Note = { text: string; ok: boolean };

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p id={id} role="alert" style={ui.error}>
      {message}
    </p>
  );
}

// Two-step "Create session" wizard (PRD J3). Step 1: job link (optional autofill), company, job
// description, one ready resume, up to 10 ready documents. Step 2: answer preferences. The API
// validates everything again; ownership always comes from the signed-in user, never from this form.
export function SessionWizard() {
  const { authedRequest } = useAuth();
  const router = useRouter();

  const [step, setStep] = useState<1 | 2>(1);
  const [options, setOptions] = useState<AiOptions | null>(null);
  const [resumes, setResumes] = useState<FileItem[] | null>(null);
  const [documents, setDocuments] = useState<FileItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState<WizardForm>(() => emptyForm(null));
  const [errors, setErrors] = useState<FormErrors>({});
  const [importing, setImporting] = useState(false);
  const [importNote, setImportNote] = useState<Note | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const headingRef = useRef<HTMLHeadingElement>(null);
  const formRef = useRef(form);
  useEffect(() => {
    formRef.current = form; // latest values for async callbacks (e.g. an import that finishes later)
  }, [form]);

  // Initial load: the files (only Ready ones can be chosen) and the allowed preference values.
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      authedRequest<{ items: FileItem[] }>("/api/v1/resumes?limit=200"),
      authedRequest<{ items: FileItem[] }>("/api/v1/documents?limit=200"),
      authedRequest<AiOptions>("/api/v1/ai/options"),
    ])
      .then(([resumeList, documentList, aiOptions]) => {
        if (cancelled) return;
        setResumes(resumeList.items);
        setDocuments(documentList.items);
        setOptions(aiOptions);
        setForm(emptyForm(aiOptions)); // the form is not editable until this point
        setLoadError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(err instanceof ApiError ? err.message : "Could not load the form.");
      });
    return () => {
      cancelled = true;
    };
  }, [authedRequest]);

  useEffect(() => {
    headingRef.current?.focus(); // keyboard/screen-reader users land on the new step
  }, [step]);

  const refreshFiles = useCallback(async () => {
    setRefreshing(true);
    try {
      const [resumeList, documentList] = await Promise.all([
        authedRequest<{ items: FileItem[] }>("/api/v1/resumes?limit=200"),
        authedRequest<{ items: FileItem[] }>("/api/v1/documents?limit=200"),
      ]);
      setResumes(resumeList.items);
      setDocuments(documentList.items);
      // Drop selections that are no longer Ready (or no longer exist).
      const readyResumes = new Set(resumeList.items.filter(isReady).map((f) => f.id));
      const readyDocs = new Set(documentList.items.filter(isReady).map((f) => f.id));
      setForm((current) => ({
        ...current,
        resumeId: current.resumeId !== null && readyResumes.has(current.resumeId) ? current.resumeId : null,
        documentIds: current.documentIds.filter((id) => readyDocs.has(id)),
      }));
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Could not refresh your files.");
    } finally {
      setRefreshing(false);
    }
  }, [authedRequest]);

  function update<K extends keyof WizardForm>(field: K, value: WizardForm[K]) {
    setForm((current) => ({ ...current, [field]: value }));
    setErrors((current) => ({ ...current, [field]: undefined }));
  }

  async function onImport() {
    const url = form.jobUrl.trim();
    if (!isHttpUrl(url)) {
      setErrors((current) => ({
        ...current,
        jobUrl: "Enter a valid web link starting with http:// or https://.",
      }));
      return;
    }
    setImporting(true);
    setImportNote(null);
    try {
      const result = await authedRequest<JobImportResult>("/api/v1/sessions/job-import", {
        body: { url },
      });
      const outcome = applyImport(formRef.current, result);
      setForm(outcome.form);
      setImportNote({ text: importNotice(result, outcome), ok: result.status !== "failed" });
    } catch (err) {
      // Manual entry is never blocked: every field stays editable whatever happens here.
      setImportNote({
        text:
          err instanceof ApiError
            ? err.message
            : "The import failed. Please enter the details manually.",
        ok: false,
      });
    } finally {
      setImporting(false);
    }
  }

  function goNext() {
    const found = validateStepOne(form);
    setErrors(found);
    if (Object.keys(found).length === 0) {
      setSubmitError(null);
      setStep(2);
    }
  }

  async function onCreate() {
    if (submitting) return;
    const stepOne = validateStepOne(form);
    const stepTwo = validateStepTwo(form, options);
    if (Object.keys(stepOne).length > 0) {
      setErrors(stepOne);
      setStep(1);
      return;
    }
    if (Object.keys(stepTwo).length > 0) {
      setErrors(stepTwo);
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      const created = await authedRequest<{ id: string }>("/api/v1/sessions", {
        body: toCreatePayload(form),
      });
      // Straight to the new session, where Start Interview is. Fall back to the dashboard.
      router.push(typeof created?.id === "string" && isUuid(created.id) ? `/sessions/${created.id}` : "/home");
    } catch (err) {
      if (err instanceof ApiError) {
        const described = describeCreateError({
          status: err.status,
          code: err.code,
          message: err.message,
          details: err.details,
        });
        setSubmitError(described.message);
        setStep(described.step);
      } else {
        setSubmitError("Something went wrong. Please try again.");
      }
      setSubmitting(false);
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (step === 1) goNext();
    else void onCreate();
  }

  if (loadError && (resumes === null || options === null)) {
    return (
      <p role="alert" style={ui.error}>
        {loadError}
      </p>
    );
  }
  if (resumes === null || documents === null || options === null) return <p>Loading…</p>;

  const readyResumes = readySplit(resumes);
  const readyDocuments = readySplit(documents);
  const hiddenCount = readyResumes.notReady.length + readyDocuments.notReady.length;
  const provider = options.providers.find((p) => p.id === form.provider);
  const atDocumentLimit = form.documentIds.length >= LIMITS.maxDocuments;

  return (
    <form onSubmit={onSubmit} noValidate style={ui.form} aria-labelledby="wizard-title">
      <div>
        <p style={ui.hint}>Step {step} of 2</p>
        <h1 id="wizard-title" ref={headingRef} tabIndex={-1} style={{ margin: 0 }}>
          {step === 1 ? "Job and materials" : "Answer preferences"}
        </h1>
      </div>

      {submitError && (
        <p role="alert" style={ui.error}>
          {submitError}
        </p>
      )}

      {step === 1 && (
        <>
          <div style={ui.field}>
            <label htmlFor="jobUrl" style={ui.label}>
              Job link (optional)
            </label>
            <div style={ui.row}>
              <input
                id="jobUrl"
                type="url"
                inputMode="url"
                value={form.jobUrl}
                onChange={(e) => update("jobUrl", e.target.value)}
                aria-invalid={errors.jobUrl ? true : undefined}
                aria-describedby="jobUrl-hint jobUrl-error"
                style={{ ...ui.input, flex: 1, minWidth: "12rem" }}
              />
              <button type="button" onClick={() => void onImport()} disabled={importing}>
                {importing ? "Importing…" : "Import details"}
              </button>
            </div>
            <p id="jobUrl-hint" style={ui.hint}>
              Public job pages only. If the import does not work, just fill in the fields below.
            </p>
            <FieldError id="jobUrl-error" message={errors.jobUrl} />
            <div aria-live="polite">
              {importNote && (
                <p
                  data-testid="import-note"
                  style={{ ...ui.hint, color: importNote.ok ? "#1b7f3b" : "#8a6d00" }}
                >
                  {importNote.text}
                </p>
              )}
            </div>
          </div>

          <div style={ui.field}>
            <label htmlFor="company" style={ui.label}>
              Company
            </label>
            <input
              id="company"
              value={form.company}
              onChange={(e) => update("company", e.target.value)}
              aria-invalid={errors.company ? true : undefined}
              aria-describedby="company-error"
              maxLength={LIMITS.name * 2}
              style={ui.input}
            />
            <FieldError id="company-error" message={errors.company} />
          </div>

          <div style={ui.field}>
            <label htmlFor="title" style={ui.label}>
              Job title (optional)
            </label>
            <input
              id="title"
              value={form.title}
              onChange={(e) => update("title", e.target.value)}
              aria-invalid={errors.title ? true : undefined}
              aria-describedby="title-error"
              style={ui.input}
            />
            <FieldError id="title-error" message={errors.title} />
          </div>

          <div style={ui.field}>
            <label htmlFor="description" style={ui.label}>
              Job description
            </label>
            <textarea
              id="description"
              rows={8}
              value={form.description}
              onChange={(e) => update("description", e.target.value)}
              aria-invalid={errors.description ? true : undefined}
              aria-describedby="description-count description-error"
              style={ui.input}
            />
            <p id="description-count" style={ui.hint}>
              {charCount(form.description)} / {LIMITS.jobDescription} characters
            </p>
            <FieldError id="description-error" message={errors.description} />
          </div>

          <fieldset style={ui.field} aria-describedby="resumeId-error">
            <legend style={ui.label}>Resume</legend>
            {readyResumes.ready.length === 0 ? (
              <p style={ui.hint}>
                No resume is ready yet. Upload one and wait until it shows Ready.
              </p>
            ) : (
              readyResumes.ready.map((file) => (
                <label key={file.id} style={ui.option}>
                  <input
                    type="radio"
                    name="resume"
                    checked={form.resumeId === file.id}
                    onChange={() => update("resumeId", file.id)}
                  />
                  {file.filename}
                </label>
              ))
            )}
            <FieldError id="resumeId-error" message={errors.resumeId} />
          </fieldset>

          <fieldset style={ui.field} aria-describedby="documentIds-error">
            <legend style={ui.label}>
              Documents (optional, {form.documentIds.length} of {LIMITS.maxDocuments} chosen)
            </legend>
            {readyDocuments.ready.length === 0 ? (
              <p style={ui.hint}>No documents are ready yet.</p>
            ) : (
              readyDocuments.ready.map((file) => {
                const checked = form.documentIds.includes(file.id);
                return (
                  <label key={file.id} style={ui.option}>
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!checked && atDocumentLimit}
                      onChange={() =>
                        update("documentIds", toggleDocument(form.documentIds, file.id))
                      }
                    />
                    {file.filename}
                  </label>
                );
              })
            )}
            <FieldError id="documentIds-error" message={errors.documentIds} />
          </fieldset>

          <div style={ui.row}>
            <button type="button" onClick={() => void refreshFiles()} disabled={refreshing}>
              {refreshing ? "Refreshing…" : "Refresh files"}
            </button>
            <p style={ui.hint}>
              Only files marked Ready can be chosen.
              {hiddenCount > 0 && ` ${hiddenCount} still processing or failed.`}{" "}
              <Link href="/home#files">Upload or check files</Link>
            </p>
          </div>
          {loadError && (
            <p role="alert" style={ui.error}>
              {loadError}
            </p>
          )}

          <div style={ui.row}>
            <Link href="/home">Cancel</Link>
            <button type="submit">Next</button>
          </div>
        </>
      )}

      {step === 2 && (
        <>
          <Select
            id="language"
            label="Answer language"
            value={form.language}
            values={options.languages}
            error={errors.language}
            onChange={(v) => update("language", v)}
          />
          <Select
            id="questionType"
            label="Question type"
            value={form.questionType}
            values={options.question_types}
            error={errors.questionType}
            onChange={(v) => update("questionType", v)}
          />
          <Select
            id="format"
            label="Answer format"
            value={form.format}
            values={options.formats}
            error={errors.format}
            onChange={(v) => update("format", v)}
          />
          <Select
            id="length"
            label="Answer length"
            value={form.length}
            values={options.lengths}
            error={errors.length}
            onChange={(v) => update("length", v)}
          />
          <Select
            id="tone"
            label="Tone"
            value={form.tone}
            values={options.tones}
            error={errors.tone}
            onChange={(v) => update("tone", v)}
          />

          {options.providers.length > 0 ? (
            <>
              <div style={ui.field}>
                <label htmlFor="provider" style={ui.label}>
                  AI provider (optional)
                </label>
                <select
                  id="provider"
                  value={form.provider}
                  onChange={(e) => {
                    update("provider", e.target.value);
                    update("model", "");
                  }}
                  style={ui.input}
                >
                  <option value="">Server default</option>
                  {options.providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.id}
                    </option>
                  ))}
                </select>
                <FieldError id="provider-error" message={errors.provider} />
              </div>
              {provider && (
                <div style={ui.field}>
                  <label htmlFor="model" style={ui.label}>
                    Model (optional)
                  </label>
                  <select
                    id="model"
                    value={form.model}
                    onChange={(e) => update("model", e.target.value)}
                    style={ui.input}
                  >
                    <option value="">Provider default</option>
                    {provider.models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}
                      </option>
                    ))}
                  </select>
                  <FieldError id="model-error" message={errors.model} />
                </div>
              )}
            </>
          ) : (
            <p style={ui.hint}>
              AI provider and model: the server default is used (no provider choices are enabled).
            </p>
          )}

          <div style={ui.field}>
            <label htmlFor="extra" style={ui.label}>
              Extra instructions (optional)
            </label>
            <textarea
              id="extra"
              rows={4}
              value={form.extra}
              onChange={(e) => update("extra", e.target.value)}
              aria-invalid={errors.extra ? true : undefined}
              aria-describedby="extra-count extra-error"
              style={ui.input}
            />
            <p id="extra-count" style={ui.hint}>
              {charCount(form.extra)} / {LIMITS.extraInstructions} characters. These adjust wording
              and style only.
            </p>
            <FieldError id="extra-error" message={errors.extra} />
          </div>

          <div style={ui.row}>
            <button type="button" onClick={() => setStep(1)} disabled={submitting}>
              Back
            </button>
            <button type="submit" disabled={submitting}>
              {submitting ? "Creating…" : "Create session"}
            </button>
          </div>
        </>
      )}
    </form>
  );
}

function Select(props: {
  id: string;
  label: string;
  value: string;
  values: string[];
  error?: string;
  onChange: (value: string) => void;
}) {
  return (
    <div style={ui.field}>
      <label htmlFor={props.id} style={ui.label}>
        {props.label}
      </label>
      <select
        id={props.id}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
        aria-invalid={props.error ? true : undefined}
        aria-describedby={`${props.id}-error`}
        style={ui.input}
      >
        {props.values.map((v) => (
          <option key={v} value={v}>
            {labelFor(v)}
          </option>
        ))}
      </select>
      <FieldError id={`${props.id}-error`} message={props.error} />
    </div>
  );
}
