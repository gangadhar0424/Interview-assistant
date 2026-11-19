# ruff: noqa: E501
"""Real-provider validation against a RUNNING API (M-3). Standard library only.

It signs up a throw-away account on the target API, uploads a synthetic resume and job description,
and measures the REAL configured providers end to end. It prints and stores only timings, counts,
status codes, provider ids and error codes. It never prints or stores audio, transcripts (it
computes word error rate in memory and discards the text), questions, answers or credentials.

    python scripts/validate/provider_validation.py --fixtures <dir> --section stt   --out stt.json
    python scripts/validate/provider_validation.py --fixtures <dir> --section llm   --out llm.json
    python scripts/validate/provider_validation.py --fixtures <dir> --section failure --out failure.json

Environment: VAL_API_URL (default http://127.0.0.1:8000). Use a throw-away database.
Fixtures come from scripts/validate/fixtures.py (+ speech_*.wav generated from truth.json).
"""

import argparse
import glob
import http.client
import json
import os
import re
import secrets
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bench"))
from stats import summarize  # noqa: E402


def now_ms() -> float:
    return time.perf_counter_ns() / 1e6


class Api:
    def __init__(self, base: str) -> None:
        host, _, port = base.replace("http://", "").partition(":")
        self.host, self.port = host, int(port or 80)
        self.token = ""

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict | None = None,
        timeout: float = 120,
    ):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        h = dict(headers or {})
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        conn.request(method, path, body=body, headers=h)
        return conn, conn.getresponse()

    def json(self, method: str, path: str, payload: dict | None = None):
        body = json.dumps(payload).encode() if payload is not None else None
        conn, resp = self.call(
            method, path, body, {"Content-Type": "application/json"} if body else {}
        )
        raw = resp.read()
        conn.close()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return resp.status, data

    def upload(self, path: str, filename: str, data: bytes):
        boundary = "----val" + secrets.token_hex(8)
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
            ).encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        conn, resp = self.call(
            "POST", path, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw or b"{}")


def setup_account(api: Api, fixtures: str):
    email = f"m3-{secrets.token_hex(5)}@example.com"
    password = secrets.token_urlsafe(18)
    status, _ = api.json("POST", "/api/v1/auth/signup", {"email": email, "password": password})
    if status not in (200, 201):
        raise SystemExit(f"signup failed with status {status}")
    status, data = api.json("POST", "/api/v1/auth/login", {"email": email, "password": password})
    if status != 200:
        raise SystemExit(f"login failed with status {status}")
    api.token = data["access_token"]
    ids = {}
    started = now_ms()
    for kind, name in (("resumes", "resume.docx"), ("documents", "jd.docx")):
        with open(os.path.join(fixtures, name), "rb") as handle:
            status, item = api.upload(f"/api/v1/{kind}", name, handle.read())
        if status != 201:
            raise SystemExit(f"upload {kind} failed with status {status}")
        ids[kind] = item["id"]
    deadline = time.time() + 600
    while time.time() < deadline:
        ready = 0
        for kind in ("resumes", "documents"):
            _, listing = api.json("GET", f"/api/v1/{kind}")
            row = next((r for r in listing.get("items", []) if r["id"] == ids[kind]), None)
            states = (
                ((row or {}).get("ingestion") or {}).get("status"),
                ((row or {}).get("indexing") or {}).get("status"),
            )
            if states == ("completed", "completed"):
                ready += 1
            elif "failed" in states:
                raise SystemExit("ingestion failed")
        if ready == 2:
            break
        time.sleep(1.0)
    else:
        raise SystemExit("ingestion timed out")
    return ids, now_ms() - started


