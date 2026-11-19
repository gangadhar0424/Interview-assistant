# ruff: noqa: E501
"""Real Electron startup and idle-resource benchmark (standard library only).

Launches the BUILT desktop app (`apps/desktop/out`, run `npm run build` first) several times with
INTERVIEW_PERF=1, a throw-away user-data directory (so no real settings or tokens are read) and an
automatic quit. The app prints one JSON report to stdout when it quits; this script parses it and
aggregates across runs.

    python scripts/bench/desktop_app_bench.py --runs 5 --exit-ms 12000 --out result.json

NOTE: each run opens the real assistant window for about `exit-ms`. The app is idle (no session,
no audio, no capture), so the resource numbers are the idle baseline, not a live-session figure.
CPU is `percentCPUUsage` from Electron: averaged since the previous sample (app ready, page load,
quit), so the last sample covers the idle window.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stats import dump, print_table, summarize  # noqa: E402

BEGIN = "INTERVIEW_PERF_REPORT_BEGIN"
END = "INTERVIEW_PERF_REPORT_END"


def run_once(desktop_dir: str, exit_ms: int) -> dict | None:
    electron = os.path.join(desktop_dir, "node_modules", "electron", "dist", "electron.exe")
    if not os.path.exists(electron):
        electron = os.path.join(desktop_dir, "node_modules", "electron", "dist", "electron")
    user_data = tempfile.mkdtemp(prefix="ia-bench-")
    env = {**os.environ, "INTERVIEW_PERF": "1", "INTERVIEW_PERF_EXIT_MS": str(exit_ms)}
    env.pop("ELECTRON_RENDERER_URL", None)  # production renderer, not a dev server
    env.pop(
        "ELECTRON_RUN_AS_NODE", None
    )  # set by VS Code terminals; it would run Electron as plain Node
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv built from this repo, no shell
            [electron, ".", f"--user-data-dir={user_data}"],
            cwd=desktop_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=exit_ms / 1000 + 45,
        )
    except subprocess.TimeoutExpired:
        return None
    finally:
        shutil.rmtree(user_data, ignore_errors=True)
    out = proc.stdout
    if BEGIN not in out or END not in out:
        return None
    return json.loads(out.split(BEGIN, 1)[1].split(END, 1)[0])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--exit-ms", type=int, default=12_000)
    parser.add_argument("--out")
    args = parser.parse_args()
    desktop_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "apps", "desktop")
    )

    reports = []
    for index in range(args.runs):
        report = run_once(desktop_dir, args.exit_ms)
        print(f"run {index + 1}/{args.runs}: {'ok' if report else 'NO REPORT'}", flush=True)
        if report:
            reports.append(report)
    if not reports:
        print(
            "UNMEASURED: no run produced a report (is the app built? `npm run build` in apps/desktop)."
        )
        raise SystemExit(2)

    startup: dict[str, list[float]] = {}
    memory: dict[str, list[float]] = {}
    cpu: dict[str, list[float]] = {}
    for report in reports:
        for name, row in report["summary"]["metrics"].items():
            if name.startswith("startup."):
                startup.setdefault(name, []).append(row["p50"])  # one sample per run
        for kind, row in report.get("resources", {}).items():
            memory.setdefault(kind, []).append(row["memory_mb"]["max"])  # peak per run
            cpu.setdefault(kind, []).append(row["cpu_percent"]["max"])

    results: dict[str, dict[str, float] | None] = {
        n: summarize(v) for n, v in sorted(startup.items())
    }
    for kind in sorted(memory):
        results[f"idle memory peak, {kind} process (MB)"] = summarize(memory[kind])
    for kind in sorted(cpu):
        results[f"CPU max sample incl. startup burst, {kind} process (%)"] = summarize(cpu[kind])

    print_table(f"Electron app, {len(reports)} runs (one sample per run)", results)
    first = reports[0]["environment"]
    print(f"\nEnvironment: {first}")
    dump(
        args.out,
        {"kind": "desktop_app", "runs": len(reports), "environment": first, "results": results},
    )


if __name__ == "__main__":
    main()
