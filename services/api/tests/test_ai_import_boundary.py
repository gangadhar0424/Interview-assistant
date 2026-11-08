"""No provider SDK imports outside app/ai/adapters/ (Phase 6 acceptance; also ruff TID251)."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
ADAPTERS = APP / "ai" / "adapters"
BANNED = {
    "openai",
    "anthropic",
    "groq",
    "litellm",
    "sarvamai",
    "google.generativeai",
    "google.genai",
}


def banned_imports(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for name in names:
            if any(name == b or name.startswith(b + ".") for b in BANNED):
                found.append(name)
    return found


def test_scanner_detects_banned_imports() -> None:
    assert banned_imports("import openai") == ["openai"]
    assert banned_imports("from anthropic import Anthropic") == ["anthropic"]
    assert banned_imports("import google.generativeai as g") == ["google.generativeai"]
    assert banned_imports("import json\nfrom app.ai import router") == []


def test_no_provider_sdk_is_imported_outside_adapters() -> None:
    offenders = [
        f"{path.relative_to(APP)}: {name}"
        for path in APP.rglob("*.py")
        if ADAPTERS not in path.parents
        for name in banned_imports(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
