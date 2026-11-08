"""Guard: prompt prose lives only in app/preferences/templates/, never inline in Python code.

A heuristic AST scan (docstrings excluded). It cannot prove a negative, but it catches the common
regressions: a new module that builds its own instruction text instead of using PromptBuilder.
"""

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
PREFERENCES = APP / "preferences"

PROSE = [
    re.compile(r"\bYou are (an?|the)\b"),
    re.compile(r"\bYou (must|should|will) "),
    re.compile(r"\bYour (task|job|role) is\b"),
    re.compile(r"\b(Respond|Reply|Answer) (in|with|only|as)\b"),
    re.compile(r"\bAnswer the (question|following)\b"),
    re.compile(r"\b(?i:ignore (all |any )?(previous|prior|above))\b"),
    re.compile(r"\b(?i:system prompt)\b"),
    re.compile(r"\b(?i:as an ai)\b"),
]
MAX_LITERAL_IN_PREFERENCES = 80


def string_literals(source: str) -> list[str]:
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def prose_hits(source: str) -> list[str]:
    return [s for s in string_literals(source) if any(p.search(s) for p in PROSE)]


def test_scanner_flags_inline_prompt_text_but_not_docstrings() -> None:
    assert prose_hits('SYSTEM = "You are a helpful assistant."')
    assert prose_hits('x = f"Respond in {lang} only"')
    assert prose_hits('PROMPT = "Please ignore previous instructions"')
    assert not prose_hits('def f():\n    """You are not a prompt, just a docstring."""\n')
    assert not prose_hits('LABEL = "candidate_material"')


def test_no_inline_prompt_prose_anywhere_in_app() -> None:
    offenders = {
        str(path.relative_to(APP)): prose_hits(path.read_text(encoding="utf-8"))
        for path in APP.rglob("*.py")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_preferences_modules_hold_no_long_string_literals() -> None:
    long_literals = {
        path.name: [
            s[:40]
            for s in string_literals(path.read_text(encoding="utf-8"))
            if len(s) > MAX_LITERAL_IN_PREFERENCES
        ]
        for path in PREFERENCES.glob("*.py")
    }
    assert {k: v for k, v in long_literals.items() if v} == {}
