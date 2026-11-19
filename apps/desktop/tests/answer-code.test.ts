/** M-8: language names, language detection and the display highlighter. */
import { describe, expect, it } from "vitest";
import {
  MAX_HIGHLIGHT_CHARS,
  canonicalLanguage,
  detectLanguage,
  highlight,
  languageLabel,
  normalizeCode,
  resolveLanguage,
} from "../src/renderer/src/answer/code";

describe("language names", () => {
  it.each([
    ["py", "python"], ["Python3", "python"], ["js", "javascript"], ["TSX", "typescript"], ["c++", "cpp"],
    ["C#", "csharp"], ["golang", "go"], ["PostgreSQL", "sql"], ["sh", "bash"], ["rust", "rust"], ["", ""],
  ])("%s -> %s", (label, expected) => expect(canonicalLanguage(label)).toBe(expected));

  it("ignores extra words in the fence info string", () => {
    expect(canonicalLanguage("python title=solution.py")).toBe("python");
    expect(canonicalLanguage("{.python}")).toBe("python");
  });

  it("labels known languages by name, and falls back to Code", () => {
    expect(languageLabel("cpp")).toBe("C++");
    expect(languageLabel("sql")).toBe("SQL");
    expect(languageLabel("")).toBe("Code");
    expect(languageLabel("<script>")).toBe("Code");
    expect(languageLabel("zig")).toBe("zig");
  });
});

describe("language detection for an unlabelled block", () => {
  it.each([
    ["python", "def two_sum(nums, target):\n    seen = {}\n    return None"],
    ["javascript", "const f = (a) => a + 1;\nconsole.log(f(1));"],
    ["java", "public class A {\n  public static void main(String[] a) { System.out.println(1); }\n}"],
    ["cpp", "#include <vector>\nint main() { std::vector<int> v; }"],
    ["go", "package main\nfunc main() {\n  x := 1\n}"],
    ["rust", "fn main() {\n    let mut x = 1;\n}"],
    ["sql", "SELECT dept, COUNT(*) FROM employees GROUP BY dept;"],
  ])("%s", (language, code) => expect(detectLanguage(code)).toBe(language));

  it("returns nothing when unsure, and a declared language always wins", () => {
    expect(detectLanguage("hello world")).toBe("");
    expect(resolveLanguage("java", "def f(x):\n    return None")).toBe("java");
    expect(resolveLanguage("", "SELECT 1 FROM t")).toBe("sql");
  });
});

describe("highlighting", () => {
  const join = (tokens: ReturnType<typeof highlight>) => tokens.map((t) => t.text).join("");

  it("is lossless: the tokens joined are the original code", () => {
    const code = 'def f(x):\n    # note\n    return "a\\"b" + str(10)  # end\n';
    expect(join(highlight(code, "python"))).toBe(code);
    const sql = "-- c\nSELECT name FROM t WHERE x = 'a' /* block */;";
    expect(join(highlight(sql, "sql"))).toBe(sql);
  });

  it("marks keywords, strings, comments and numbers", () => {
    const kinds = highlight('return 42 # done "x"', "python").map((t) => [t.kind, t.text]);
    expect(kinds).toContainEqual(["keyword", "return"]);
    expect(kinds).toContainEqual(["number", "42"]);
    expect(kinds).toContainEqual(["comment", '# done "x"']);
  });

  it("does not treat # inside a string as a comment, or a keyword inside a word", () => {
    const tokens = highlight('s = "a # b"\nfinally_x = 1', "python");
    expect(tokens.filter((t) => t.kind === "comment")).toEqual([]);
    expect(tokens.filter((t) => t.kind === "keyword")).toEqual([]);
  });

  it("SQL keywords match in any case", () => {
    const keywords = highlight("select 1 from t", "sql").filter((t) => t.kind === "keyword");
    expect(keywords.map((t) => t.text)).toEqual(["select", "from"]);
  });

  it("an unknown language or an oversized block is one plain token", () => {
    expect(highlight("whatever", "text")).toEqual([{ kind: "plain", text: "whatever" }]);
    expect(highlight("x = 1\n".repeat(MAX_HIGHLIGHT_CHARS), "python")).toHaveLength(1);
  });

  it("an unterminated string or block comment does not hang or lose text", () => {
    const code = "x = 'abc\ny = 2 /* never closed";
    expect(join(highlight(code, "javascript"))).toBe(code);
  });
});

describe("typographic characters in code", () => {
  it("are replaced with ASCII in a recognised language", () => {
    const messy = "# non‑empty list only\nx = ‘a’ + “b”​\ny = 1 − 2";
    expect(normalizeCode(messy, "python")).toBe("# non-empty list only\nx = 'a' + \"b\"\ny = 1 - 2");
  });

  it("leave sample input and output, plain text and unknown languages exactly as written", () => {
    const sample = "f([2, 7]) → [0, 1]  # k‑th";
    expect(normalizeCode(sample, "text")).toBe(sample);
    expect(normalizeCode(sample, "")).toBe(sample);
    expect(normalizeCode(sample, "zig")).toBe(sample);
  });

  it("do not touch ordinary non-ASCII text such as accents or Indic scripts", () => {
    const code = 'name = "café తెలుగు"';
    expect(normalizeCode(code, "python")).toBe(code);
  });
});
