# ruff: noqa: E501
"""Shared statistics for the benchmark scripts: nearest-rank percentiles, same as the desktop recorder."""

import json
import math


def percentile(sorted_values: list[float], p: float) -> float:
    rank = math.ceil(p / 100 * len(sorted_values))
    return sorted_values[min(len(sorted_values) - 1, max(0, rank - 1))]


def summarize(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(percentile(ordered, 50), 3),
        "p95": round(percentile(ordered, 95), 3),
        "max": round(ordered[-1], 3),
    }


def print_table(title: str, rows: dict[str, dict[str, float] | None]) -> None:
    print(f"\n{title}")
    print(f"{'metric':<56}{'n':>5}{'min':>10}{'p50':>10}{'p95':>10}{'max':>10}")
    for name, s in rows.items():
        if s is None:
            print(f"{name:<56}{'UNMEASURED':>45}")
        else:
            print(
                f"{name:<56}{int(s['count']):>5}{s['min']:>10.2f}{s['p50']:>10.2f}"
                f"{s['p95']:>10.2f}{s['max']:>10.2f}"
            )


def dump(path: str | None, payload: object) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