def create_session(api: Api, ids: dict, llm: str, stt: str) -> str | None:
    status, data = api.json(
        "POST",
        "/api/v1/sessions",
        {
            "type": "interview",
            "company_name": "Halcyon Payments",
            "job_description": "Staff Backend Engineer. Payments, Kafka, PostgreSQL, idempotency.",
            "resume_id": ids["resumes"],
            "document_ids": [ids["documents"]],
            "preferences": {
                "language": "en",
                "question_type": "auto",
                "answer_format": "script_bullets",
                "answer_length": "balanced",
                "answer_tone": "simple",
                "provider": llm,
                "stt_provider": stt,
            },
        },
    )
    if status != 201:
        print(f"  session create failed: status={status} code={data.get('error', {}).get('code')}")
        return None
    sid = data["id"]
    for _ in range(60):
        _, detail = api.json("GET", f"/api/v1/sessions/{sid}")
        if detail.get("status") == "ready":
            return sid
        time.sleep(0.5)
    return None


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def wer(reference: str, hypothesis: str) -> float:
    r, h = words(reference), words(hypothesis)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / max(1, len(r))


def stt_section(
    api: Api, ids: dict, fixtures: str, truth: dict, providers: list[str], reps: int
) -> dict:
    out: dict = {}
    wavs = sorted(glob.glob(os.path.join(fixtures, "speech_*.wav")))
    for provider in providers:
        sid = create_session(api, ids, "openai", provider)
        if not sid:
            out[provider] = {"error": "session_create_failed"}
            continue
        totals, servers, wers, question_flags = [], [], [], []
        ok = fail = 0
        codes: dict[str, int] = {}
        seconds = 0.0
        for _ in range(reps):
            for path in wavs:
                index = int(re.search(r"_(\d+)\.wav$", path).group(1)) - 1  # type: ignore[union-attr]
                data = open(path, "rb").read()
                started = now_ms()
                conn, resp = api.call(
                    "POST",
                    f"/api/v1/sessions/{sid}/transcribe",
                    data,
                    {"Content-Type": "audio/wav"},
                )
                raw = resp.read()
                conn.close()
                elapsed = now_ms() - started
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                if resp.status == 200:
                    ok += 1
                    totals.append(elapsed)
                    if isinstance(body.get("latency_ms"), (int, float)):
                        servers.append(float(body["latency_ms"]))
                    wers.append(wer(truth["spoken"][index], body.get("text", "")))
                    question_flags.append(bool(body.get("is_question")))
                    seconds += (len(data) - 44) / 32000
                else:
                    fail += 1
                    code = body.get("error", {}).get("code", str(resp.status))
                    codes[code] = codes.get(code, 0) + 1
                    if resp.status == 429:
                        time.sleep(float(resp.getheader("Retry-After") or 5))
                time.sleep(1.0)  # stay under the per-user transcribe limit (40/min by default)
        n = ok + fail
        out[provider] = {
            "requests": n,
            "success_rate": round(ok / n, 3) if n else None,
            "failure_codes": codes,
            "client_total_ms": summarize(totals),
            "server_reported_ms": summarize(servers),
            "wer": summarize(wers),
            "mean_wer": round(sum(wers) / len(wers), 4) if wers else None,
            "exact_match_rate": round(sum(1 for w in wers if w == 0) / len(wers), 3)
            if wers
            else None,
            "is_question_rate": round(sum(question_flags) / len(question_flags), 3)
            if question_flags
            else None,
            "audio_seconds_ok": round(seconds, 1),
        }
        print(
            f"  stt {provider}: ok={ok} fail={fail} p50={out[provider]['client_total_ms'] and out[provider]['client_total_ms']['p50']} meanWER={out[provider]['mean_wer']}"
        )
    return out


