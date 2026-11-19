// @vitest-environment jsdom
/**
 * M-8: ten real model answers to the synthetic coding prompts (tests/fixtures/coding-answers.json,
 * Groq, prompt v5) go through the parser and the renderer. This is the stand-in for "a sample of 10
 * coding prompts renders correctly": structure and text are asserted here; how it looks at compact
 * and expanded window sizes is a manual check (docs/DESKTOP_MANUAL_CHECKLIST.md).
 */
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseAnswer } from "../src/renderer/src/answer/markdown";
import { AnswerBody } from "../src/renderer/src/ui/AnswerBody";
import fixtureJson from "./fixtures/coding-answers.json";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface Fixture {
  answers: Array<{ id: string; prompt: string; text: string }>;
}
const fixture: Fixture = fixtureJson;
const FENCE = "```";

let host: HTMLDivElement;
let root: Root;
const copyText = vi.fn(async (_text: string) => true);

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

describe("ten real coding answers", () => {
  it("has ten answers", () => expect(fixture.answers).toHaveLength(10));

  it.each(fixture.answers.map((a) => [a.id, a.text] as const))("%s: structure parses and renders", async (_id, text) => {
    const blocks = parseAnswer(text);
    const fences = text.split("\n").filter((line) => line.trim().startsWith(FENCE)).length;
    const codeBlocks = blocks.filter((b) => b.kind === "code");
    // every fence line is accounted for, and no block is left open in a finished answer
    expect(codeBlocks.length * 2).toBe(fences);
    expect(codeBlocks.every((b) => b.kind === "code" && b.closed)).toBe(true);
    // no Markdown syntax is left showing as text
    for (const block of blocks) {
      if (block.kind === "paragraph") {
        for (const part of block.inline) expect(part.text.startsWith("### ")).toBe(false);
      }
    }
    await act(async () => {
      root.render(createElement(AnswerBody, { text, copyText }));
    });
    expect(host.querySelectorAll("figure.code-block")).toHaveLength(codeBlocks.length);
    expect(host.textContent).not.toContain("###");
    expect(host.textContent).not.toContain(FENCE);
    expect(host.querySelector("script, img, a, iframe")).toBeNull();
    // every code block has a visible, non-empty label
    for (const label of host.querySelectorAll(".code-lang")) expect(label.textContent?.length).toBeGreaterThan(0);
  });

  it("the Python solutions render with the Python label and no typographic characters left", async () => {
    const answer = fixture.answers.find((a) => a.id === "arr-two-sum");
    expect(answer).toBeDefined();
    await act(async () => {
      root.render(createElement(AnswerBody, { text: answer?.text ?? "", copyText }));
    });
    const labels = [...host.querySelectorAll(".code-lang")].map((n) => n.textContent);
    expect(labels).toContain("Python");
    for (const code of host.querySelectorAll("pre code")) {
      expect(code.textContent ?? "").not.toMatch(/[‐-―‘’“”  ]/);
    }
  });
});
