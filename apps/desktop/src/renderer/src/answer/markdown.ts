/**
 * A deliberately small Markdown subset for answers (M-8): fenced code, headings, bullet and numbered
 * lists, paragraphs, and inline `code` and **bold**. Pure and dependency-free.
 *
 * Security: the output is a plain data structure that the renderer draws as React elements. There is
 * no HTML pass-through (a literal `<script>` shows as text), no links and no images (a `[x](url)` is
 * shown as written), so model output can never inject markup, navigate the window or load a resource.
 *
 * It tolerates a half-streamed answer: an unclosed fence is returned as a code block with
 * `closed: false`, and half-typed inline markers stay literal.
 */

export type Inline = { kind: "text" | "code" | "bold"; text: string };

export type Block =
  | { kind: "paragraph"; inline: Inline[] }
  | { kind: "heading"; level: 1 | 2 | 3; inline: Inline[] }
  | { kind: "list"; ordered: boolean; items: Inline[][] }
  | { kind: "code"; declared: string; code: string; closed: boolean };

const FENCE_OPEN = /^ {0,3}(`{3,})[ \t]*([^`\n]*?)[ \t]*$/;
const HEADING = /^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$/;
const BULLET = /^ {0,3}[-*+•][ \t]+(.+)$/;
const NUMBERED = /^ {0,3}\d{1,3}[.)][ \t]+(.+)$/;

/** Inline `code` and **bold**; everything else is literal text. Single `*` and `_` are left alone so
 * code-like prose (`a * b`, `snake_case`) is never mangled. */
export function parseInline(source: string): Inline[] {
  const out: Inline[] = [];
  let text = "";
  const flush = () => {
    if (text) out.push({ kind: "text", text });
    text = "";
  };
  let i = 0;
  while (i < source.length) {
    if (source.charAt(i) === "`") {
      const end = source.indexOf("`", i + 1);
      if (end > i + 1) {
        flush();
        out.push({ kind: "code", text: source.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    } else if (source.startsWith("**", i)) {
      const end = source.indexOf("**", i + 2);
      if (end > i + 2 && !/^\s|\s$/.test(source.slice(i + 2, end))) {
        flush();
        out.push({ kind: "bold", text: source.slice(i + 2, end) });
        i = end + 2;
        continue;
      }
    }
    text += source.charAt(i);
    i += 1;
  }
  flush();
  return out;
}

export function parseAnswer(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: Inline[][] } | null = null;

  const closeParagraph = () => {
    if (paragraph.length) blocks.push({ kind: "paragraph", inline: parseInline(paragraph.join("\n")) });
    paragraph = [];
  };
  const closeList = () => {
    if (list) blocks.push({ kind: "list", ...list });
    list = null;
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i] ?? "";
    const open = FENCE_OPEN.exec(line);
    if (open) {
      closeParagraph();
      closeList();
      const marker = open[1] ?? "```";
      const body: string[] = [];
      let closed = false;
      for (i += 1; i < lines.length; i += 1) {
        const inner = lines[i] ?? "";
        if (new RegExp(`^ {0,3}\`{${marker.length},}[ \\t]*$`).test(inner)) {
          closed = true;
          break;
        }
        body.push(inner);
      }
      blocks.push({ kind: "code", declared: open[2] ?? "", code: body.join("\n"), closed });
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      closeParagraph();
      closeList();
      const level = Math.min(3, (heading[1] ?? "#").length) as 1 | 2 | 3;
      blocks.push({ kind: "heading", level, inline: parseInline(heading[2] ?? "") });
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    const item = bullet ?? numbered;
    if (item) {
      closeParagraph();
      const ordered = !bullet;
      if (list && list.ordered !== ordered) closeList();
      list ??= { ordered, items: [] };
      list.items.push(parseInline(item[1] ?? ""));
      continue;
    }
    if (line.trim() === "") {
      closeParagraph();
      closeList();
      continue;
    }
    closeList();
    paragraph.push(line);
  }
  closeParagraph();
  closeList();
  return blocks;
}

/** The text of a code block as it should be copied: the code only, no fence and no label. */
export function codeOf(blocks: Block[], index: number): string {
  const block = blocks[index];
  return block?.kind === "code" ? block.code : "";
}
