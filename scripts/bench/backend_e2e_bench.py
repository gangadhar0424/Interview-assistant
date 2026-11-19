# ruff: noqa: E501
"""End-to-end API benchmark against a RUNNING backend (standard library only).

Needs a reachable API with a ready session whose voice and reasoning providers are chosen, plus an
account. It never prints or stores audio, screenshots, transcripts, questions or answers: only
timings and status codes. Credentials come from the environment and are never printed.

    BENCH_API_URL=http://127.0.0.1:8000  BENCH_EMAIL=...  BENCH_PASSWORD=...  BENCH_SESSION_ID=<uuid>
    python scripts/bench/backend_e2e_bench.py --samples 30 --out result.json

What each number means (kept separate on purpose):
  client_total_ms          wall time seen by this script for the whole request
  server_reported_ms       the API's own `latency_ms` (validation + provider + detection), when returned
  network_and_client_ms    client_total - server_reported (DERIVED; includes TLS/connect, loopback and body read)
  provider time            is inside server_reported; see the backend histograms for the provider alone
For the question stream: first event (question.created), retrieval_ms (reported by the server),
first answer delta, and done are all measured from the moment the request was sent.

Use synthetic inputs only. Do not point this at an account holding real interview material.
"""

import argparse
import http.client
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stats import dump, print_table, summarize  # noqa: E402
from synth import QUESTIONS, make_png, make_wav  # noqa: E402


def now_ms() -> float:
    return time.perf_counter_ns() / 1e6


class Client:
    def __init__(self, base: str) -> None:
        parts = urllib.parse.urlparse(base)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.secure = parts.scheme == "https"
        self.token = ""

    def conn(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=60)

    def request(self, method: str, path: str, body: bytes | None, headers: dict[str, str]):
        conn = self.conn()
        if self.token:
            headers = {**headers, "Authorization": f"Bearer {self.token}"}
        conn.request(method, path, body=body, headers=headers)
        return conn, conn.getresponse()

    def login(self, email: str, password: str) -> None:
        payload = json.dumps({"email": email, "password": password}).encode()
        conn, resp = self.request(
            "POST", "/api/v1/auth/login", payload, {"Content-Type": "application/json"}
        )
        data = json.loads(resp.read() or b"{}")
        conn.close()
        if resp.status != 200 or "access_token" not in data:
            raise SystemExit(f"login failed with status {resp.status}")
        self.token = data["access_token"]


def timed_post(client: Client, path: str, body: bytes, content_type: str):
    start = now_ms()
    conn, resp = client.request("POST", path, body, {"Content-Type": content_type})
    raw = resp.read()
    total = now_ms() - start
    conn.close()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    return resp.status, total, data


def stream_question(client: Client, session_id: str, text: str):
    """Returns (status, marks) with ms offsets from request start; reads SSE frames, keeps no text."""
    marks: dict[str, float] = {}
    start = now_ms()
    body = json.dumps({"text": text}).encode()
    conn, resp = client.request(
        "POST",
        f"/api/v1/sessions/{session_id}/questions",
        body,
        {"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    marks["headers"] = now_ms() - start
    if resp.status != 200:
        resp.read()
        conn.close()
        return resp.status, marks
    event = ""
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("event:"):
            event = line[6:].strip()
            if event == "question.created":
                marks.setdefault("first_event", now_ms() - start)
            elif event == "answer.delta":
                marks.setdefault("first_token", now_ms() - start)
            elif event in ("answer.done", "answer.error"):
                marks["done"] = now_ms() - start
                marks["terminal"] = 1.0 if event == "answer.done" else 0.0
                break
        elif line.startswith("data:") and event == "retrieval.done":
            try:
                marks["server_retrieval_ms"] = float(json.loads(line[5:]).get("ms"))
            except (ValueError, TypeError):
                pass
    conn.close()
    return 200, marks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--audio-seconds", type=float, default=5.0)
    parser.add_argument("--out")
    args = parser.parse_args()

    base = os.environ.get("BENCH_API_URL", "")
    email = os.environ.get("BENCH_EMAIL", "")
    password = os.environ.get("BENCH_PASSWORD", "")
    session_id = os.environ.get("BENCH_SESSION_ID", "")
    if not (base and email and password and session_id):
        print("UNMEASURED: set BENCH_API_URL, BENCH_EMAIL, BENCH_PASSWORD and BENCH_SESSION_ID.")
        raise SystemExit(2)

    client = Client(base)
    client.login(email, password)
    results: dict[str, dict[str, float] | None] = {}
    failures: dict[str, int] = {}

    def fail(name: str) -> None:
        failures[name] = failures.get(name, 0) + 1

    wav = make_wav(args.audio_seconds)
    totals, servers, overheads = [], [], []
    for _ in range(args.samples):
        status, total, data = timed_post(
            client, f"/api/v1/sessions/{session_id}/transcribe", wav, "audio/wav"
        )
        if status != 200:
            fail("transcribe")
            continue
        totals.append(total)
        server = data.get("latency_ms")
        if isinstance(server, (int, float)):
            servers.append(float(server))
            overheads.append(max(0.0, total - float(server)))
    results["transcribe client_total_ms"] = summarize(totals)
    results["transcribe server_reported_ms"] = summarize(servers)
    results["transcribe network_and_client_ms (derived)"] = summarize(overheads)

    png = make_png(1280, 720)
    totals, servers, overheads = [], [], []
    for _ in range(max(5, args.samples // 3)):
        status, total, data = timed_post(
            client, f"/api/v1/sessions/{session_id}/screenshots", png, "image/png"
        )
        if status != 200:
            fail("screenshot")
            continue
        totals.append(total)
        server = data.get("latency_ms")
        if isinstance(server, (int, float)):
            servers.append(float(server))
            overheads.append(max(0.0, total - float(server)))
    results["screenshot client_total_ms"] = summarize(totals)
    results["screenshot server_reported_ms (validate + vision)"] = summarize(servers)
    results["screenshot network_and_client_ms (derived)"] = summarize(overheads)

    first_event, retrieval, first_token, done, token_to_done = [], [], [], [], []
    for i in range(args.samples):
        status, marks = stream_question(client, session_id, QUESTIONS[i % len(QUESTIONS)])
        if status != 200 or marks.get("terminal") != 1.0:
            fail("question")
            continue
        if "first_event" in marks:
            first_event.append(marks["first_event"])
        if "server_retrieval_ms" in marks:
            retrieval.append(marks["server_retrieval_ms"])
        if "first_token" in marks:
            first_token.append(marks["first_token"])
            token_to_done.append(marks["done"] - marks["first_token"])
        done.append(marks["done"])
    results["question submit -> first event (ms)"] = summarize(first_event)
    results["question server retrieval_ms (server-reported)"] = summarize(retrieval)
    results["question submit -> first answer token (ms)"] = summarize(first_token)
    results["question first token -> done (ms)"] = summarize(token_to_done)
    results["question submit -> done (ms)"] = summarize(done)

    print_table(
        "Backend end-to-end (ms). Provider time is inside the server-reported figures.", results
    )
    if failures:
        print(f"\nFailed requests by endpoint (excluded from the statistics): {failures}")
    dump(
        args.out,
        {
            "kind": "backend_e2e",
            "samples": args.samples,
            "unit": "ms",
            "results": results,
            "failures": failures,
        },
    )


if __name__ == "__main__":
    main()
