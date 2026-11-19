# ruff: noqa: E501, RUF001, S101, T201, S603, PLR2004
"""Run a model-written solution against the hidden cases of one evaluation problem (M-8).

This is the only place model-written code is executed, and only for the synthetic problems in
tests/eval/coding_set.jsonl, in a throwaway temp directory, as a separate `python -I` process with a
wall-clock limit, no stdin and a minimal environment. It is NOT a security sandbox: it does not block
file or network access, so run it only on output you generated from these synthetic prompts. Python
and SQL (via an in-memory sqlite3 database) are supported; other languages are not executed.

Used by scripts/eval_coding.py and tests/test_coding_assistance.py.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from coding_structure import Block, detect_language, extract_blocks

TIMEOUT_SECONDS = 10
MARK = "@@CASE_RESULTS@@"

_HARNESS = """
import json as _json, sys as _sys
_CASES = {cases}
_out = []
for _args, _expected in _CASES:
    try:
        _got = {entry}(*_args)
        _out.append({{"ok": True, "got": _json.loads(_json.dumps(_got, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else list(o)))}})
    except BaseException as _e:
        _out.append({{"ok": False, "error": type(_e).__name__}})
_sys.stdout.write("\\n{mark}" + _json.dumps(_out) + "\\n")
"""


def python_blocks(text: str) -> list[Block]:
    return [
        b
        for b in extract_blocks(text)
        if b.declared == "python" or (not b.declared and detect_language(b.code) == "python")
    ]


def sql_blocks(text: str) -> list[Block]:
    return [
        b
        for b in extract_blocks(text)
        if b.declared == "sql" or (not b.declared and detect_language(b.code) == "sql")
    ]


def _normalise(value: Any, norm: str) -> Any:
    value = json.loads(json.dumps(value, default=list))
    if norm == "sorted":
        return sorted(value)
    if norm == "groups":
        return sorted(sorted(group) for group in value)
    return value


def run_python(answer: str, row: dict[str, Any]) -> dict[str, Any]:
    """{"status": ..., "passed": n, "total": n}. status: ok | no_code | no_entry | timeout |
    crash | wrong (some case failed)."""
    entry, cases = row["entry"], row["cases"]
    candidates = [b for b in python_blocks(answer) if f"def {entry}" in b.code]
    total = len(cases)
    if not candidates:
        return {
            "status": "no_entry" if python_blocks(answer) else "no_code",
            "passed": 0,
            "total": total,
        }
    source = candidates[0].code
    harness = _HARNESS.format(cases=repr([(a, e) for a, e in cases]), entry=entry, mark=MARK)
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "solution.py"
        script.write_text(source + "\n" + harness, encoding="utf-8")
        try:
            done = subprocess.run(  # noqa: S603 - synthetic problems only, see module docstring
                [sys.executable, "-I", str(script)],
                cwd=tmp,
                input="",
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                },
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "passed": 0, "total": total}
    marker = done.stdout.rfind(MARK)
    if marker < 0:
        return {"status": "crash", "passed": 0, "total": total}
    results = json.loads(done.stdout[marker + len(MARK) :].strip().splitlines()[0])
    norm = row.get("norm", "exact")
    passed = 0
    for result, (_, expected) in zip(results, cases, strict=True):
        if result["ok"] and _normalise(result["got"], norm) == _normalise(expected, norm):
            passed += 1
    return {"status": "ok" if passed == total else "wrong", "passed": passed, "total": total}


def run_sql(answer: str, row: dict[str, Any]) -> dict[str, Any]:
    blocks = sql_blocks(answer)
    if not blocks:
        return {"status": "no_code", "passed": 0, "total": 1}
    expected = json.loads(json.dumps(row["sql_expected"]))
    for block in blocks:
        query = block.code.strip().rstrip(";")
        db = sqlite3.connect(":memory:")
        try:
            db.executescript(row["sql_setup"])
            got = [list(r) for r in db.execute(query).fetchall()]
        except sqlite3.Error:
            continue
        finally:
            db.close()
        if got == expected:
            return {"status": "ok", "passed": 1, "total": 1}
    return {"status": "wrong", "passed": 0, "total": 1}


def run_reference(row: dict[str, Any]) -> dict[str, Any]:
    """Run the dataset's own reference solution (validates the set itself)."""
    fence = "```"
    if row["kind"] == "function":
        return run_python(f"{fence}python\n{row['reference']}{fence}\n", row)
    if row["kind"] == "sql":
        return run_sql(f"{fence}sql\n{row['reference']}\n{fence}\n", row)
    return {"status": "n/a", "passed": 0, "total": 0}
