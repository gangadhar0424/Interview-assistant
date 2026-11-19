import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { highlight, languageLabel, normalizeCode, resolveLanguage } from "../answer/code";
import { parseAnswer, type Block, type Inline } from "../answer/markdown";

/**
 * Draws an answer (M-8): explanation text and fenced code blocks, each code block with a language
 * label, scrolling, and its own Copy button. Everything is built from React elements and text nodes
 * from the small parser in ../answer/markdown, so model output is never interpreted as HTML.
 *
 * `copyText` is injected (the app passes the main-process clipboard bridge) so the component stays
 * testable; it resolves true when the text reached the clipboard.
 */
export type CopyText = (text: string) => Promise<boolean>;

function InlineText({ parts }: { parts: Inline[] }): ReactNode {
  return parts.map((part, index) => {
    if (part.kind === "code") return <code className="answer-inline-code" key={index}>{part.text}</code>;
    if (part.kind === "bold") return <strong key={index}>{part.text}</strong>;
    return <Fragment key={index}>{part.text}</Fragment>;
  });
}

function CopyButton({ label, getText, copyText }: { label: string; getText: () => string; copyText: CopyText }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  async function copy() {
    let ok = false;
    try {
      ok = await copyText(getText());
    } catch {
      ok = false;
    }
    setState(ok ? "copied" : "failed");
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 1600);
  }
  return (
    <button aria-label={label} className="code-copy" onClick={() => void copy()} type="button">
      {state === "copied" ? "Copied" : state === "failed" ? "Not copied" : "Copy"}
    </button>
  );
}

export function CodeBlockView({
  declared,
  code,
  closed,
  copyText,
}: {
  declared: string;
  code: string;
  closed: boolean;
  copyText: CopyText;
}) {
  const language = useMemo(() => resolveLanguage(declared, code), [declared, code]);
  // What is shown is what Copy puts on the clipboard: typographic characters are replaced in code.
  const shown = useMemo(() => normalizeCode(code, language), [code, language]);
  const tokens = useMemo(() => highlight(shown, language), [shown, language]);
  const label = languageLabel(language);
  const lines = shown === "" ? 0 : shown.split("\n").length;
  return (
    <figure className="code-block">
      <figcaption className="code-header">
        <span className="code-lang">{label}</span>
        <span className="code-meta">
          {closed ? `${lines} line${lines === 1 ? "" : "s"}` : "writing…"}
        </span>
        <CopyButton copyText={copyText} getText={() => shown} label={`Copy ${label} code`} />
      </figcaption>
      <pre className="code-pre" tabIndex={0}>
        <code>
          {tokens.map((token, index) =>
            token.kind === "plain" ? (
              <Fragment key={index}>{token.text}</Fragment>
            ) : (
              <span className={`tok-${token.kind}`} key={index}>
                {token.text}
              </span>
            ),
          )}
        </code>
      </pre>
    </figure>
  );
}

function BlockView({ block, copyText }: { block: Block; copyText: CopyText }) {
  switch (block.kind) {
    case "code":
      return <CodeBlockView {...block} copyText={copyText} />;
    case "heading":
      return (
        <p className={`answer-heading answer-heading-${block.level}`} role="heading" aria-level={block.level + 2}>
          <InlineText parts={block.inline} />
        </p>
      );
    case "list": {
      const Tag = block.ordered ? "ol" : "ul";
      return (
        <Tag className="answer-list">
          {block.items.map((item, index) => (
            <li key={index}>
              <InlineText parts={item} />
            </li>
          ))}
        </Tag>
      );
    }
    default:
      return (
        <p className="answer-text">
          <InlineText parts={block.inline} />
        </p>
      );
  }
}

export function AnswerBody({ text, copyText }: { text: string; copyText: CopyText }) {
  const blocks = useMemo(() => parseAnswer(text), [text]);
  return (
    <div className="answer-rich">
      {blocks.map((block, index) => (
        <BlockView block={block} copyText={copyText} key={index} />
      ))}
    </div>
  );
}
