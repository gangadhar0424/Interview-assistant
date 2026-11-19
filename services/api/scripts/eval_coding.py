# ruff: noqa: E501, RUF001, S101, T201, S603, PLR2004
"""Coding-assistance evaluation (M-8): generate answers with a real provider, then score them.

    cd services/api
    python scripts/eval_coding.py generate --arm before --provider groq --out runs/before.json
    python scripts/eval_coding.py generate --arm after  --provider groq --out runs/after.json
    python scripts/eval_coding.py score runs/before.json runs/after.json [--json]

Arms (same questions, same session preferences: English, coding, full_script, balanced, simple):
  before  prompt v4 and the word-derived token budget the code used before M-8
  after   prompt v5 and the coding token floor (the code as it is now)
`generate` calls the configured provider through the app's own adapter (keys come from the app
settings and are never printed); it is the only step that costs money. Only synthetic problems are
sent. Answers are written to --out and are never printed or logged. `score` makes no model call.

What is measured (all AUTOMATED; none is a human judgement):
  structure      scripts/coding_structure.py checks (sections, fenced code with language, complexity
                 stated, tests, no claim of having run the code, assumptions on incomplete input)
  functional     Python solutions run against hidden cases and SQL run on sqlite (scripts/coding_exec.py)
  complexity     the stated Big-O equals the reference (string match after normalising)
  tests proxy    a tests section with at least two cases and at least one boundary case
  completeness   balanced code fences, no placeholder code, answer not cut off
Human review of interpretation, readability and test-case quality is NOT included.
"""

import argparse
import asyncio
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coding_exec import run_python, run_sql  # noqa: E402
from coding_structure import (  # noqa: E402
    Spec,
    complexities,
    extract_blocks,
    fences_balanced,
    section_names,
    split_prose,
    structure_checks,
)

DATASET = Path(__file__).resolve().parents[1] / "tests" / "eval" / "coding_set.jsonl"
_BOUNDARY = re.compile(
    r"\[\s*\]|\"\"|''|\bempty\b|\bNone\b|\bnull\b|\bsingle\b|\bone element\b|\b0\b|-1", re.I
)


def load_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]


def spec_for(row: dict[str, Any]) -> Spec:
    return Spec(
        wants_code=row["wants_code"],
        wants_complexity=row["wants_complexity"],
        wants_tests=row["wants_tests"],
        wants_assumptions=row["wants_assumptions"],
        language=row["language"],
    )


def _norm_o(term: str) -> str:
    """Canonical form of a Big-O term: lower case, no spaces, any multiplication sign is `*`, and
    the factors of a product are sorted, so `O(m·n)`, `O(n*m)` and `O(m * n)` are the same."""
    text = re.sub(r"\s+", "", term.lower().replace("·", "*").replace("×", "*").replace("⋅", "*"))
    match = re.fullmatch(r"o\((.*)\)", text)
    if not match or "(" in match.group(1):
        return text
    return "o(" + "*".join(sorted(match.group(1).split("*"))) + ")"


def complexity_matches(answer: str, reference: str, alternatives: list[str] | None = None) -> bool:
    accepted = {_norm_o(reference), *(_norm_o(a) for a in alternatives or [])}
    return any(_norm_o(c) in accepted for c in complexities(answer))


def tests_proxy(answer: str) -> bool:
    """A tests section with at least two cases and one boundary case (a proxy, not a judgement)."""
    match = re.search(
        r"^#{1,6}\s*test(?:s| cases)\b.*?(?=^#{1,6}\s|\Z)", answer, re.M | re.S | re.I
    )
    if not match:
        return False
    body = match.group(0)
    cases = len(re.findall(r"(?:input|->|=>|==|expected|returns?|output)", body, re.I))
    return cases >= 2 and bool(_BOUNDARY.search(body))


