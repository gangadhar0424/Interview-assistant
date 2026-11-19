# ruff: noqa: E501, RUF001, S101, T201, S603, PLR2004
"""Structural checks of a coding answer (M-8). Pure, deterministic, no model, no code execution.

These are AUTOMATED STRUCTURE checks: does the answer have the sections, fenced code blocks with a
language, a stated complexity and tests, and does it avoid claiming that code was run. They say
nothing about whether the solution is CORRECT; correctness is measured separately by running the
Python solutions against hidden cases (scripts/eval_coding.py) and by human review (not done here).

Used by scripts/eval_coding.py and tests/test_coding_assistance.py.
"""

import re
from dataclasses import dataclass

SECTION_ORDER = ("problem", "assumptions", "approach", "code", "complexity", "tests", "edge cases")

_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "node": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "c#": "csharp",
    "cs": "csharp",
    "golang": "go",
    "rs": "rust",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "postgresql": "sql",
    "mysql": "sql",
    "postgres": "sql",
    "plaintext": "text",
    "txt": "text",
}

# What a question may ask for, in plain words, to the canonical id.
_REQUESTED = (
    ("python", re.compile(r"\bpython\b", re.I)),
    ("java", re.compile(r"\bjava\b(?!\s*script)", re.I)),
    ("javascript", re.compile(r"\bjavascript\b|\bnode\.?js\b", re.I)),
    ("typescript", re.compile(r"\btypescript\b", re.I)),
    ("cpp", re.compile(r"c\+\+", re.I)),
    ("go", re.compile(r"\bgolang\b|\bin go\b", re.I)),
    ("sql", re.compile(r"\bsql\b", re.I)),
    ("rust", re.compile(r"\brust\b", re.I)),
    ("csharp", re.compile(r"c#|\bc sharp\b", re.I)),
)

_FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
_OPEN_FENCE = re.compile(r"^```", re.MULTILINE)
_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+|\*\*)([A-Za-z][A-Za-z ]{1,30}?)\**:?\**\s*$", re.MULTILINE
)
_BIG_O = re.compile(r"O\s*\(\s*[^()\n]{1,40}(?:\([^()\n]*\))?[^()\n]{0,20}\)")
_EXEC_CLAIM = re.compile(
    r"\bI (?:ran|run|executed|tested|compiled|verified by running)\b"
    r"|\b(?:when|after) (?:I|we) (?:ran|run|executed|tested) (?:it|this|the code)\b"
    r"|\b(?:tested|executed|ran) (?:successfully|and (?:passed|works))\b"
    r"|\ball (?:the )?tests? pass(?:ed)?\b"
    r"|\bthis (?:code )?(?:was|has been) (?:run|tested|executed)\b",
    re.I,
)
_PLACEHOLDER = re.compile(
    r"^\s*(?:\.\.\.|…)\s*$|^\s*(?:pass\s*)?(?:#|//)\s*(?:todo|implement|your code here)\b",
    re.I | re.M,
)


def canonical_language(label: str) -> str:
    key = label.strip().lower().split()[0] if label.strip() else ""
    key = key.lstrip("{.").rstrip("}")
    return _ALIASES.get(key, key)


def requested_language(question: str) -> str | None:
    """The language the question names, or None. The first one named wins."""
    hits = [(m.start(), name) for name, rx in _REQUESTED if (m := rx.search(question))]
    return min(hits)[1] if hits else None


@dataclass(frozen=True, slots=True)
class Block:
    declared: str  # canonical declared language, "" when the fence names none
    code: str


def extract_blocks(text: str) -> list[Block]:
    return [Block(canonical_language(m.group(1)), m.group(2)) for m in _FENCE.finditer(text)]


def fences_balanced(text: str) -> bool:
    """True when every opening fence has a closing one (an odd count means a cut-off block)."""
    return len(_OPEN_FENCE.findall(text)) % 2 == 0