def ask(api: Api, sid: str, text: str) -> dict:
    marks: dict = {"error": None, "status": 0}
    started = now_ms()
    conn, resp = api.call(
        "POST",
        f"/api/v1/sessions/{sid}/questions",
        json.dumps({"text": text}).encode(),
        {"Content-Type": "application/json", "Accept": "text/event-stream"},
        timeout=150,
    )
    marks["status"] = resp.status
    if resp.status != 200:
        raw = resp.read()
        conn.close()
        try:
            marks["error"] = json.loads(raw or b"{}").get("error", {}).get("code", str(resp.status))
        except ValueError:
            marks["error"] = str(resp.status)
        marks["retry_after"] = resp.getheader("Retry-After")
        marks["total"] = now_ms() - started
        return marks
    event = ""
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("event:"):
            event = line[6:].strip()
            if event == "answer.delta":
                marks.setdefault("first_token", now_ms() - started)
            if event in ("answer.done", "answer.error"):
                marks["done"] = now_ms() - started
                marks["terminal"] = event
        elif line.startswith("data:"):
            try:
                data = json.loads(line[5:])
            except ValueError:
                continue
            if event == "retrieval.done":
                marks["retrieval_ms"] = data.get("ms")
            elif event == "answer.started":
                marks["provider"] = data.get("provider")
            elif event == "answer.done":
                m = data.get("metrics", {})
                marks["srv_first_token_ms"] = m.get("first_token_ms")
                marks["srv_total_ms"] = m.get("total_ms")
                marks["in_tokens"] = data.get("input_tokens")
                marks["out_tokens"] = data.get("output_tokens")
                marks["source_kind"] = (data.get("source") or {}).get("kind")
                marks["fallback_used"] = data.get("fallback_used")
            elif event == "answer.error":
                marks["error"] = data.get("code")
        if marks.get("terminal") and line.startswith("data:"):
            break  # the terminal event's data line has been read
    conn.close()
    marks.setdefault("done", now_ms() - started)
    return marks


def llm_section(api: Api, ids: dict, truth: dict, providers: list[str], reps: int) -> dict:
    out: dict = {}
    for provider in providers:
        sid = create_session(api, ids, provider, "openai")
        if not sid:
            out[provider] = {"error": "session_create_failed"}
            continue
        rows = []
        for _ in range(reps):
            for question in truth["ask"]:
                for attempt in range(4):
                    row = ask(api, sid, question)
                    if row.get("status") == 429 and attempt < 3:
                        time.sleep(float(row.get("retry_after") or 6))
                        continue
                    break
                rows.append(row)
                time.sleep(1.0)
        model_rows = [
            r for r in rows if r.get("terminal") == "answer.done" and r.get("provider") == provider
        ]
        exact_rows = [
            r
            for r in rows
            if r.get("terminal") == "answer.done" and r.get("provider") == "extractive"
        ]
        failed = [r for r in rows if r.get("terminal") != "answer.done"]
        codes: dict[str, int] = {}
        for r in failed:
            codes[str(r.get("error"))] = codes.get(str(r.get("error")), 0) + 1
        out[provider] = {
            "requests": len(rows),
            "completed": sum(1 for r in rows if r.get("terminal") == "answer.done"),
            "completion_rate": round(
                sum(1 for r in rows if r.get("terminal") == "answer.done") / len(rows), 3
            ),
            "model_answers": len(model_rows),
            "exact_text_answers_no_model_call": len(exact_rows),
            "failure_codes": codes,
            "fallback_used_count": sum(1 for r in rows if r.get("fallback_used")),
            "retrieval_ms_server": summarize(
                [float(r["retrieval_ms"]) for r in rows if r.get("retrieval_ms") is not None]
            ),
            "client_first_token_ms_model": summarize(
                [r["first_token"] for r in model_rows if "first_token" in r]
            ),
            "client_total_ms_model": summarize([r["done"] for r in model_rows]),
            "server_first_token_ms_model": summarize(
                [
                    float(r["srv_first_token_ms"])
                    for r in model_rows
                    if r.get("srv_first_token_ms") is not None
                ]
            ),
            "server_total_ms_model": summarize(
                [float(r["srv_total_ms"]) for r in model_rows if r.get("srv_total_ms") is not None]
            ),
            "input_tokens_mean": round(
                sum(r.get("in_tokens") or 0 for r in model_rows) / max(1, len(model_rows)), 1
            ),
            "output_tokens_mean": round(
                sum(r.get("out_tokens") or 0 for r in model_rows) / max(1, len(model_rows)), 1
            ),
            "input_tokens_total": sum(r.get("in_tokens") or 0 for r in model_rows),
            "output_tokens_total": sum(r.get("out_tokens") or 0 for r in model_rows),
        }
        o = out[provider]
        print(
            f"  llm {provider}: {o['completed']}/{o['requests']} completed, model={o['model_answers']} exact={o['exact_text_answers_no_model_call']} codes={codes}"
        )
    return out


