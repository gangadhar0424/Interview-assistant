"""Generic job-page extraction (no site-specific adapters, owner decision 12).

1. schema.org JSON-LD `JobPosting` (what most job boards embed): company, title, description.
2. Fallback heuristics: Open Graph / <title> for title and company, <article>/<main> text (or the
   meta description) for the description.

Everything returned is plain text for the user to review and edit. It is cleaned, length-capped,
never contains markup, and is NEVER treated as instructions (it is untrusted page content).
"""

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from app.preferences.sanitize import clean_text

MAX_NAME_CHARS = 200
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "nav", "header",
                        "footer", "aside", "form"})  # fmt: skip
_BLOCK_TAGS = frozenset({"p", "br", "div", "section", "article", "main", "h1", "h2", "h3", "h4",
                         "h5", "h6", "tr", "ul", "ol", "table", "blockquote"})  # fmt: skip
_VOID_TAGS = frozenset({"br", "meta", "link", "img", "input", "hr", "area", "base", "col",
                        "embed", "source", "track", "wbr"})  # fmt: skip
_MAX_JSON_NODES = 5000
_CHARSET_META = re.compile(rb"charset=[\"']?([A-Za-z0-9_\-]+)", re.IGNORECASE)
_MULTI_SPACE = re.compile(r" {2,}")


@dataclass(frozen=True)
class JobInfo:
    company: str | None
    title: str | None
    description: str | None
    structured: bool  # True when the data came from JSON-LD (trusted structure, not a guess)


class _TextExtractor(HTMLParser):
    """HTML fragment to plain text, keeping paragraph and list structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS and tag not in _VOID_TAGS:
            self._skip += 1
        elif tag == "li":
            if self.parts and not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
            self.parts.append("- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS or tag == "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _tidy(text: str) -> str:
    lines = [_MULTI_SPACE.sub(" ", line).strip() for line in clean_text(text).split("\n")]
    return clean_text("\n".join(lines))


def html_to_text(fragment: str) -> str:
    if "<" not in fragment and "&lt;" in fragment:
        fragment = html.unescape(fragment)  # some boards double-escape their markup
    parser = _TextExtractor()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must never fail the import
        return _tidy(re.sub(r"<[^>]*>", " ", fragment))
    return _tidy("".join(parser.parts))


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self.body_parts: list[str] = []
        self.region_parts: list[str] = []  # text inside <article>/<main>
        self._in_title = False
        self._jsonld_buf: list[str] | None = None
        self._skip = 0
        self._region = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key and "content" in values and key not in self.meta:
                self.meta[key] = values["content"]
        elif tag == "script" and "ld+json" in values.get("type", "").lower():
            self._jsonld_buf = []
        elif tag in _SKIP_TAGS and tag not in _VOID_TAGS:
            self._skip += 1
        elif tag in ("article", "main"):
            self._region += 1
        if tag == "li":
            if self.body_parts and not self.body_parts[-1].endswith("\n"):
                self._add("\n")
            self._add("- ")
        elif tag in _BLOCK_TAGS:
            self._add("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._jsonld_buf is not None:
            self.jsonld.append("".join(self._jsonld_buf))
            self._jsonld_buf = None
        elif tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in ("article", "main") and self._region:
            self._region -= 1
        if tag in _BLOCK_TAGS or tag == "li":
            self._add("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._jsonld_buf is not None:
            self._jsonld_buf.append(data)
        else:
            self._add(data)

    def _add(self, text: str) -> None:
        if self._skip:
            return
        self.body_parts.append(text)
        if self._region:
            self.region_parts.append(text)


def _as_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _organisation_name(value: object) -> str | None:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        return _as_text(value.get("name"))
    return _as_text(value)


def _is_job_posting(node: dict[str, object]) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.lower() == "jobposting" for k in kinds)


def _find_job_posting(blocks: list[str]) -> dict[str, object] | None:
    for raw in blocks:
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        stack: list[object] = [data]
        visited = 0
        while stack and visited < _MAX_JSON_NODES:  # iterative: deep nesting cannot recurse
            node = stack.pop()
            visited += 1
            if isinstance(node, dict):
                if _is_job_posting(node):
                    return node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return None


def _decode(body: bytes, charset: str | None) -> str:
    candidates = [charset] if charset else []
    meta = _CHARSET_META.search(body[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    for name in [*candidates, "utf-8"]:
        try:
            return body.decode(name, errors="replace")
        except (LookupError, TypeError):
            continue
    return body.decode("utf-8", errors="replace")


def _plain(value: str | None) -> str | None:
    """Single-line plain text: markup removed, entities decoded (names are never HTML)."""
    if not value:
        return None
    return " ".join(html_to_text(value).split()) or None


def _cap(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    cleaned = clean_text(value)
    cleaned = cleaned[:limit].rstrip() if len(cleaned) > limit else cleaned
    return cleaned or None


def extract_job(body: bytes, charset: str | None, max_description_chars: int) -> JobInfo:
    page = _PageParser()
    try:
        page.feed(_decode(body, charset))
        page.close()
    except Exception:  # noqa: BLE001, S110 - malformed markup must never fail the import
        pass  # keep whatever was parsed before the error

    posting = _find_job_posting(page.jsonld)
    if posting is not None:
        description = _as_text(posting.get("description"))
        info = JobInfo(
            company=_cap(
                _plain(_organisation_name(posting.get("hiringOrganization"))), MAX_NAME_CHARS
            ),
            title=_cap(
                _plain(_as_text(posting.get("title")) or _as_text(posting.get("name"))),
                MAX_NAME_CHARS,
            ),
            description=_cap(
                html_to_text(description) if description else None, max_description_chars
            ),
            structured=True,
        )
        if info.company or info.title or info.description:
            return info

    title = page.meta.get("og:title") or "".join(page.title_parts)
    company = page.meta.get("og:site_name") or page.meta.get("application-name")
    region = _tidy("".join(page.region_parts))
    summary = page.meta.get("og:description") or page.meta.get("description")
    description = region or (_tidy(summary) if summary else "") or _tidy("".join(page.body_parts))
    return JobInfo(
        company=_cap(_plain(company), MAX_NAME_CHARS),
        title=_cap(_plain(title), MAX_NAME_CHARS),
        description=_cap(description, max_description_chars),
        structured=False,
    )
