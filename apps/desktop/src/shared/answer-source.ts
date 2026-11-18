// Where an answer came from (D-66): exact text of a file, a model answer from related passages, or
// general knowledge. References carry ids, a file name and a heading, never source text. Anything
// unexpected parses to "no source": the label is a convenience and must never break an answer.

export type AnswerSourceRef = {
  source_type: string | null;
  source_id: string | null;
  name: string | null;
  heading: string | null;
  validated: boolean | null;
};

export type AnswerSourceKind = "resume" | "document" | "synthesis" | "general";
export type AnswerSource = { kind: AnswerSourceKind; refs: AnswerSourceRef[] };

const KINDS: readonly string[] = ["resume", "document", "synthesis", "general"];

export function parseAnswerSource(raw: unknown): AnswerSource | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const value = raw as Record<string, unknown>;
  const kind = value.kind;
  if (typeof kind !== "string" || !KINDS.includes(kind) || !Array.isArray(value.refs)) return null;
  const text = (item: Record<string, unknown>, key: string): string | null =>
    typeof item[key] === "string" ? (item[key] as string) : null;
  const refs: AnswerSourceRef[] = [];
  for (const entry of value.refs as unknown[]) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) continue;
    const item = entry as Record<string, unknown>;
    refs.push({
      source_type: text(item, "source_type"),
      source_id: text(item, "source_id"),
      name: text(item, "name"),
      heading: text(item, "heading"),
      validated: typeof item.validated === "boolean" ? item.validated : null,
    });
  }
  return { kind: kind as AnswerSourceKind, refs };
}

/** A short label for where an answer came from, or null when none was recorded. */
export function sourceLabel(source: AnswerSource | null | undefined): string | null {
  if (!source) return null;
  const first = source.refs[0];
  switch (source.kind) {
    case "resume":
      return "From your resume";
    case "document":
      return first?.name ? `From document: ${first.name}` : "From your document";
    case "synthesis":
      return first?.name ? `Based on: ${first.name}` : "From your resume and documents";
    case "general":
      return first?.validated === false
        ? "General knowledge (not fully validated)"
        : first?.validated === true
          ? "General knowledge (validated)"
          : "General knowledge";
  }
}