def failure_section(
    api: Api, ids: dict, truth: dict, bad: dict, good_llm: str, good_stt: str
) -> dict:
    """The server was started with an invalid key for `bad` providers. Selection is exclusive (D-57):
    a failing provider must give a safe error and must NOT be silently replaced."""
    out: dict = {"llm": {}, "stt": {}}
    wav = open(sorted(glob.glob(os.path.join(truth["_fixtures"], "speech_*.wav")))[0], "rb").read()
    for provider in bad.get("llm", []):
        sid = create_session(api, ids, provider, good_stt)
        rows = []
        for _ in range(5):
            row = ask(api, sid, truth["ask"][3])
            rows.append(
                {
                    "terminal": row.get("terminal"),
                    "error": row.get("error"),
                    "status": row.get("status"),
                    "ms": round(row["done"] if "done" in row else row.get("total", 0), 1),
                    "answered_by": row.get("provider"),
                }
            )
        out["llm"][provider] = rows
        print(
            f"  llm failure {provider}: {[(r['terminal'] or r['status'], r['error'], r['ms']) for r in rows]}"
        )
    sid = create_session(api, ids, good_llm, good_stt)
    row = ask(api, sid, truth["ask"][3])
    out["llm"]["healthy_control"] = {
        "terminal": row.get("terminal"),
        "answered_by": row.get("provider"),
        "ms": round(row["done"], 1),
    }
    for provider in bad.get("stt", []):
        sid = create_session(api, ids, good_llm, provider)
        rows = []
        for _ in range(5):
            started = now_ms()
            conn, resp = api.call(
                "POST", f"/api/v1/sessions/{sid}/transcribe", wav, {"Content-Type": "audio/wav"}
            )
            raw = resp.read()
            conn.close()
            try:
                code = json.loads(raw or b"{}").get("error", {}).get("code")
            except ValueError:
                code = None
            rows.append({"status": resp.status, "error": code, "ms": round(now_ms() - started, 1)})
        out["stt"][provider] = rows
        print(f"  stt failure {provider}: {[(r['status'], r['error'], r['ms']) for r in rows]}")
    sid = create_session(api, ids, good_llm, good_stt)
    started = now_ms()
    conn, resp = api.call(
        "POST", f"/api/v1/sessions/{sid}/transcribe", wav, {"Content-Type": "audio/wav"}
    )
    resp.read()
    conn.close()
    out["stt"]["healthy_control"] = {"status": resp.status, "ms": round(now_ms() - started, 1)}
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--section", choices=["stt", "llm", "failure"], required=True)
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--llm", default="openai,groq,anthropic,gemini")
    parser.add_argument("--stt", default="openai,groq,sarvam,gemini")
    parser.add_argument("--bad-llm", default="")
    parser.add_argument("--bad-stt", default="")
    parser.add_argument("--good-llm", default="groq")
    parser.add_argument("--good-stt", default="groq")
    parser.add_argument("--out")
    args = parser.parse_args()

    truth = json.load(open(os.path.join(args.fixtures, "truth.json"), encoding="utf-8"))
    truth["_fixtures"] = args.fixtures
    api = Api(os.environ.get("VAL_API_URL", "http://127.0.0.1:8000"))
    ids, ingest_ms = setup_account(api, args.fixtures)
    print(f"setup ok; upload to indexed (2 files, real embeddings): {ingest_ms / 1000:.1f} s")
    result: dict = {"section": args.section, "ingest_two_files_to_indexed_ms": round(ingest_ms, 1)}
    if args.section == "stt":
        result["results"] = stt_section(
            api, ids, args.fixtures, truth, args.stt.split(","), args.reps
        )
    elif args.section == "llm":
        result["results"] = llm_section(api, ids, truth, args.llm.split(","), args.reps)
    else:
        bad = {
            "llm": [p for p in args.bad_llm.split(",") if p],
            "stt": [p for p in args.bad_stt.split(",") if p],
        }
        result["results"] = failure_section(api, ids, truth, bad, args.good_llm, args.good_stt)
    if args.out:
        json.dump(result, open(args.out, "w", encoding="utf-8"), indent=2)
    print("done")


if __name__ == "__main__":
    main()
