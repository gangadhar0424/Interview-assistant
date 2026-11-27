"""Phase F: backend performance and reliability probe (local, throwaway, synthetic).

Starts its OWN API (and worker) on 127.0.0.1:8766 against a throwaway database `ia_perf` and Redis
database 1 inside this project's own Docker containers, runs the scenarios below with synthetic
accounts and synthetic files, writes one JSON result file, and tears everything down. It stops and
restarts this project's postgres and redis containers during the outage scenarios, so run it only
on a developer machine. It never calls a real AI provider (providers are forced to `fake`) and
prints no credential.

Usage (from services/api, with the project's venv):
    python ../../scripts/perf/backend_reliability.py --out result.json [--only a,b] [--sustain 90]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "services" / "api"
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
V1 = f"{BASE}/api/v1"
PG = "interview-assistant-postgres-1"
REDIS = "interview-assistant-redis-1"
DB = "ia_perf"
PW = "Perf-Passw0rd-" + uuid.uuid4().hex[:8]


# ----------------------------------------------------------------------------- helpers
def env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(p / 100 * len(s) + 0.5) - 1))]


def summary(ms: list[float]) -> dict[str, float]:
    if not ms:
        return {"n": 0}
    return {
        "n": len(ms),
        "min": round(min(ms), 1),
        "p50": round(pct(ms, 50), 1),
        "p95": round(pct(ms, 95), 1),
        "p99": round(pct(ms, 99), 1),
        "max": round(max(ms), 1),
    }


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def psql(sql: str, db: str = "postgres") -> None:
    user = env_file()["POSTGRES_USER"]
    docker("exec", PG, "psql", "-U", user, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql)


def proc_stats(pid: int) -> dict[str, float] | None:
    """Working set (MB) and cumulative CPU seconds of a process tree root, via PowerShell."""
    # The venv `python.exe` is a small launcher; the real server is its child. Sum the whole tree.
    cmd = (
        f"$ids=@({pid}); $i=0; while($i -lt $ids.Count){{ "
        "$ids += @(Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $ids[$i]) "
        "-ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessId }); $i++ }; "
        "$ps=@($ids | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }); "
        "if($ps.Count){ '{0} {1}' -f ($ps | Measure-Object WorkingSet64 -Sum).Sum,"
        "($ps | ForEach-Object { $_.TotalProcessorTime.TotalSeconds } | Measure-Object -Sum).Sum }"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    parts = r.stdout.split()
    if len(parts) != 2:
        return None
    return {"rss_mb": round(int(parts[0]) / 1048576, 1), "cpu_s": round(float(parts[1].replace(",", ".")), 2)}


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


def pdf(extra: int = 0, salt: str = "") -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + salt.encode() + b"x" * extra + b"\ntrailer\n%%EOF\n"


class Stack:
    def __init__(self) -> None:
        self.api: subprocess.Popen[bytes] | None = None
        self.worker: subprocess.Popen[bytes] | None = None
        self.tmp = Path(tempfile.mkdtemp(prefix="ia-perf-"))
        e = env_file()
        self.db_url = (
            f"postgresql+asyncpg://{e['POSTGRES_USER']}:{e['POSTGRES_PASSWORD']}@localhost:5432/{DB}"
        )
        self.env = {
            **os.environ,
            "DATABASE_URL": self.db_url,
            "REDIS_URL": "redis://localhost:6379/1",
            "APP_ENV": "local",
            "JWT_SECRET": secrets.token_urlsafe(48),
            "FILE_STORAGE_ROOT": str(self.tmp / "files"),
            "LLM_ENABLED_PROVIDERS": "fake",
            "STT_ENABLED_PROVIDERS": "fake",
            "EMBEDDING_PROVIDER": "fake",  # never download or run the real model in a probe
            "USER_MAX_DOCUMENTS": "10000",
            "USER_MAX_STORAGE_MB": "100000",
            "DOCUMENT_MAX_UPLOAD_MB": "20",
        }

    def py(self) -> str:
        return str(API_DIR / ".venv" / "Scripts" / "python.exe")

    def setup_db(self) -> None:
        psql(f"DROP DATABASE IF EXISTS {DB}")
        psql(f"CREATE DATABASE {DB}")
        r = subprocess.run(
            [self.py(), "-m", "alembic", "upgrade", "head"],
            cwd=API_DIR,
            env={**self.env, "DATABASE_URL": self.db_url},
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise SystemExit("migration failed: " + r.stderr[-400:])

    def start_api(self) -> None:
        self.api = subprocess.Popen(
            [self.py(), "-m", "uvicorn", "app.main:get_app", "--factory", "--port", str(PORT),
             "--no-proxy-headers", "--log-level", "warning"],
            cwd=API_DIR, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.5)
        raise SystemExit("API did not start")

    def start_worker(self) -> None:
        self.worker = subprocess.Popen(
            [self.py(), "-m", "dramatiq", "app.workers.tasks", "--processes", "1", "--threads", "2"],
            cwd=API_DIR, env=self.env,
            stdout=open(self.tmp / "worker.log", "ab"), stderr=subprocess.STDOUT,
        )

    def stop_all(self) -> None:
        for p in (self.worker, self.api):
            if p is not None:
                kill_tree(p)
        try:
            psql(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        except subprocess.CalledProcessError:
            pass


def register(c: httpx.Client, tag: str) -> dict[str, str]:
    email = f"{tag}-{uuid.uuid4().hex[:8]}@example.com"
    r = c.post(f"{V1}/auth/signup", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    r = c.post(f"{V1}/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ----------------------------------------------------------------------------- scenarios
async def load_levels(headers_list: list[dict[str, str]], levels: list[int], seconds: float) -> dict:
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(
        timeout=30, limits=httpx.Limits(max_connections=400, max_keepalive_connections=400)
    ) as client:
        for conc in levels:
            lat: list[float] = []
            codes: dict[str, int] = {}
            stop = time.perf_counter() + seconds

            async def worker(i: int) -> None:
                h = headers_list[i % len(headers_list)]
                while time.perf_counter() < stop:
                    t = time.perf_counter()
                    try:
                        r = await client.get(f"{V1}/auth/me", headers=h)
                        code = str(r.status_code)
                    except httpx.HTTPError as exc:
                        code = type(exc).__name__
                    lat.append((time.perf_counter() - t) * 1000)
                    codes[code] = codes.get(code, 0) + 1

            t0 = time.perf_counter()
            await asyncio.gather(*(worker(i) for i in range(conc)))
            wall = time.perf_counter() - t0
            out[str(conc)] = {
                "requests": len(lat),
                "rps": round(len(lat) / wall, 1),
                "latency_ms": summary(lat),
                "status": codes,
            }
    return out


def scenario_a_concurrency(stack: Stack) -> dict:
    with httpx.Client(timeout=20) as c:
        hs = [register(c, f"load{i}") for i in range(5)]
    return asyncio.run(load_levels(hs, [1, 10, 50, 100, 200], 8))


def scenario_b_large_payloads(stack: Stack) -> dict:
    out: dict = {}
    with httpx.Client(timeout=120) as c:
        h = register(c, "big")
        for mb in (1, 5, 10, 19):
            body = pdf(mb * 1024 * 1024 - 100, uuid.uuid4().hex)
            t = time.perf_counter()
            r = c.post(f"{V1}/documents", headers=h, files={"file": ("big.pdf", body, "application/pdf")})
            out[f"upload_{mb}MB"] = {"status": r.status_code, "ms": round((time.perf_counter() - t) * 1000, 1)}
        def refused(label: str, send) -> None:  # type: ignore[no-untyped-def]
            """Over-limit bodies are refused early and the connection may be closed (API.md), so a
            client still sending can see a reset instead of the 413. Both count as a refusal."""
            t = time.perf_counter()
            try:
                out[label] = {"status": send().status_code}
            except httpx.HTTPError as exc:
                out[label] = {"status": f"connection closed by server ({type(exc).__name__})"}
            out[label]["ms"] = round((time.perf_counter() - t) * 1000, 1)

        over = pdf(21 * 1024 * 1024)
        refused("upload_21MB_over_limit", lambda: c.post(
            f"{V1}/documents", headers=h, files={"file": ("over.pdf", over, "application/pdf")}))
        refused("json_3MiB_over_2MiB_cap", lambda: c.post(
            f"{V1}/sessions", headers=h, json={"x": "a" * (3 * 1024 * 1024)}))
        # concurrent large valid uploads from distinct users
        hs = [register(c, f"bigc{i}") for i in range(4)]

    async def go() -> list[dict]:
        async with httpx.AsyncClient(timeout=180) as ac:
            async def one(hh: dict[str, str], i: int) -> dict:
                t = time.perf_counter()
                r = await ac.post(
                    f"{V1}/documents", headers=hh,
                    files={"file": (f"c{i}.pdf", pdf(8 * 1024 * 1024, uuid.uuid4().hex), "application/pdf")},
                )
                return {"status": r.status_code, "ms": round((time.perf_counter() - t) * 1000, 1)}
            return await asyncio.gather(*(one(hh, i) for i, hh in enumerate(hs)))

    out["concurrent_4x8MB_distinct_users"] = asyncio.run(go())
    return out


def _hold_headers_only(n: int) -> list[socket.socket]:
    out = []
    for _ in range(n):
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        s.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Slow: 1\r\n")  # never terminated
        out.append(s)
    return out


def _hold_stalled_uploads(n: int, headers: dict[str, str]) -> list[socket.socket]:
    out = []
    for _ in range(n):
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        s.sendall(
            b"POST /api/v1/documents HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + f"Authorization: {headers['Authorization']}\r\n".encode()
            + b"Content-Type: multipart/form-data; boundary=zz\r\nContent-Length: 900000\r\n\r\n--zz\r\n"
        )  # promises 900 KB, sends a few bytes, then stalls
        out.append(s)
    return out


def _probe_normal(h: dict[str, str], n: int = 6) -> dict:
    """Authenticated, database-bound request while slow clients are held (15 s client timeout)."""
    lat: list[float] = []
    codes: dict[str, int] = {}
    with httpx.Client(timeout=15) as c:
        for _ in range(n):
            t = time.perf_counter()
            try:
                code = str(c.get(f"{V1}/auth/me", headers=h).status_code)
            except httpx.HTTPError as exc:
                code = type(exc).__name__
            lat.append((time.perf_counter() - t) * 1000)
            codes[code] = codes.get(code, 0) + 1
            time.sleep(0.2)
    return {"latency_ms": summary(lat), "status": codes}


def scenario_c_slow_clients(stack: Stack) -> dict:
    out: dict = {}
    with httpx.Client(timeout=20) as c:
        h = register(c, "slow")
        h_other = register(c, "slowother")
    out["baseline_no_slow_clients"] = _probe_normal(h)
    held = _hold_headers_only(60)  # request headers never finished; no auth, no database
    time.sleep(1)
    out["60_connections_with_unfinished_headers"] = _probe_normal(h)
    for s in held:
        s.close()
    time.sleep(1)
    for n in (10, 25, 60):  # authenticated uploads that promise a body and then stall
        held = _hold_stalled_uploads(n, h)
        time.sleep(1.5)
        out[f"{n}_stalled_uploads_by_one_user__same_user_request"] = _probe_normal(h, 4)
        out[f"{n}_stalled_uploads_by_one_user__other_user_request"] = _probe_normal(h_other, 4)
        out[f"{n}_stalled_uploads_by_one_user__health"] = {
            "status": httpx.get(f"{BASE}/health", timeout=15).status_code
        }
        for s in held:
            s.close()
        time.sleep(3)  # let the server notice the disconnects
        out[f"recovery_after_closing_{n}"] = _probe_normal(h, 3)
    return out


def scenario_d_pool(stack: Stack) -> dict:
    """Far more concurrent DB-bound requests than pool_size + max_overflow (10 + 10)."""
    with httpx.Client(timeout=20) as c:
        hs = [register(c, f"pool{i}") for i in range(5)]
    return asyncio.run(load_levels(hs, [300], 10))


def _poll(paths: list[tuple[str, dict[str, str] | None]], seconds: float, interval: float = 0.5) -> list[dict]:
    rows: list[dict] = []
    end = time.time() + seconds
    with httpx.Client(timeout=15) as c:
        while time.time() < end:
            for path, h in paths:
                t = time.perf_counter()
                try:
                    r = c.get(BASE + path, headers=h or {})
                    code = r.status_code
                    ra = r.headers.get("retry-after")
                except httpx.HTTPError as exc:
                    code, ra = type(exc).__name__, None
                rows.append({"path": path, "status": code, "ms": round((time.perf_counter() - t) * 1000, 1),
                             "retry_after": ra, "t": round(time.time() - (end - seconds), 1)})
            time.sleep(interval)
    return rows


def _recovery_seconds(headers: dict[str, str], limit: float = 90) -> float | None:
    t0 = time.time()
    with httpx.Client(timeout=10) as c:
        while time.time() - t0 < limit:
            try:
                if c.get(f"{BASE}/ready").status_code == 200 and c.get(
                    f"{V1}/auth/me", headers=headers
                ).status_code == 200:
                    return round(time.time() - t0, 1)
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    return None


def _wait_healthy(name: str, limit: float = 90) -> None:
    t0 = time.time()
    while time.time() - t0 < limit:
        r = docker("inspect", "-f", "{{.State.Health.Status}}", name, check=False)
        if r.stdout.strip() == "healthy":
            return
        time.sleep(1)


def _summ(rows: list[dict]) -> dict:
    by: dict[str, dict] = {}
    for p in sorted({r["path"] for r in rows}):
        rs = [r for r in rows if r["path"] == p]
        codes: dict[str, int] = {}
        for r in rs:
            codes[str(r["status"])] = codes.get(str(r["status"]), 0) + 1
        by[p] = {"status": codes, "max_ms": max(r["ms"] for r in rs),
                 "retry_after_seen": sorted({r["retry_after"] for r in rs if r["retry_after"]})}
    return by


def scenario_e_redis_outage(stack: Stack) -> dict:
    with httpx.Client(timeout=20) as c:
        h = register(c, "redis")
    paths = [("/health", None), ("/ready", None), ("/api/v1/auth/me", h), ("/api/v1/resumes", h)]
    docker("stop", "-t", "2", REDIS)
    try:
        during = _poll(paths, 12)
        with httpx.Client(timeout=20) as c:
            t = time.perf_counter()
            login = c.post(f"{V1}/auth/login", json={"email": "nobody@example.com", "password": "x" * 12})
            login_ms = round((time.perf_counter() - t) * 1000, 1)
    finally:
        docker("start", REDIS)
        _wait_healthy(REDIS)
    rec = _recovery_seconds(h)
    return {
        "during_outage": _summ(during),
        "login_during_outage": {"status": login.status_code, "ms": login_ms},
        "seconds_until_ready_and_authenticated_ok_after_redis_start": rec,
    }


def scenario_f_db_outage(stack: Stack) -> dict:
    with httpx.Client(timeout=20) as c:
        h = register(c, "db")
    paths = [("/health", None), ("/ready", None), ("/api/v1/auth/me", h), ("/api/v1/resumes", h)]
    def stored() -> int:
        root = stack.tmp / "files" / "uploads"
        return sum(1 for f in root.rglob("*") if f.is_file()) if root.exists() else 0

    def temps() -> int:
        root = stack.tmp / "files" / "tmp"
        return sum(1 for f in root.iterdir()) if root.exists() else 0

    files_before, temps_before = stored(), temps()
    docker("stop", "-t", "2", PG)
    try:
        during = _poll(paths, 14)
        with httpx.Client(timeout=30) as c:
            t = time.perf_counter()
            up = c.post(f"{V1}/documents", headers=h, files={"file": ("a.pdf", pdf(100), "application/pdf")})
            up_ms = round((time.perf_counter() - t) * 1000, 1)
    finally:
        docker("start", PG)
        _wait_healthy(PG)
    rec = _recovery_seconds(h)
    return {
        "during_outage": _summ(during),
        "upload_during_outage": {
            "status": up.status_code,
            "ms": up_ms,
            "stored_files_added_by_failed_upload": stored() - files_before,
            "temp_files_left_by_failed_upload": temps() - temps_before,
        },
        "seconds_until_ready_and_authenticated_ok_after_postgres_start": rec,
    }


def scenario_g_worker(stack: Stack) -> dict:
    out: dict = {}
    with httpx.Client(timeout=30) as c:
        h = register(c, "worker")

        def status(doc_id: str) -> str:
            j = c.get(f"{V1}/documents/{doc_id}", headers=h).json()
            ing, idx = j.get("ingestion") or {}, j.get("indexing") or {}
            extra = "".join(f" [{x.get('error_code')}]" for x in (ing, idx) if x.get("error_code"))
            return f"{ing.get('status')}/{idx.get('status')}{extra}"

        def upload(tag: str) -> str:
            text = f"Synthetic worker probe {tag}. " * 200
            r = c.post(f"{V1}/documents", headers=h, files={"file": (f"{tag}.txt", text.encode(), "text/plain")})
            assert r.status_code == 201, r.text
            return r.json()["id"]

        def wait_done(doc_id: str, limit: float) -> float | None:
            t0 = time.time()
            while time.time() - t0 < limit:
                if status(doc_id).startswith("completed/completed"):
                    return round(time.time() - t0, 1)
                time.sleep(0.5)
            return None

        out["normal_ingestion_seconds"] = wait_done(upload("normal"), 60)
        if stack.worker:
            kill_tree(stack.worker)  # hard kill, as a crash would
        time.sleep(1)
        doc = upload("while-worker-down")
        time.sleep(6)
        out["status_while_worker_down"] = status(doc)
        log = stack.tmp / "worker.log"
        out["worker_log_tail"] = log.read_text(errors="replace")[-1500:] if log.exists() else ""
        stack.start_worker()
        out["seconds_to_complete_after_worker_restart_without_recovery_sweep"] = wait_done(doc, 45)
        if out["seconds_to_complete_after_worker_restart_without_recovery_sweep"] is None:
            r = subprocess.run([stack.py(), "-m", "app.workers.recovery"], cwd=API_DIR, env=stack.env,
                               capture_output=True, text=True)
            out["recovery_command_exit"] = r.returncode
            out["seconds_to_complete_after_recovery_command"] = wait_done(doc, 60)
    return out


def scenario_h_sustained(stack: Stack, seconds: int) -> dict:
    with httpx.Client(timeout=20) as c:
        hs = [register(c, f"sus{i}") for i in range(6)]
    samples: list[dict] = []
    stop_flag = {"stop": False}
    lat: list[float] = []
    codes: dict[str, int] = {}

    async def run() -> None:
        async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=100)) as client:
            stop = time.perf_counter() + seconds

            async def sampler() -> None:
                while time.perf_counter() < stop:
                    row: dict = {"t": round(seconds - (stop - time.perf_counter()), 0)}
                    for name, p in (("api", stack.api), ("worker", stack.worker)):
                        if p is not None:
                            s = await asyncio.to_thread(proc_stats, p.pid)
                            if s:
                                row[name] = s
                    samples.append(row)
                    await asyncio.sleep(10)

            async def user(i: int) -> None:
                h = hs[i % len(hs)]
                n = 0
                while time.perf_counter() < stop:
                    n += 1
                    t = time.perf_counter()
                    try:
                        if n % 10 == 0:
                            r = await client.post(
                                f"{V1}/documents", headers=h,
                                files={"file": ("s.txt", (f"sustained {uuid.uuid4().hex} " * 50).encode(), "text/plain")},
                            )
                        elif n % 5 == 0:
                            r = await client.get(f"{V1}/documents", headers=h)
                        else:
                            r = await client.get(f"{V1}/auth/me", headers=h)
                        code = str(r.status_code)
                    except httpx.HTTPError as exc:
                        code = type(exc).__name__
                    lat.append((time.perf_counter() - t) * 1000)
                    codes[code] = codes.get(code, 0) + 1
                    await asyncio.sleep(0.01)

            await asyncio.gather(sampler(), *(user(i) for i in range(20)))

    asyncio.run(run())
    stop_flag["stop"] = True
    first = next((s for s in samples if "api" in s), None)
    last = next((s for s in reversed(samples) if "api" in s), None)
    return {
        "seconds": seconds,
        "concurrency": 20,
        "requests": len(lat),
        "latency_ms": summary(lat),
        "status": codes,
        "resource_samples": samples,
        "api_rss_growth_mb": round(last["api"]["rss_mb"] - first["api"]["rss_mb"], 1) if first and last else None,
        "api_cpu_seconds_used": round(last["api"]["cpu_s"] - first["api"]["cpu_s"], 1) if first and last else None,
    }


SCENARIOS = {
    "a_concurrency": scenario_a_concurrency,
    "b_large_payloads": scenario_b_large_payloads,
    "c_slow_clients": scenario_c_slow_clients,
    "d_pool": scenario_d_pool,
    "g_worker": scenario_g_worker,
    "h_sustained": None,  # needs --sustain
    "e_redis_outage": scenario_e_redis_outage,
    "f_db_outage": scenario_f_db_outage,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--sustain", type=int, default=90)
    ap.add_argument("--watchdog", type=int, default=0, help="dump all thread stacks after N seconds (debugging)")
    args = ap.parse_args()
    if args.watchdog:
        import faulthandler

        faulthandler.dump_traceback_later(args.watchdog, exit=False)
    only = [x for x in args.only.split(",") if x]
    stack = Stack()
    results: dict = {
        "environment": {
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version.split()[0],
            "note": "throwaway DB ia_perf, Redis db 1, fake providers, synthetic data, loopback only",
        }
    }
    try:
        stack.setup_db()
        stack.start_api()
        stack.start_worker()
        time.sleep(4)
        results["environment"]["api_idle"] = proc_stats(stack.api.pid) if stack.api else None
        for name, fn in SCENARIOS.items():
            if only and name not in only and name.split("_")[0] not in only:
                continue
            t = time.time()
            print(f"== {name}", flush=True)
            try:
                if name == "h_sustained":
                    results[name] = scenario_h_sustained(stack, args.sustain)
                else:
                    assert fn is not None
                    results[name] = fn(stack)
            except Exception as exc:  # noqa: BLE001  (record and carry on to the next scenario)
                results[name] = {"ERROR": f"{type(exc).__name__}: {exc}"}
            results[name]["_scenario_seconds"] = round(time.time() - t, 1)
            print(json.dumps(results[name], default=str)[:1500], flush=True)
            Path(args.out).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        results["environment"]["api_end"] = proc_stats(stack.api.pid) if stack.api else None
    finally:
        stack.stop_all()
        Path(args.out).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
