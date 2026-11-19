/** M-8: the answer Markdown subset. Pure parser tests: structure, code blocks, streaming, no HTML. */
import { describe, expect, it } from "vitest";
import { codeOf, parseAnswer, parseInline } from "../src/renderer/src/answer/markdown";

const FENCE = "```";

describe("coding answer structure", () => {
  const answer = [
    "### Problem",
    "Return the indices of two numbers that add up to the target.",
    "",
    "### Approach",
    "- Keep a map of value to index.",
    "- Look up the complement while scanning.",
    "",
    "### Code",
    `${FENCE}python`,
    "def two_sum(nums, target):",
    "    seen = {}",
    "    return []",
    FENCE,
    "",
    "### Complexity",
    "`O(n)` time and **O(n)** space.",
  ].join("\n");

  it("splits headings, paragraphs, lists and one closed code block in order", () => {
    const blocks = parseAnswer(answer);
    expect(blocks.map((b) => b.kind)).toEqual([
      "heading", "paragraph", "heading", "list", "heading", "code", "heading", "paragraph",
    ]);
    expect(blocks[5]).toMatchObject({ kind: "code", declared: "python", closed: true });
    expect(codeOf(blocks, 5)).toBe("def two_sum(nums, target):\n    seen = {}\n    return []");
  });

  it("keeps indentation and blank lines inside code exactly", () => {
    const text = `${FENCE}python\nif x:\n\n    y = 1\n${FENCE}`;
    expect(codeOf(parseAnswer(text), 0)).toBe("if x:\n\n    y = 1");
  });

  it("parses inline code and bold, and leaves single * and _ alone", () => {
    expect(parseInline("use `a * b` and **fast** snake_case 2*3")).toEqual([
      { kind: "text", text: "use " },
      { kind: "code", text: "a * b" },
      { kind: "text", text: " and " },
      { kind: "bold", text: "fast" },
      { kind: "text", text: " snake_case 2*3" },
    ]);
  });
});

describe("multiple code blocks", () => {
  it("returns every block with its own language and code", () => {
    const text = [
      `${FENCE}python`, "a = 1", FENCE, "between", `${FENCE}sql`, "SELECT 1;", FENCE, FENCE, "plain", FENCE,
    ].join("\n");
    const code = parseAnswer(text).filter((b) => b.kind === "code");
    expect(code.map((b) => (b.kind === "code" ? [b.declared, b.code] : []))).toEqual([
      ["python", "a = 1"],
      ["sql", "SELECT 1;"],
      ["", "plain"],
    ]);
  });

  it("a longer fence may contain a shorter one", () => {
    const text = ["````markdown", `${FENCE}python`, "x = 1", FENCE, "````"].join("\n");
    const blocks = parseAnswer(text);
    expect(blocks[0]).toMatchObject({ kind: "code", closed: true });
    expect(codeOf(blocks, 0)).toBe(`${FENCE}python\nx = 1\n${FENCE}`);
  });
});

describe("a half-streamed answer", () => {
  it("an unclosed fence is a code block marked not closed, with the code so far", () => {
    const blocks = parseAnswer(`Here it is:\n${FENCE}python\ndef f(x):\n    ret`);
    expect(blocks[0]?.kind).toBe("paragraph");
    expect(blocks[1]).toMatchObject({ kind: "code", declared: "python", closed: false, code: "def f(x):\n    ret" });
  });

  it("a half-typed inline marker stays literal", () => {
    expect(parseInline("the **key idea")).toEqual([{ kind: "text", text: "the **key idea" }]);
    expect(parseInline("a `code")).toEqual([{ kind: "text", text: "a `code" }]);
  });

  it("two backticks are not a fence yet", () => {
    expect(parseAnswer("``").map((b) => b.kind)).toEqual(["paragraph"]);
  });

  it("empty and whitespace-only text give no blocks", () => {
    expect(parseAnswer("")).toEqual([]);
    expect(parseAnswer("  \n\n ")).toEqual([]);
  });

  it("handles Windows line endings", () => {
    const blocks = parseAnswer(`${FENCE}py\r\nx = 1\r\n${FENCE}`);
    expect(blocks[0]).toMatchObject({ kind: "code", declared: "py", code: "x = 1", closed: true });
  });
});

describe("untrusted text is never interpreted", () => {
  it("raw HTML stays literal text in a paragraph", () => {
    const text = '<img src=x onerror="alert(1)"> <script>alert(1)</script>';
    expect(parseAnswer(text)).toEqual([{ kind: "paragraph", inline: [{ kind: "text", text }] }]);
  });

  it("links and images are not parsed: they stay as written", () => {
    const text = "[click](javascript:alert(1)) ![x](http://evil.example/p.png)";
    expect(parseAnswer(text)).toEqual([{ kind: "paragraph", inline: [{ kind: "text", text }] }]);
  });

  it("HTML inside a code block is just code", () => {
    expect(codeOf(parseAnswer(`${FENCE}html\n<script>x()</script>\n${FENCE}`), 0)).toBe("<script>x()</script>");
  });
});
