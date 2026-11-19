/**
 * Code-block helpers (M-8): language names and a small syntax highlighter. Pure and dependency-free.
 *
 * Nothing here produces HTML. `highlight` returns plain tokens that the renderer draws as React text
 * nodes, so model output can never inject markup. The highlighter is deliberately approximate (it is
 * a tokenizer, not a parser): good enough to make code scannable in a small window, never used for
 * anything but display.
 */

export type TokenKind = "plain" | "keyword" | "string" | "comment" | "number";
export interface CodeToken {
  kind: TokenKind;
  text: string;
}

const ALIASES: Record<string, string> = {
  py: "python",
  python3: "python",
  js: "javascript",
  node: "javascript",
  jsx: "javascript",
  ts: "typescript",
  tsx: "typescript",
  "c++": "cpp",
  cc: "cpp",
  cxx: "cpp",
  "c#": "csharp",
  cs: "csharp",
  golang: "go",
  rs: "rust",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  postgres: "sql",
  postgresql: "sql",
  mysql: "sql",
  sqlite: "sql",
  plaintext: "text",
  txt: "text",
};

const LABELS: Record<string, string> = {
  python: "Python",
  javascript: "JavaScript",
  typescript: "TypeScript",
  java: "Java",
  c: "C",
  cpp: "C++",
  csharp: "C#",
  go: "Go",
  rust: "Rust",
  sql: "SQL",
  bash: "Bash",
  json: "JSON",
  text: "Text",
};

/** The canonical language id for a fence label ("py" -> "python"), or "" when none is given. */
export function canonicalLanguage(label: string | undefined | null): string {
  const first = (label ?? "").trim().toLowerCase().split(/\s+/)[0] ?? "";
  const key = first.replace(/^[{.]+|[}]+$/g, "");
  return ALIASES[key] ?? key;
}

