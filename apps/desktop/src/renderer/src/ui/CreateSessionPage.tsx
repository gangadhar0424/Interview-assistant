import { useEffect, useState, type FormEvent } from "react";
import {
  charCount,
  optionLabel,
  SESSION_LIMITS,
  styleSummary,
  webAppCreateLink,
  type SessionDraft,
} from "../../../shared/session-form";
import { useDesktopStore } from "../store";

const STATUS_TEXT: Record<string, string> = {
  ready: "Ready",
  processing: "Still processing",
  failed: "Processing failed",
};

/**
 * Create an interview session from the desktop (Phase 11.5). The form lives in renderer memory only
 * for this workflow; every backend call goes through the main process.
 */
export function CreateSessionPage({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: () => void;
}) {
  const options = useDesktopStore((state) => state.createOptions);
  const draft = useDesktopStore((state) => state.createDraft);
  const status = useDesktopStore((state) => state.createStatus);
  const error = useDesktopStore((state) => state.createError);
  const fieldErrors = useDesktopStore((state) => state.createFieldErrors);
  const backendUrl = useDesktopStore((state) => state.settings?.backendBaseUrl ?? "");
  const loadCreateOptions = useDesktopStore((state) => state.loadCreateOptions);
  const updateDraft = useDesktopStore((state) => state.updateDraft);
  const resetCreate = useDesktopStore((state) => state.resetCreate);
  const submitCreate = useDesktopStore((state) => state.submitCreate);
  const [customize, setCustomize] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    void loadCreateOptions();
  }, [loadCreateOptions]);

  const resumes = options?.resumes ?? [];
  const documents = options?.documents ?? [];
  const readyResumes = resumes.filter((item) => item.status === "ready");
  const submitting = status === "submitting";
  const link = webAppCreateLink(backendUrl);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    const id = await submitCreate();
    if (id) onCreated();
  }

  function toggleDocument(id: string) {
    const selected = draft.documentIds.includes(id);
    updateDraft({
      documentIds: selected ? draft.documentIds.filter((d) => d !== id) : [...draft.documentIds, id],
    });
  }

  async function copyLink() {
    if (!link) return;
    const result = await window.desktop.app.copyText(link);
    if (result.ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }
  }

  function cancel() {
    resetCreate(); // the draft is dropped, never stored
    onCancel();
  }

  const select = (
    label: string,
    field: keyof SessionDraft,
    values: string[] | undefined,
  ) => (
    <label className="create-field" key={field}>
      <span className="field-label">{label}</span>
      <select
        className="field-control"
        onChange={(event) => updateDraft({ [field]: event.target.value } as Partial<SessionDraft>)}
        value={String(draft[field] ?? "")}
      >
        {(values ?? []).map((value) => (
          <option key={value} value={value}>
            {optionLabel(value)}
          </option>
        ))}
      </select>
      {fieldErrors[field] && <span className="field-error">{fieldErrors[field]}</span>}
    </label>
  );

  return (
    <div className="page-scroll">
      <div className="page-heading">
        <div>
          <p className="eyebrow">NEW SESSION</p>
          <h1>Create a session</h1>
          <p className="muted-copy">Tell the assistant about the role and choose what it may use.</p>
        </div>
      </div>
      {error && (
        <p className="inline-error create-error" role="alert">
          {error}
        </p>
      )}

      {status === "loading" && !options && <p className="muted-copy">Loading your files…</p>}

      {options && readyResumes.length === 0 && (
        <section aria-label="No ready resume" className="create-guidance">
          <h2>You need a ready resume first</h2>
          <p>
            A session needs one resume that has finished processing. This app can’t upload files yet, so
            upload your resume in the web app, wait until it shows as ready, then come back and refresh.
          </p>
          {resumes.length > 0 && (
            <ul className="create-file-status">
              {resumes.map((item) => (
                <li key={item.id}>
                  {item.filename} — {STATUS_TEXT[item.status]}
                </li>
              ))}
            </ul>
          )}
          {link ? (
            <div className="create-link-row">
              <code className="create-link">{link}</code>
              <button className="button button-secondary" onClick={() => void copyLink()} type="button">
                {copied ? "Copied" : "Copy link"}
              </button>
            </div>
          ) : (
            <p className="settings-help">
              Open the Interview Assistant web app in your browser, go to Create session, and upload a
              resume there.
            </p>
          )}
          <div className="capture-actions">
            <button
              className="button button-primary"
              disabled={status === "loading"}
              onClick={() => void loadCreateOptions()}
              type="button"
            >
              Refresh
            </button>
            <button className="button button-secondary" onClick={cancel} type="button">
              Back
            </button>
          </div>
        </section>
      )}

      {options && readyResumes.length > 0 && (
        <form className="create-form" onSubmit={(event) => void submit(event)}>
          <label className="create-field">
            <span className="field-label">Company name</span>
            <input
              autoComplete="off"
              className="field-control"
              onChange={(event) => updateDraft({ company: event.target.value })}
              value={draft.company}
            />
            {fieldErrors.company && <span className="field-error">{fieldErrors.company}</span>}
          </label>

          <label className="create-field">
            <span className="field-label">Job title (optional)</span>
            <input
              autoComplete="off"
              className="field-control"
              onChange={(event) => updateDraft({ title: event.target.value })}
              value={draft.title}
            />
            {fieldErrors.title && <span className="field-error">{fieldErrors.title}</span>}
          </label>

          <label className="create-field">
            <span className="field-label">Job description</span>
            <textarea
              className="field-control create-description"
              onChange={(event) => updateDraft({ description: event.target.value })}
              placeholder="Paste the job description here."
              rows={6}
              value={draft.description}
            />
            <span className="character-count">
              {charCount(draft.description.trim())}/{SESSION_LIMITS.description.toLocaleString("en-US")}
            </span>
            {fieldErrors.description && <span className="field-error">{fieldErrors.description}</span>}
          </label>

          <fieldset className="create-group">
            <legend className="field-label">Resume (choose one)</legend>
            {resumes.map((item) => (
              <label
                className={`create-choice ${item.status === "ready" ? "" : "create-choice-disabled"}`}
                key={item.id}
              >
                <input
                  checked={draft.resumeId === item.id}
                  disabled={item.status !== "ready"}
                  name="resume"
                  onChange={() => updateDraft({ resumeId: item.id })}
                  type="radio"
                />
                <span>
                  {item.filename}
                  {item.status !== "ready" && <em> — {STATUS_TEXT[item.status]}</em>}
                </span>
              </label>
            ))}
            {fieldErrors.resumeId && <span className="field-error">{fieldErrors.resumeId}</span>}
          </fieldset>

          <fieldset className="create-group">
            <legend className="field-label">
              Supporting documents (optional, up to {SESSION_LIMITS.maxDocuments})
            </legend>
            {documents.length === 0 && <p className="settings-help">No documents uploaded yet.</p>}
            {documents.map((item) => {
              const checked = draft.documentIds.includes(item.id);
              const atLimit = draft.documentIds.length >= SESSION_LIMITS.maxDocuments && !checked;
              return (
                <label
                  className={`create-choice ${item.status === "ready" && !atLimit ? "" : "create-choice-disabled"}`}
                  key={item.id}
                >
                  <input
                    checked={checked}
                    disabled={item.status !== "ready" || atLimit}
                    onChange={() => toggleDocument(item.id)}
                    type="checkbox"
                  />
                  <span>
                    {item.filename}
                    {item.status !== "ready" && <em> — {STATUS_TEXT[item.status]}</em>}
                  </span>
                </label>
              );
            })}
            {fieldErrors.documentIds && <span className="field-error">{fieldErrors.documentIds}</span>}
          </fieldset>

          <section className="create-style" aria-label="Answer style">
            <div className="create-style-row">
              <div>
                <span className="field-label">Answer style</span>
                <p className="create-style-summary">{styleSummary(draft)}</p>
              </div>
              <button className="text-button" onClick={() => setCustomize(!customize)} type="button">
                {customize ? "Hide" : "Customize"}
              </button>
            </div>
            {customize && (
              <div className="create-style-fields">
                {select("Language", "language", options.options.languages)}
                {select("Question type", "questionType", options.options.question_types)}
                {select("Answer format", "format", options.options.formats)}
                {select("Answer length", "length", options.options.lengths)}
                {select("Tone", "tone", options.options.tones)}
                <label className="create-field">
                  <span className="field-label">Extra instructions (optional)</span>
                  <textarea
                    className="field-control"
                    onChange={(event) => updateDraft({ extra: event.target.value })}
                    rows={3}
                    value={draft.extra}
                  />
                  <span className="character-count">
                    {charCount(draft.extra.trim())}/{SESSION_LIMITS.extra.toLocaleString("en-US")}
                  </span>
                  {fieldErrors.extra && <span className="field-error">{fieldErrors.extra}</span>}
                </label>
              </div>
            )}
          </section>

          <div className="capture-actions">
            <button className="button button-primary" disabled={submitting} type="submit">
              {submitting ? "Creating…" : "Create session"}
            </button>
            <button className="button button-secondary" disabled={submitting} onClick={cancel} type="button">
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
