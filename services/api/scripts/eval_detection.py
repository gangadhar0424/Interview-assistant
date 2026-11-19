# ruff: noqa: E501, T201
"""Evaluate the question detector on tests/eval/detection_set.jsonl (M-7).

    cd services/api
    python scripts/eval_detection.py                       # all examples, metrics only
    python scripts/eval_detection.py --split dev --failures   # misses on the TUNING set
    python scripts/eval_detection.py --split heldout --failures --detector-frozen
    python scripts/eval_detection.py --latency --json out.json

Reports precision, recall, F1, accuracy, false-positive rate, false-negative rate and the confusion
matrix overall and broken down by language, category, split and positive/negative. The held-out
examples may only be listed as failures with --detector-frozen, so the detector cannot be tuned on them
by accident. The data is synthetic (see scripts/build_detection_set.py). Nothing here calls a model or
the network.
"""

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.qa.detection import classify as current_classify  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parents[1] / "tests" / "eval"
SETS = {
    "main": EVAL_DIR / "detection_set.jsonl",
    "challenge": EVAL_DIR / "detection_challenge.jsonl",
}


def load(which: str = "main") -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in SETS[which].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(1 for r in rows if r["label"] and r["pred"])
    fp = sum(1 for r in rows if not r["label"] and r["pred"])
    tn = sum(1 for r in rows if not r["label"] and not r["pred"])
    fn = sum(1 for r in rows if r["label"] and not r["pred"])

    def ratio(a: float, b: float) -> float | None:
        return round(a / b, 3) if b else None

    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = (
        round(2 * precision * recall / (precision + recall), 3)
        if precision and recall
        else (0.0 if tp + fp + fn else None)
    )
    return {
        "n": len(rows),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": ratio(tp + tn, len(rows)),
        "fpr": ratio(fp, fp + tn),
        "fnr": ratio(fn, fn + tp),
    }


def group(
    rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], str]
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        buckets[key(r)].append(r)
    return {k: metrics(v) for k, v in sorted(buckets.items())}


def fmt(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:5.2f}"


def table(title: str, data: dict[str, dict[str, Any]]) -> None:
    print(f"\n{title}")
    print(
        f"{'':16}{'n':>4}{'TP':>4}{'FP':>4}{'TN':>4}{'FN':>4}{'prec':>7}{'rec':>7}{'F1':>7}{'acc':>7}{'FPR':>7}{'FNR':>7}"
    )
    for name, m in data.items():
        print(
            f"{name:16}{m['n']:>4}{m['tp']:>4}{m['fp']:>4}{m['tn']:>4}{m['fn']:>4}"
            f"{fmt(m['precision']):>7}{fmt(m['recall']):>7}{fmt(m['f1']):>7}{fmt(m['accuracy']):>7}{fmt(m['fpr']):>7}{fmt(m['fnr']):>7}"
        )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--set",
        choices=["main", "challenge"],
        default="main",
        help="challenge = the harder set written after the freeze",
    )
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="all")
    parser.add_argument("--failures", action="store_true", help="list misclassified examples")
    parser.add_argument(
        "--detector-frozen", action="store_true", help="allow listing held-out failures"
    )
    parser.add_argument(
        "--detector-file",
        help="score another detector module (a file defining classify), e.g. the baseline",
    )
    parser.add_argument("--latency", action="store_true")
    parser.add_argument("--json")
    args = parser.parse_args()
    if args.failures and args.set == "main" and args.split != "dev" and not args.detector_frozen:
        raise SystemExit(
            "Listing held-out failures needs --detector-frozen (do not tune on the held-out set)."
        )

    classify = current_classify
    if args.detector_file:
        import importlib.util

        spec = importlib.util.spec_from_file_location("alt_detector", args.detector_file)
        if not spec or not spec.loader:
            raise SystemExit("cannot load the detector file")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        classify = module.classify
    rows = load(args.set)
    if args.set == "main" and args.split != "all":
        rows = [r for r in rows if r["split"] == args.split]
    for r in rows:
        d = classify(r["text"])
        r["pred"], r["conf"], r["reason"] = d.is_question, d.confidence, d.reason

    result = {
        "split": args.split,
        "overall": metrics(rows),
        "by_language": group(rows, lambda r: r["lang"]),
        "by_split": group(rows, lambda r: r["split"]),
        "by_label": group(rows, lambda r: "positive" if r["label"] else "negative"),
        "by_category": group(rows, lambda r: r["category"]),
        "by_language_category": group(rows, lambda r: f"{r['lang']}/{r['category']}"),
    }
    print(f"split={args.split} examples={len(rows)}")
    table("OVERALL", {"all": result["overall"]})
    table("BY LANGUAGE", result["by_language"])
    table("BY SPLIT", result["by_split"])
    table("BY CATEGORY (precision is n/a for single-label categories)", result["by_category"])
    if args.failures:
        print("\nFAILURES")
        for r in rows:
            if r["pred"] != r["label"]:
                kind = "FALSE POSITIVE" if r["pred"] else "FALSE NEGATIVE"
                print(
                    f"  {kind:15} {r['id']:22} reason={r['reason']:20} conf={r['conf']:.2f}  {r['text']}"
                )
    if args.latency:
        texts = [r["text"] for r in rows]
        samples = []
        for _ in range(30):
            for text in texts:
                t = time.perf_counter_ns()
                classify(text)
                samples.append((time.perf_counter_ns() - t) / 1000)
        samples.sort()
        latency = {
            "calls": len(samples),
            "mean_us": round(statistics.fmean(samples), 2),
            "p50_us": round(samples[len(samples) // 2], 2),
            "p95_us": round(samples[int(len(samples) * 0.95)], 2),
            "max_us": round(samples[-1], 2),
        }
        result["latency_us"] = latency
        print(f"\nLATENCY per call (microseconds): {latency}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