/** What to show on the block's label: a known name, the cleaned label as given, or "Code". */
export function languageLabel(language: string): string {
  if (!language) return "Code";
  return LABELS[language] ?? (/^[a-z0-9+#._-]{1,20}$/.test(language) ? language : "Code");
}

const DETECTORS: Array<[string, RegExp, number]> = [
  ["python", /^\s*def \w+\(.*\)\s*(?:->\s*[\w[\], .|]+)?:\s*$/m, 3],
  ["python", /^\s*(?:from \w[\w.]* import |import \w+\s*$)/m, 2],
  ["python", /\bself\b|\bNone\b|\bTrue\b|\bFalse\b|\blen\(|^\s*(?:elif|for .+ in .+:|while .+:)/m, 1],
  ["typescript", /: (?:number|string|boolean)\b|\binterface \w+/, 3],
  ["javascript", /\bconst \w+ =|\blet \w+ =|=>|\bfunction \w*\(|console\.log/, 3],
  ["java", /\bpublic (?:static )?(?:class|int|void|boolean|String)\b|System\.out/, 3],
  ["cpp", /#include\s*<|std::|\bcout\b|\bvector<|\bint main\(/, 3],
  ["go", /^\s*func \w*\(|^package \w+|\bfmt\.|:= /m, 3],
  ["rust", /\bfn \w+\(|\blet mut\b|println!/, 3],
  ["sql", /\b(?:select|insert into|update|delete from|create table)\b/i, 3],
  ["bash", /^#!\/bin\/(?:ba)?sh|^\s*(?:echo|grep|awk|sed|cd) \S/m, 2],
  ["json", /^\s*[{[][\s\S]*:[\s\S]*[}\]]\s*$/, 1],
];

/** A best-effort guess for a block whose fence names no language. Returns "" when unsure. */
export function detectLanguage(code: string): string {
  const scores = new Map<string, number>();
  for (const [lang, pattern, weight] of DETECTORS) {
    if (pattern.test(code)) scores.set(lang, (scores.get(lang) ?? 0) + weight);
  }
  if (scores.get("typescript")) {
    scores.set("typescript", (scores.get("typescript") ?? 0) + (scores.get("javascript") ?? 0));
  }
  let best = "";
  let bestScore = 0;
  for (const [lang, score] of scores) {
    if (score > bestScore) {
      best = lang;
      bestScore = score;
    }
  }
  return bestScore >= 3 ? best : "";
}

/** The language to treat a block as: its declared one, else a guess. */
export function resolveLanguage(declared: string, code: string): string {
  return canonicalLanguage(declared) || detectLanguage(code);
}

const words = (list: string) => new Set(list.split(" "));
const KEYWORDS: Record<string, Set<string>> = {
  python: words(
    "def class return if elif else for while in not and or is import from as with try except finally raise yield lambda pass break continue None True False global nonlocal assert del async await",
  ),
  javascript: words(
    "function const let var return if else for while do switch case break continue new class extends import export from default try catch finally throw async await typeof instanceof in of null undefined true false this yield",
  ),
  typescript: words(
    "function const let var return if else for while do switch case break continue new class extends implements import export from default try catch finally throw async await typeof instanceof in of null undefined true false this interface type enum readonly public private protected number string boolean void",
  ),
  java: words(
    "public private protected static final class interface extends implements return if else for while do switch case break continue new try catch finally throw throws import package void int long double boolean char String null true false this super",
  ),
  c: words(
    "int long short char float double void unsigned signed const static struct enum typedef return if else for while do switch case break continue sizeof NULL include define",
  ),
  cpp: words(
    "int long short char float double void unsigned signed const static struct class enum typedef template typename namespace using auto return if else for while do switch case break continue new delete try catch throw nullptr true false include define public private protected virtual",
  ),
  csharp: words(
    "using namespace class struct interface public private protected static readonly void int long double bool string var new return if else for foreach while do switch case break continue try catch finally throw null true false this base async await",
  ),
  go: words(
    "func package import return if else for range switch case break continue go defer chan map struct interface type var const nil true false select fallthrough",
  ),
  rust: words(
    "fn let mut const static struct enum impl trait pub use mod return if else for while loop match break continue as in ref move where self Self true false Some None Ok Err",
  ),
  sql: words(
    "select from where group by order having join inner left right outer full on as and or not in is null like limit offset distinct count sum avg min max insert into values update set delete create table index primary key foreign references union all case when then else end asc desc over partition with exists between",
  ),
  bash: words("if then else elif fi for while do done case esac function in return exit echo export local"),
};

const HASH_COMMENT = new Set(["python", "bash"]);
const SLASH_COMMENT = new Set(["javascript", "typescript", "java", "c", "cpp", "csharp", "go", "rust"]);
/** A block bigger than this is shown unhighlighted: keeps rendering fast while a long answer streams. */
export const MAX_HIGHLIGHT_CHARS = 20_000;

/** Tokenize one block for display. Lossless: joining every `text` gives back `code` exactly. */
export function highlight(code: string, language: string): CodeToken[] {
  const keywords = KEYWORDS[language];
  if (!keywords && language !== "json") return [{ kind: "plain", text: code }];
  if (code.length > MAX_HIGHLIGHT_CHARS) return [{ kind: "plain", text: code }];
  const caseless = language === "sql";
  const tokens: CodeToken[] = [];
  let plain = "";
  const flush = () => {
    if (plain) tokens.push({ kind: "plain", text: plain });
    plain = "";
  };
  const push = (kind: TokenKind, text: string) => {
    flush();
    tokens.push({ kind, text });
  };
  let i = 0;
  while (i < code.length) {
    const c = code.charAt(i);
    const next = code.charAt(i + 1);
    if (
      (HASH_COMMENT.has(language) && c === "#") ||
      (language === "sql" && c === "-" && next === "-") ||
      (SLASH_COMMENT.has(language) && c === "/" && next === "/")
    ) {
      let end = code.indexOf("\n", i);
      if (end < 0) end = code.length;
      push("comment", code.slice(i, end));
      i = end;
    } else if ((SLASH_COMMENT.has(language) || language === "sql") && c === "/" && next === "*") {
      let end = code.indexOf("*/", i + 2);
      end = end < 0 ? code.length : end + 2;
      push("comment", code.slice(i, end));
      i = end;
    } else if (c === '"' || c === "'" || (c === "`" && language !== "sql")) {
      let j = i + 1;
      while (j < code.length && code.charAt(j) !== c && code.charAt(j) !== "\n") {
        j += code.charAt(j) === "\\" ? 2 : 1;
      }
      j = Math.min(code.length, j + 1);
      push("string", code.slice(i, j));
      i = j;
    } else if (/[0-9]/.test(c) && !/[\w]/.test(code.charAt(i - 1))) {
      let j = i + 1;
      while (j < code.length && /[0-9a-fA-F_.xXeE]/.test(code.charAt(j))) j += 1;
      push("number", code.slice(i, j));
      i = j;
    } else if (/[A-Za-z_]/.test(c)) {
      let j = i + 1;
      while (j < code.length && /[A-Za-z0-9_]/.test(code.charAt(j))) j += 1;
      const word = code.slice(i, j);
      if (keywords?.has(caseless ? word.toLowerCase() : word)) push("keyword", word);
      else plain += word;
      i = j;
    } else {
      plain += c;
      i += 1;
    }
  }
  flush();
  return tokens;
}

const CODE_LANGUAGES = new Set([...Object.keys(KEYWORDS), "json"]);
const ZERO_WIDTH = /[\u200B-\u200D\u2060\uFEFF]/g;
const ODD_SPACES = /[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]/g;
const ODD_DASHES = /[\u2010-\u2015\u2212]/g;
const CURLY_SINGLE = /[\u2018\u2019]/g;
const CURLY_DOUBLE = /[\u201C\u201D]/g;

/**
 * The code to show and to copy. Language models sometimes write typographic characters (a
 * non-breaking hyphen, a narrow no-break space, curly quotes, zero-width characters) inside code
 * (seen in live Groq output); pasted into an editor they are syntax errors or invisible bugs. For a
 * recognised programming language they are replaced with their ASCII equivalents; any other block
 * (sample input and output, plain text) is left exactly as written. A typographic character that was
 * deliberately inside a string literal is normalised too: for code that is the safer error.
 */
export function normalizeCode(code: string, language: string): string {
  if (!CODE_LANGUAGES.has(language)) return code;
  return code
    .replace(ZERO_WIDTH, "")
    .replace(ODD_SPACES, " ")
    .replace(ODD_DASHES, "-")
    .replace(CURLY_SINGLE, "'")
    .replace(CURLY_DOUBLE, '"');
}
