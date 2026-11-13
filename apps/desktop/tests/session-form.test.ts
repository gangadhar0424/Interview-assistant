import { describe, expect, it } from "vitest";
import {
  charCount,
  defaultDraft,
  hasErrors,
  optionLabel,
  parseDraft,
  SESSION_LIMITS,
  styleSummary,
  toCreateBody,
  validateDraft,
  webAppCreateLink,
  type SessionDraft,
  type SessionOptions,
} from "../src/shared/session-form";

const RESUME = "11111111-2222-4333-8444-555555555555";
const DOC_A = "aaaaaaaa-2222-4333-8444-555555555555";
const DOC_B = "bbbbbbbb-2222-4333-8444-555555555555";

const options: SessionOptions = {
  languages: ["en", "hi", "te"],
  question_types: ["auto", "behavioral", "coding"],
  formats: ["full_script", "script_bullets", "bullets"],
  lengths: ["short", "balanced", "long"],
  tones: ["simple", "formal"],
  providers: [{ id: "groq", label: "Groq" }, { id: "anthropic", label: "Claude" }],
  voice_providers: [{ id: "sarvam", label: "Sarvam" }, { id: "openai", label: "OpenAI" }],
};

function valid(over: Partial<SessionDraft> = {}): SessionDraft {
  return {
    ...defaultDraft(options),
    provider: "groq",
    sttProvider: "sarvam",
    company: "Acme",
    description: "Backend engineer, Python and PostgreSQL.",
    resumeId: RESUME,
    ...over,
  };
}

describe("defaults come from the options the backend offers", () => {
  it("uses the web wizard defaults when they are offered", () => {
    expect(defaultDraft(options)).toMatchObject({
      language: "en",
      questionType: "auto",
      format: "full_script",
      length: "balanced",
      tone: "simple",
      resumeId: null,
      documentIds: [],
    });
  });

  it("falls back to the first offered value instead of inventing one", () => {
    const draft = defaultDraft({ ...options, languages: ["hi", "te"], tones: ["formal"] });
    expect(draft.language).toBe("hi");
    expect(draft.tone).toBe("formal");
  });

  it("works before the options are loaded", () => {
    expect(defaultDraft(null).language).toBe("en");
  });

  it("labels values readably and summarises the chosen style", () => {
    expect(optionLabel("hi")).toBe("Hindi");
    expect(optionLabel("script_bullets")).toBe("Script bullets");
    expect(styleSummary(defaultDraft(options))).toBe("English · Auto · Full script · Balanced · Simple");
  });
});

describe("required fields", () => {
  it("accepts a valid minimal draft", () => {
    expect(validateDraft(valid(), options)).toEqual({});
  });

  it("requires company, description and an explicit resume", () => {
    const errors = validateDraft(valid({ company: "   ", description: "\n\t", resumeId: null }), options);
    expect(errors.company).toBeDefined();
    expect(errors.description).toBeDefined();
    expect(errors.resumeId).toBe("Choose a resume.");
  });

  it("the title is optional", () => {
    expect(validateDraft(valid({ title: "" }), options)).toEqual({});
  });
});

describe("limits mirror the backend defaults and are counted in code points", () => {
  it("accepts text exactly at the limits and refuses one over", () => {
    expect(hasErrors(validateDraft(valid({ company: "a".repeat(SESSION_LIMITS.name) }), options))).toBe(false);
    expect(validateDraft(valid({ company: "a".repeat(SESSION_LIMITS.name + 1) }), options).company).toBeDefined();
    expect(validateDraft(valid({ title: "t".repeat(SESSION_LIMITS.name + 1) }), options).title).toBeDefined();
    expect(hasErrors(validateDraft(valid({ description: "d".repeat(SESSION_LIMITS.description) }), options))).toBe(false);
    expect(validateDraft(valid({ description: "d".repeat(SESSION_LIMITS.description + 1) }), options).description).toBeDefined();
    expect(validateDraft(valid({ extra: "e".repeat(SESSION_LIMITS.extra + 1) }), options).extra).toBeDefined();
  });

  it("counts emoji and other astral characters as one, like the server", () => {
    expect(charCount("😀😀")).toBe(2);
    expect("😀😀".length).toBe(4);
    expect(hasErrors(validateDraft(valid({ company: "😀".repeat(SESSION_LIMITS.name) }), options))).toBe(false);
  });

  it("trims but never rewrites or truncates", () => {
    const body = toCreateBody(valid({ company: "  Acme  ", description: "  Needs\nPython  " })) as Record<string, unknown>;
    expect(body.company_name).toBe("Acme");
    expect(body.job_description).toBe("Needs\nPython");
  });
});

describe("resume and document selection", () => {
  const readyResumes = new Set([RESUME]);
  const readyDocs = new Set([DOC_A, DOC_B]);

  it("only a ready resume can be chosen", () => {
    expect(validateDraft(valid(), options, readyResumes, readyDocs)).toEqual({});
    expect(validateDraft(valid({ resumeId: "99999999-2222-4333-8444-555555555555" }), options, readyResumes, readyDocs).resumeId).toBe(
      "Choose a resume that is ready.",
    );
    expect(validateDraft(valid({ resumeId: "not-an-id" }), options).resumeId).toBeDefined();
  });

  it("documents must be ready, unique, valid and within the limit", () => {
    expect(validateDraft(valid({ documentIds: [DOC_A, DOC_B] }), options, readyResumes, readyDocs)).toEqual({});
    expect(validateDraft(valid({ documentIds: [DOC_A, DOC_A] }), options).documentIds).toBe("Each document can only be chosen once.");
    expect(validateDraft(valid({ documentIds: ["nope"] }), options).documentIds).toBeDefined();
    expect(
      validateDraft(valid({ documentIds: ["cccccccc-2222-4333-8444-555555555555"] }), options, readyResumes, readyDocs).documentIds,
    ).toBe("Choose only documents that are ready.");
    const many = Array.from({ length: SESSION_LIMITS.maxDocuments + 1 }, (_, i) => `00000000-2222-4333-8444-${String(i).padStart(12, "0")}`);
    expect(validateDraft(valid({ documentIds: many }), options).documentIds).toContain("at most 10");
    expect(hasErrors(validateDraft(valid({ documentIds: many.slice(0, 10) }), options))).toBe(false);
  });
});

