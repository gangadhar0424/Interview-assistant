import { describe, expect, it } from "vitest";
import { parseSseEvent } from "../src/main/sse";
import { parseAnswerSource, sourceLabel } from "../src/shared/answer-source";

const REF = {
  source_type: "document",
  source_id: "s1",
  name: "notes.pdf",
  heading: "40. Enhancement",
  validated: null,
};

const doneFrame = (extra: Record<string, unknown>) => ({
  event: "answer.done",
  data: JSON.stringify({
    answer_id: "a1",
    question_id: "q1",
    provider: "extractive",
    model: "document",
    metrics: { retrieval_ms: 5, first_token_ms: 9, total_ms: 20 },
    fallback_used: false,
    input_tokens: null,
    output_tokens: null,
    ...extra,
  }),
});

describe("answer source (D-66)", () => {
  it("is parsed from answer.done and its absence is not an error", () => {
    const withSource = parseSseEvent(doneFrame({ source: { kind: "document", refs: [REF] } }));
    expect(withSource).toMatchObject({ type: "answer.done", source: { kind: "document", refs: [REF] } });
    const legacy = parseSseEvent(doneFrame({}));
    expect(legacy).toMatchObject({ type: "answer.done", source: null });
  });

  it("treats anything unexpected as no source, never as an exception", () => {
    for (const junk of [null, undefined, 5, "x", [], {}, { kind: "nope", refs: [] }, { kind: "resume" }]) {
      expect(parseAnswerSource(junk)).toBeNull();
    }
    const parsed = parseAnswerSource({ kind: "resume", refs: [null, 3, { name: 7 }, REF] });
    expect(parsed?.refs.map((r) => r.name)).toEqual([null, "notes.pdf"]);
  });

  it("labels where an answer came from in plain words", () => {
    expect(sourceLabel(null)).toBeNull();
    expect(sourceLabel({ kind: "resume", refs: [] })).toBe("From your resume");
    expect(sourceLabel({ kind: "document", refs: [REF] })).toBe("From document: notes.pdf");
    expect(sourceLabel({ kind: "synthesis", refs: [] })).toBe("From your resume and documents");
    expect(sourceLabel({ kind: "synthesis", refs: [REF] })).toBe("Based on: notes.pdf");
    expect(sourceLabel({ kind: "general", refs: [{ ...REF, validated: true }] })).toBe(
      "General knowledge (validated)",
    );
    expect(sourceLabel({ kind: "general", refs: [{ ...REF, validated: false }] })).toBe(
      "General knowledge (not fully validated)",
    );
    expect(sourceLabel({ kind: "general", refs: [] })).toBe("General knowledge");
  });
});