def score_row(row: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    text = run.get("text") or ""
    out: dict[str, Any] = {
        "id": row["id"],
        "category": row["category"],
        "answered": bool(text.strip()),
    }
    if not out["answered"]:
        out["error"] = run.get("error", "empty")
        return out
    checks = structure_checks(text, spec_for(row))
    out["structure"] = checks
    out["structure_pass"] = sum(checks.values()) / len(checks)
    out["complete"] = checks["balanced_fences"] and checks.get("no_placeholder_code", True)
    if row["kind"] == "function":
        out["functional"] = run_python(text, row)
    elif row["kind"] == "sql":
        out["functional"] = run_sql(text, row)
    if row["wants_complexity"] and row["time"] != "n/a":
        out["complexity_correct"] = complexity_matches(text, row["time"], row.get("time_alt"))
    if row["wants_tests"]:
        out["tests_proxy"] = tests_proxy(text)
    if row.get("must_mention"):
        prose = split_prose(text).lower()
        out["mentions_ok"] = all(w in prose for w in row["must_mention"])
    out["words"] = len(split_prose(text).split())
    out["code_lines"] = sum(len(b.code.splitlines()) for b in extract_blocks(text))
    out["sections"] = section_names(text)
    out["finish_cut"] = (run.get("finish") == "length") or not fences_balanced(text)
    return out


def rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def summarise(
    rows: list[dict[str, Any]], runs: list[dict[str, Any]], token_cap: int | None = None
) -> dict[str, Any]:
    scored = [score_row(r, x) for r, x in zip(rows, runs, strict=True)]
    answered = [s for s in scored if s["answered"]]
    func = [s["functional"] for s in answered if "functional" in s]
    cases_passed = sum(f["passed"] for f in func)
    cases_total = sum(f["total"] for f in func)
    by_cat: dict[str, dict[str, Any]] = {}
    for s in scored:
        cat = by_cat.setdefault(s["category"], {"n": 0, "struct": [], "func_ok": [], "cut": []})
        cat["n"] += 1
        if s["answered"]:
            cat["struct"].append(s["structure_pass"])
            cat["cut"].append(s["finish_cut"])
            if "functional" in s:
                cat["func_ok"].append(s["functional"]["status"] == "ok")
    ttft = [x["ttft_ms"] for x in runs if x.get("ttft_ms") is not None]
    total = [x["total_ms"] for x in runs if x.get("total_ms") is not None]
    out_tok = [x["usage"]["output_tokens"] for x in runs if x.get("usage")]
    in_tok = [x["usage"]["input_tokens"] for x in runs if x.get("usage")]
    return {
        "problems": len(rows),
        "answered": len(answered),
        "structure_pass_mean": round(statistics.fmean(s["structure_pass"] for s in answered), 3)
        if answered
        else None,
        "complete_rate": rate([s["complete"] for s in answered]),
        "cut_off_rate": rate([s["finish_cut"] for s in answered]),
        # Output tokens (which include a reasoning model's hidden thinking) reached the request's
        # limit: the answer may have been cut short even when its code fences are balanced.
        "hit_token_limit_rate": rate(
            [x["usage"]["output_tokens"] >= token_cap for x in runs if x.get("usage") and token_cap]
        ),
        "functional_problems": len(func),
        "functional_all_cases_pass": rate([f["status"] == "ok" for f in func]),
        "functional_cases_passed": f"{cases_passed}/{cases_total}",
        "complexity_correct": rate(
            [s["complexity_correct"] for s in answered if "complexity_correct" in s]
        ),
        "states_complexity": rate(
            [
                s["structure"]["states_complexity"]
                for s in answered
                if "states_complexity" in s["structure"]
            ]
        ),
        "tests_proxy": rate([s["tests_proxy"] for s in answered if "tests_proxy" in s]),
        "code_block_has_language": rate(
            [
                s["structure"]["code_block_has_language"]
                for s in answered
                if "code_block_has_language" in s["structure"]
            ]
        ),
        "language_label_matches_code": rate(
            [
                s["structure"]["language_label_matches_code"]
                for s in answered
                if "language_label_matches_code" in s["structure"]
            ]
        ),
        "requested_language_used": rate(
            [
                s["structure"]["requested_language_used"]
                for s in answered
                if "requested_language_used" in s["structure"]
            ]
        ),
        "states_assumptions_on_incomplete": rate(
            [
                s["structure"]["states_assumptions"]
                for s in answered
                if "states_assumptions" in s["structure"]
            ]
        ),
        "no_execution_claim": rate([s["structure"]["no_execution_claim"] for s in answered]),
        "ttft_ms_p50": round(statistics.median(ttft)) if ttft else None,
        "total_ms_p50": round(statistics.median(total)) if total else None,
        "total_ms_p95": round(sorted(total)[max(0, math.ceil(0.95 * len(total)) - 1)])
        if total
        else None,
        "input_tokens_mean": round(statistics.fmean(in_tok)) if in_tok else None,
        "output_tokens_mean": round(statistics.fmean(out_tok)) if out_tok else None,
        "by_category": {
            c: {
                "n": v["n"],
                "structure": round(statistics.fmean(v["struct"]), 3) if v["struct"] else None,
                "functional_ok": rate(v["func_ok"]),
                "cut_off": rate(v["cut"]),
            }
            for c, v in sorted(by_cat.items())
        },
        "per_problem": scored,
    }


async def generate(
    arm: str, provider_id: str, only: list[str] | None, pace: float
) -> dict[str, Any]:
    from app.ai.errors import LLMError
    from app.ai.registry import build_registry
    from app.ai.types import GenerationConfig, Token, Usage
    from app.core.config import get_settings
    from app.preferences.compiler import PreferenceCompiler, PreferencesInput
    from app.preferences.prompt_builder import PromptBuilder, PromptInputs

    s = get_settings()

    def secret(value: Any) -> str | None:
        return value.get_secret_value() if value else None

    registry = build_registry(
        [provider_id],
        groq_api_key=secret(s.groq_api_key),
        groq_model=s.groq_model,
        gemini_api_key=secret(s.gemini_api_key),
        gemini_model=s.gemini_model,
        openai_api_key=secret(s.openai_api_key),
        openai_model=s.openai_model,
        anthropic_api_key=secret(s.anthropic_api_key),
        anthropic_model=s.anthropic_model,
    )
    entry = registry.get(provider_id)
    if entry is None:
        raise SystemExit("provider not available")
    model = entry.spec.models[0].id if hasattr(entry.spec, "models") else ""
    compiler = PreferenceCompiler.from_settings(s)
    base = {
        "language": "en",
        "answer_format": "full_script",
        "answer_length": "balanced",
        "answer_tone": "simple",
    }
    compiled = compiler.compile(PreferencesInput(question_type="coding", **base))
    # The budget the code used before M-8 is the non-coding budget (no floor).
    old_tokens = compiler.compile(
        PreferencesInput(question_type="behavioral", **base)
    ).generation.max_output_tokens
    version = "v4" if arm == "before" else "v5"
    tokens = old_tokens if arm == "before" else compiled.generation.max_output_tokens
    config = GenerationConfig(temperature=compiled.generation.temperature, max_output_tokens=tokens)
    builder = PromptBuilder(version)
    rows = [r for r in load_rows() if not only or r["id"] in only]
    runs: list[dict[str, Any]] = []
    for row in rows:
        built = builder.build(compiled.answer, PromptInputs(question=row["prompt"]))
        record: dict[str, Any] = {
            "id": row["id"],
            "text": "",
            "ttft_ms": None,
            "total_ms": None,
            "usage": None,
        }
        for attempt in range(3):
            parts: list[str] = []
            started = time.perf_counter()
            first: float | None = None
            usage = None
            try:
                async for event in entry.provider.stream_chat(built.messages, model, config):
                    if isinstance(event, Token):
                        if first is None:
                            first = time.perf_counter()
                        parts.append(event.text)
                    elif isinstance(event, Usage):
                        usage = {
                            "input_tokens": event.input_tokens,
                            "output_tokens": event.output_tokens,
                        }
                record.update(
                    text="".join(parts),
                    ttft_ms=round((first - started) * 1000) if first else None,
                    total_ms=round((time.perf_counter() - started) * 1000),
                    usage=usage,
                    error=None,
                )
                break
            except LLMError as exc:
                record["error"] = type(exc).__name__
                await asyncio.sleep(8 * (attempt + 1))
        runs.append(record)
        print(f"{row['id']}: {'ok' if record['text'] else record.get('error')}", flush=True)
        await asyncio.sleep(pace)
    meta = {
        "arm": arm,
        "prompt_version": version,
        "provider": provider_id,
        "model": model,
        "max_output_tokens": tokens,
    }
    return {"meta": meta, "runs": runs}


def cmd_score(paths: list[Path], as_json: bool) -> None:
    rows = load_rows()
    report = {}
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        runs_by_id = {r["id"]: r for r in data["runs"]}
        selected = [r for r in rows if r["id"] in runs_by_id]
        summary = summarise(
            selected,
            [runs_by_id[r["id"]] for r in selected],
            data["meta"].get("max_output_tokens"),
        )
        report[path.name] = {"meta": data["meta"], **summary}
    if as_json:
        print(json.dumps(report, indent=2))
        return
    for name, result in report.items():
        print(f"== {name}  {result['meta']}")
        for key, value in result.items():
            if key not in ("meta", "per_problem", "by_category"):
                print(f"  {key}: {value}")
        for cat, v in result["by_category"].items():
            print(f"  [{cat}] {v}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--arm", choices=["before", "after"], required=True)
    gen.add_argument("--provider", required=True)
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--only", nargs="*")
    gen.add_argument("--pace", type=float, default=2.0)
    sc = sub.add_parser("score")
    sc.add_argument("runs", nargs="+", type=Path)
    sc.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.cmd == "generate":
        result = asyncio.run(generate(args.arm, args.provider, args.only, args.pace))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {len(result['runs'])} runs to {args.out}")
    else:
        cmd_score(args.runs, args.json)


if __name__ == "__main__":
    main()