describe("preference values must be offered", () => {
  it("rejects values the backend does not offer and malformed values", () => {
    const errors = validateDraft(
      valid({ language: "fr", questionType: "../x", format: "", length: "huge", tone: "Rude" }),
      options,
    );
    expect(Object.keys(errors).sort()).toEqual(["format", "language", "length", "questionType", "tone"]);
  });
});

describe("provider selection (D-57)", () => {
  it("nothing is preselected when several are offered; a single choice is", () => {
    expect(defaultDraft(options)).toMatchObject({ provider: "", sttProvider: "" });
    expect(
      defaultDraft({ ...options, providers: [{ id: "groq", label: "Groq" }], voice_providers: [{ id: "sarvam", label: "Sarvam" }] }),
    ).toMatchObject({ provider: "groq", sttProvider: "sarvam" });
    expect(defaultDraft(null)).toMatchObject({ provider: "", sttProvider: "" });
  });

  it("both are mandatory and must come from the matching server list", () => {
    expect(validateDraft(valid(), options)).toEqual({});
    expect(validateDraft(valid({ provider: "" }), options).provider).toBe("Choose a reasoning model.");
    expect(validateDraft(valid({ sttProvider: "" }), options).sttProvider).toBe("Choose a voice listening model.");
    expect(validateDraft(valid({ provider: "openai" }), options).provider).toBeDefined(); // voice-only id
    expect(validateDraft(valid({ sttProvider: "groq" }), options).sttProvider).toBeDefined(); // reasoning-only id
    expect(validateDraft(valid({ provider: "nope" }), options).provider).toBeDefined();
  });

  it("with no server list (main process) only the shape is checked, and emptiness is refused", () => {
    expect(validateDraft(valid(), null)).toEqual({});
    expect(validateDraft(valid({ provider: "" }), null).provider).toBeDefined();
    expect(validateDraft(valid({ sttProvider: "../x" }), null).sttProvider).toBeDefined();
  });

  it("the body carries both providers and no model", () => {
    const prefs = (toCreateBody(valid()) as { preferences: Record<string, unknown> }).preferences;
    expect(prefs).toMatchObject({ provider: "groq", stt_provider: "sarvam" });
    expect("model" in prefs || "stt_model" in prefs).toBe(false);
  });
});

describe("the request body", () => {
  it("is exactly the backend contract and never carries identity or status", () => {
    const body = toCreateBody(valid({ title: "Backend Engineer", documentIds: [DOC_A], extra: "Keep it brief." }));
    expect(body).toEqual({
      type: "interview",
      company_name: "Acme",
      title: "Backend Engineer",
      job_description: "Backend engineer, Python and PostgreSQL.",
      resume_id: RESUME,
      document_ids: [DOC_A],
      preferences: {
        language: "en",
        question_type: "auto",
        answer_format: "full_script",
        answer_length: "balanced",
        answer_tone: "simple",
        extra_instructions: "Keep it brief.",
        provider: "groq",
        stt_provider: "sarvam",
      },
    });
    const minimal = toCreateBody(valid()) as { preferences: Record<string, unknown> } & Record<string, unknown>;
    expect("title" in minimal).toBe(false);
    expect("extra_instructions" in minimal.preferences).toBe(false);
    expect("job_url" in minimal).toBe(false); // job-link import is not part of this phase
    expect(JSON.stringify(minimal)).not.toMatch(/user_id|status|"model"|stt_model/);
  });
});

describe("parsing an untrusted draft", () => {
  it("accepts a well-formed draft", () => {
    expect(parseDraft(valid())).toEqual(valid());
  });

  it.each([
    ["null", null],
    ["array", []],
    ["string", "x"],
    ["extra key", { ...valid(), user_id: "me" }],
    ["number for text", { ...valid(), company: 5 }],
    ["documents not an array", { ...valid(), documentIds: "a" }],
    ["document id not a string", { ...valid(), documentIds: [1] }],
    ["resume id wrong type", { ...valid(), resumeId: 7 }],
    ["missing field", (() => { const { tone: _tone, ...rest } = valid(); return rest; })()],
  ])("refuses %s", (_name, value) => {
    expect(() => parseDraft(value)).toThrow();
  });
});

describe("the route to the web app when there is no ready resume", () => {
  it("is known only for a local backend, otherwise the UI explains it in words", () => {
    expect(webAppCreateLink("http://127.0.0.1:8000")).toBe("http://localhost:3000/sessions/new");
    expect(webAppCreateLink("http://localhost:8000")).toBe("http://localhost:3000/sessions/new");
    expect(webAppCreateLink("https://api.example.com")).toBeNull();
    expect(webAppCreateLink("not a url")).toBeNull();
  });
});
