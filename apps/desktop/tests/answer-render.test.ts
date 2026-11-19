// @vitest-environment jsdom
/**
 * M-8: the answer renderer in a DOM. Language labels, per-block Copy, long code, several blocks,
 * a half-streamed block, and that untrusted text never becomes elements.
 */
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MAX_COPY_LENGTH, validateCopyText } from "../src/main/ipc-validation";
import { AnswerBody } from "../src/renderer/src/ui/AnswerBody";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const FENCE = "```";

let host: HTMLDivElement;
let root: Root;
let copyText: ReturnType<typeof vi.fn<(text: string) => Promise<boolean>>>;

async function show(text: string) {
  await act(async () => {
    root.render(createElement(AnswerBody, { text, copyText }));
  });
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  copyText = vi.fn(async () => true);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

const answer = [
  "### Approach",
  "Scan once with a map.",
  `${FENCE}python`,
  "def two_sum(nums, target):",
  "    return []",
  FENCE,
  "### Tests",
  `${FENCE}`,
  "two_sum([2, 7], 9) -> [0, 1]",
  FENCE,
].join("\n");

describe("code block rendering", () => {
  it("shows a language label, line count and the code with its indentation", async () => {
    await show(answer);
    const blocks = host.querySelectorAll("figure.code-block");
    expect(blocks).toHaveLength(2);
    expect(blocks[0]?.querySelector(".code-lang")?.textContent).toBe("Python");
    expect(blocks[0]?.querySelector(".code-meta")?.textContent).toBe("2 lines");
    expect(blocks[0]?.querySelector("pre code")?.textContent).toBe("def two_sum(nums, target):\n    return []");
  });

  it("an unlabelled block gets a detected label, or Code when unsure", async () => {
    await show(`${FENCE}\nSELECT 1 FROM t;\n${FENCE}\n${FENCE}\nhello\n${FENCE}`);
    const labels = [...host.querySelectorAll(".code-lang")].map((n) => n.textContent);
    expect(labels).toEqual(["SQL", "Code"]);
  });

  it("separates explanation from code: headings and paragraphs are outside the figures", async () => {
    await show(answer);
    expect(host.querySelectorAll("[role=heading]")).toHaveLength(2);
    expect(host.querySelector("figure p")).toBeNull();
    expect(host.querySelector(".answer-rich > p.answer-text")?.textContent).toBe("Scan once with a map.");
  });

  it("long code stays inside one scrollable, keyboard-focusable block", async () => {
    const long = Array.from({ length: 300 }, (_, i) => `x${i} = ${i}  # ${"c".repeat(120)}`).join("\n");
    await show(`${FENCE}python\n${long}\n${FENCE}`);
    const pre = host.querySelector("pre.code-pre");
    expect(pre?.getAttribute("tabindex")).toBe("0");
    expect(host.querySelector(".code-meta")?.textContent).toBe("300 lines");
    expect(pre?.textContent).toBe(long);
    expect(host.querySelectorAll("figure.code-block")).toHaveLength(1);
  });

  it("a half-streamed block shows what has arrived and says it is still being written", async () => {
    await show(`${FENCE}python\ndef f(x):\n    ret`);
    expect(host.querySelector(".code-meta")?.textContent).toBe("writing…");
    expect(host.querySelector("pre code")?.textContent).toBe("def f(x):\n    ret");
  });
});

describe("copy", () => {
  it("copies exactly the code of the block clicked: no fence, no label, no explanation", async () => {
    await show(answer);
    const buttons = host.querySelectorAll<HTMLButtonElement>("button.code-copy");
    expect(buttons).toHaveLength(2);
    await act(async () => buttons[1]?.click());
    expect(copyText).toHaveBeenCalledTimes(1);
    expect(copyText).toHaveBeenCalledWith("two_sum([2, 7], 9) -> [0, 1]");
    await act(async () => buttons[0]?.click());
    expect(copyText).toHaveBeenLastCalledWith("def two_sum(nums, target):\n    return []");
  });

  it("each button names its language for assistive technology, and confirms the copy", async () => {
    await show(answer);
    const first = host.querySelector<HTMLButtonElement>("button.code-copy");
    expect(first?.getAttribute("aria-label")).toBe("Copy Python code");
    await act(async () => first?.click());
    expect(first?.textContent).toBe("Copied");
  });

  it("says so when the clipboard write fails, instead of claiming success", async () => {
    copyText.mockResolvedValueOnce(false);
    await show(answer);
    const first = host.querySelector<HTMLButtonElement>("button.code-copy");
    await act(async () => first?.click());
    expect(first?.textContent).toBe("Not copied");
  });

  it("a thrown clipboard error is also reported as not copied", async () => {
    copyText.mockRejectedValueOnce(new Error("denied"));
    await show(answer);
    const first = host.querySelector<HTMLButtonElement>("button.code-copy");
    await act(async () => first?.click());
    expect(first?.textContent).toBe("Not copied");
  });

  it("shows and copies code with typographic characters replaced by ASCII", async () => {
    await show(`${FENCE}python\n# k‑th largest value\ndef f(k):\n    return k\n${FENCE}`);
    expect(host.querySelector("pre code")?.textContent).toBe("# k-th largest value\ndef f(k):\n    return k");
    await act(async () => host.querySelector<HTMLButtonElement>("button.code-copy")?.click());
    expect(copyText).toHaveBeenCalledWith("# k-th largest value\ndef f(k):\n    return k");
  });

  it("the main process accepts a long answer with code for the Copy answer button", () => {
    const longAnswer = `### Code\n${FENCE}python\n${"x = 1\n".repeat(10_000)}${FENCE}`;
    expect(longAnswer.length).toBeLessThan(MAX_COPY_LENGTH);
    expect(validateCopyText(longAnswer)).toBe(longAnswer);
  });
});

describe("untrusted text never becomes elements", () => {
  it("renders HTML, links and images as text", async () => {
    await show(
      '<img src=x onerror="alert(1)"><script>alert(1)</script>\n[go](javascript:alert(1)) ![p](http://evil.example/p.png)',
    );
    expect(host.querySelector("img, script, a, iframe")).toBeNull();
    expect(host.textContent).toContain("<script>alert(1)</script>");
    expect(host.textContent).toContain("[go](javascript:alert(1))");
  });

  it("a hostile language label is not echoed as markup or a class", async () => {
    await show(`${FENCE}"><img src=x onerror=alert(1)>\ncode\n${FENCE}`);
    expect(host.querySelector("img")).toBeNull();
    expect(host.querySelector(".code-lang")?.textContent).toBe("Code");
  });
});

describe("plain answers are unchanged", () => {
  it("a spoken answer with no Markdown is one paragraph with its line breaks kept", async () => {
    await show("I led the migration.\nIt cut latency by half.");
    const paragraphs = host.querySelectorAll("p.answer-text");
    expect(paragraphs).toHaveLength(1);
    expect(paragraphs[0]?.textContent).toBe("I led the migration.\nIt cut latency by half.");
    expect(host.querySelector("figure")).toBeNull();
  });
});