def detect_language(code: str) -> str:
    """A best-effort guess from the code's own syntax. Returns "" when unsure."""
    scores = {
        "python": 0,
        "javascript": 0,
        "typescript": 0,
        "java": 0,
        "cpp": 0,
        "go": 0,
        "sql": 0,
        "rust": 0,
        "bash": 0,
        "csharp": 0,
    }
    checks: list[tuple[str, str, int]] = [
        ("python", r"^\s*def \w+\(.*\)\s*(?:->\s*[\w\[\], .|]+)?:\s*$", 3),
        ("python", r"^\s*(?:from \w[\w.]* import |import \w+\s*$)", 2),
        ("python", r"^\s*(?:elif |if .+:\s*$|for .+ in .+:\s*$|while .+:\s*$)", 1),
        ("python", r"\bself\b|\bNone\b|\bTrue\b|\bFalse\b|\blen\(", 1),
        ("javascript", r"\bconst \w+ =|\blet \w+ =|=>|\bfunction \w*\(|console\.log", 2),
        ("typescript", r": (?:number|string|boolean)\b|\binterface \w+|<[A-Z]\w*>\(", 3),
        ("java", r"\bpublic (?:static )?(?:class|int|void|boolean|String)\b|System\.out", 3),
        ("java", r"\bnew \w+<.*>\(\)|\bList<|\bMap<|\bString\[\]", 1),
        ("cpp", r"#include\s*<|std::|\bcout\b|\bvector<|\bint main\(", 3),
        ("go", r"^\s*func \w*\(|^package \w+|\bfmt\.|:= ", 3),
        ("rust", r"\bfn \w+\(|\blet mut\b|->\s*\w+\s*\{|println!", 3),
        ("sql", r"\b(?:SELECT|INSERT INTO|UPDATE|DELETE FROM|CREATE TABLE)\b", 3),
        ("sql", r"\b(?:FROM|WHERE|GROUP BY|ORDER BY|JOIN|HAVING)\b", 1),
        ("bash", r"^#!/bin/(?:ba)?sh|^\s*(?:echo|grep|awk|sed|cd) \S", 2),
        ("csharp", r"\busing System|\bConsole\.Write|\bnamespace \w+|\bstring\[\]", 3),
    ]
    for lang, pattern, weight in checks:
        flags = re.M | (re.I if lang == "sql" else 0)
        scores[lang] += min(len(re.findall(pattern, code, flags)), 3) * weight
    if scores["typescript"] and scores["javascript"]:
        scores["typescript"] += scores["javascript"]
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 3 else ""


def section_names(text: str) -> list[str]:
    """Normalised heading names found in the answer (`### Approach` or a bold `**Approach**` line)."""
    names = []
    for m in _HEADING.finditer(text):
        name = m.group(1).strip().lower()
        names.append("edge cases" if name in ("edge case", "edge cases") else name)
    return names


def has_complexity(text: str) -> bool:
    return bool(_BIG_O.search(text))


def complexities(text: str) -> list[str]:
    """Big-O terms, normalised (lower case, no spaces), in order of appearance."""
    return [re.sub(r"\s+", "", m.group(0)).lower() for m in _BIG_O.finditer(text)]


def claims_execution(text: str) -> bool:
    """True when the prose says the code was run or tested. Code blocks are ignored."""
    return bool(_EXEC_CLAIM.search(_FENCE.sub("", text)))


def has_placeholder_code(blocks: list[Block]) -> bool:
    return any(_PLACEHOLDER.search(b.code) for b in blocks)


@dataclass(frozen=True, slots=True)
class Spec:
    """What a case expects (from the dataset row)."""

    wants_code: bool = True
    wants_complexity: bool = True
    wants_tests: bool = True
    wants_assumptions: bool = False  # the question is incomplete or ambiguous
    language: str | None = None  # the language the question requires


def structure_checks(answer: str, spec: Spec) -> dict[str, bool]:
    """Pass/fail per structural check. Checks that do not apply to the case are left out."""
    blocks = extract_blocks(answer)
    code_blocks = [b for b in blocks if b.declared not in ("text", "")] or blocks
    names = section_names(answer)
    out: dict[str, bool] = {"balanced_fences": fences_balanced(answer)}
    out["no_execution_claim"] = not claims_execution(answer)
    if spec.wants_code:
        out["has_code_block"] = bool(blocks)
        # A block that is recognisably code must name its language; a plain block of sample
        # input/output (nothing detectable) may be unlabelled.
        out["code_block_has_language"] = bool(blocks) and all(
            b.declared or not detect_language(b.code) for b in blocks
        )
        out["no_placeholder_code"] = bool(blocks) and not has_placeholder_code(blocks)
        if blocks:
            detected_ok = True
            for b in blocks:
                guess = detect_language(b.code)
                if b.declared and guess and b.declared != guess and b.declared not in ("text",):
                    detected_ok = False
            out["language_label_matches_code"] = detected_ok
        if spec.language:
            out["requested_language_used"] = any(b.declared == spec.language for b in code_blocks)
    else:
        out["no_code_dump"] = not any(len(b.code.splitlines()) > 25 for b in blocks)
    if spec.wants_complexity:
        out["states_complexity"] = has_complexity(answer)
    if spec.wants_tests:
        out["has_tests"] = "tests" in names or "test cases" in names or len(blocks) >= 2
    if spec.wants_assumptions:
        out["states_assumptions"] = "assumptions" in names or bool(
            re.search(r"\bassum(?:e|ing|ption)", answer, re.I)
        )
    order = [SECTION_ORDER.index(n) for n in names if n in SECTION_ORDER]
    out["sections_in_order"] = order == sorted(order)
    return out


def split_prose(text: str) -> str:
    """The answer without its fenced code blocks."""
    return _FENCE.sub("", text)
