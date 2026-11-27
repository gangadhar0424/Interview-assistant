"""Authorized WebSocket attack probe: owner's own throwaway API on 127.0.0.1, throwaway database,
synthetic accounts. One line per attack. Usage: python -I ws_attack.py <api-log-file> <db-name>"""

import json
import subprocess
import sys
import time
import uuid

import fitz  # PyMuPDF, only to make a valid synthetic PDF
import httpx
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

HTTP = "http://127.0.0.1:8767/api/v1"
WS = "ws://127.0.0.1:8767/api/v1/ws/sessions"
LOG, DB = sys.argv[1], sys.argv[2]
PW = "Pentest-Passw0rd-" + uuid.uuid4().hex[:8]
results = []
c = httpx.Client(timeout=20)


def rec(pid, ok, detail):
    results.append(ok)
    print(f"{pid:<7} {'PASS' if ok else 'FAIL'}  {detail}", flush=True)


def psql(sql):
    subprocess.run(["docker", "exec", "interview-assistant-postgres-1", "psql", "-U", "ia_dev", "-d", DB, "-c", sql],
                   check=True, capture_output=True)


def user(tag):
    email = f"{tag}-{uuid.uuid4().hex[:8]}@example.com"
    assert c.post(f"{HTTP}/auth/signup", json={"email": email, "password": PW}).status_code == 201
    tok = c.post(f"{HTTP}/auth/login", json={"email": email, "password": PW}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def make_session(headers, n):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), f"Synthetic resume {n}. Skills: testing, python, security review.")
    r = c.post(f"{HTTP}/resumes", headers=headers, files={"file": (f"r{n}.pdf", doc.tobytes(), "application/pdf")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


A, B = user("alice"), user("bob")
resume_ids = [make_session(A, i) for i in range(4)] + [make_session(B, 99)]
psql("UPDATE ingestion_jobs SET status='completed'")
psql("INSERT INTO ingestion_jobs (user_id, source_type, source_id, status, stage) SELECT user_id, source_type, source_id, 'completed', 'embed' FROM ingestion_jobs WHERE stage='extract' ON CONFLICT DO NOTHING")
PREF = {"language": "en", "question_type": "auto", "answer_format": "bullets", "answer_length": "short",
        "answer_tone": "simple", "provider": "fake", "stt_provider": "fake"}


def new_session(headers, resume_id, n):
    r = c.post(f"{HTTP}/sessions", headers=headers,
               json={"company_name": f"WsCo{n}", "job_description": "Build secure things.", "resume_id": resume_id, "preferences": PREF})
    assert r.status_code == 201, r.text
    return r.json()["id"]


A_SESSIONS = [new_session(A, resume_ids[i], i) for i in range(4)]
B_SESSION = new_session(B, resume_ids[4], 9)


def ticket(headers, session):
    r = c.post(f"{HTTP}/auth/ws-ticket", headers=headers, json={"session_id": session})
    return r.status_code, (r.json().get("ticket") if r.status_code == 200 else None)


def dial(session, tk, origin=None):
    url = f"{WS}/{session}" + (f"?ticket={tk}" if tk is not None else "")
    try:
        return "open", connect(url, origin=origin, open_timeout=10)
    except InvalidStatus as exc:
        return "refused", exc.response.status_code
    except Exception as exc:  # noqa: BLE001
        return "error", type(exc).__name__


def hello(ws, version=1):
    ws.send(json.dumps({"type": "client.hello", "payload": {"protocol_version": version}}))


def close_code(ws, timeout=8):
    """Read until the server closes; returns the close code (or None if still open)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            ws.recv(timeout=max(0.1, end - time.time()))
        except ConnectionClosed as exc:
            return exc.rcvd.code if exc.rcvd else 1006
        except TimeoutError:
            break
    return None


# ---------------- handshake attacks (all must be the same bare refusal) ----------------
S = A_SESSIONS[0]
rec("WS-01", dial(S, None) == ("refused", 403), "no ticket -> 403")
rec("WS-02", dial(S, "A" * 43) == ("refused", 403), "well-formed but unknown ticket -> 403")
rec("WS-02b", dial(S, "not-a-ticket!!") == ("refused", 403), "malformed ticket -> 403")
rec("WS-03", dial("not-a-uuid", "A" * 43) == ("refused", 403), "malformed session id -> 403")

status, tk = ticket(A, S)
kind, ws = dial(S, tk)
ok_first = kind == "open"
if ok_first:
    ws.close()
second = dial(S, tk)
rec("WS-04", ok_first and second == ("refused", 403), f"ticket replay: first use opens={ok_first}, second use -> {second}")

_, tk = ticket(A, A_SESSIONS[1])
rec("WS-05", dial(S, tk) == ("refused", 403), "A's ticket for session 1 used on session 0 path -> 403 (and burned)")
rec("WS-05b", dial(A_SESSIONS[1], tk) == ("refused", 403), "the mismatched ticket was consumed, cannot be retried on the right path")

st, _ = ticket(B, S)
rec("WS-06", st == 404, f"B asks for a ticket for A's session -> {st}")
_, tkb = ticket(B, B_SESSION)
rec("WS-06b", dial(S, tkb) == ("refused", 403), "B's own ticket presented on A's session path -> 403")

_, tk = ticket(A, S)
rec("WS-07", dial(S, tk, origin="https://evil.example") == ("refused", 403), "cross-site WebSocket hijack: Origin not on the allow-list -> 403")
_, tk = ticket(A, S)
kind, ws = dial(S, tk, origin="http://localhost:3000")
rec("WS-07b", kind == "open", f"configured web origin is accepted -> {kind}")
if kind == "open":
    ws.close()
_, tk = ticket(A, S)
kind, ws = dial(S, tk, origin=None)
rec("INFO", True, f"no Origin header (non-browser client) -> {kind} (by design: browsers always send Origin; the ticket is still required)")
if kind == "open":
    ws.close()

_, tk = ticket(A, S)
time.sleep(6)  # instance runs with WS_TICKET_TTL_SECONDS=5
rec("WS-08", dial(S, tk) == ("refused", 403), "ticket older than its lifetime -> 403")

# ---------------- post-handshake abuse ----------------
def open_ready(headers=A, session=S):
    _, t = ticket(headers, session)
    kind, w = dial(session, t, origin="http://localhost:3000")
    assert kind == "open", (kind, w)
    ready = json.loads(w.recv(timeout=10))
    assert ready["type"] == "session.ready"
    return w, ready


w, ready = open_ready()
limits = ready["payload"]["limits"]
big = json.dumps({"type": "ping", "payload": {}, "id": "x" * (limits["max_frame_bytes"] + 100)})
w.send(big)
rec("WS-09", close_code(w) == 1009, f"text frame over max_frame_bytes ({limits['max_frame_bytes']}) -> close 1009")

w, _ = open_ready()
w.send(b"\x00\x01binary-audio-like")
rec("WS-10", close_code(w) == 1003, "binary frame -> close 1003 (unsupported data)")

w, _ = open_ready()
hello(w)
codes = []
for i in range(6):
    try:
        w.send("{not json" if i % 2 == 0 else json.dumps({"type": "nope", "payload": {}}))
    except ConnectionClosed:
        break  # the server already closed it: that is the expected outcome
    time.sleep(0.15)
cc = close_code(w)
rec("WS-11", cc == 4003, f"six malformed/unknown messages after hello -> close {cc} (protocol error)")

w, _ = open_ready()
w.send("{not json")
rec("WS-12", close_code(w) == 4003, "malformed first message (before hello) -> fatal close 4003")

w, _ = open_ready()
w.send(json.dumps({"type": "question.submit", "payload": {"text": "what is a mutex?"}}))
rec("WS-13", close_code(w) == 4003, "question.submit BEFORE client.hello -> fatal close 4003 (no processing)")

w, _ = open_ready()
hello(w, version=999)
rec("WS-14", close_code(w) == 4004, "unsupported protocol version -> close 4004")

w, _ = open_ready()
t0 = time.time()
cc = close_code(w, timeout=15)
rec("WS-15", cc == 4003 and (time.time() - t0) < 14, f"no hello within the timeout -> close {cc} after {time.time() - t0:.1f}s (instance hello timeout 4s)")

w, _ = open_ready()
hello(w)
for _ in range(60):
    try:
        w.send(json.dumps({"type": "ping", "payload": {}}))
    except ConnectionClosed:
        break
rec("WS-16", close_code(w) == 4005, "message flood above max_messages_per_second -> close 4005")

w, _ = open_ready()
hello(w)
w.send(json.dumps({"type": "question.submit", "payload": {"text": "q" * 15000}}))
deadline = time.time() + 8
saw_error = False
while time.time() < deadline:
    try:
        msg = json.loads(w.recv(timeout=2))
    except (TimeoutError, ConnectionClosed):
        break
    if msg.get("type") == "error":
        saw_error = True
        break
rec("WS-17", saw_error, "question.submit with 15 000 characters (over the 2000-character limit, under the frame limit) -> error event, no crash")
w.close()

# one socket per (user, session): a second connection supersedes the first
w1, _ = open_ready()
hello(w1)
w2, _ = open_ready()
hello(w2)
rec("WS-18", close_code(w1) == 4001, "second connection for the same (user, session) supersedes the first (close 4001)")
w2.close()

# per-user connection cap (3): open 3 distinct sessions, the 4th is refused with 4429
open_sockets = []
for i in range(3):
    ws_i, _ = open_ready(A, A_SESSIONS[i])
    hello(ws_i)
    open_sockets.append(ws_i)
_, tk4 = ticket(A, A_SESSIONS[3])
kind, w4 = dial(A_SESSIONS[3], tk4, origin="http://localhost:3000")
cc = None
if kind == "open":
    try:
        w4.recv(timeout=5)
    except ConnectionClosed as exc:
        cc = exc.rcvd.code if exc.rcvd else 1006
    except TimeoutError:
        pass
    if cc is None:
        cc = close_code(w4, timeout=5)
rec("WS-19", cc == 4429, f"fourth concurrent socket for one user -> close {cc} (cap is 3)")
for s in open_sockets:
    s.close()

# ticket request rate limit
codes = []
for _ in range(150):
    r = c.post(f"{HTTP}/auth/ws-ticket", headers=A, json={"session_id": S})
    codes.append(r.status_code)
rec("WS-20", 429 in codes, f"ticket requests above the per-minute limit are throttled (first 429 at request {codes.index(429) + 1 if 429 in codes else 'never'})")

# liveness after the abuse, and a normal session still works
rec("WS-21", c.get("http://127.0.0.1:8767/health").status_code == 200, "API still healthy after all attacks")
time.sleep(61)  # let the ticket rate window pass
w, ready = open_ready()
hello(w)
w.send(json.dumps({"type": "ping", "payload": {}}))
pong = json.loads(w.recv(timeout=5))
rec("WS-22", pong.get("type") == "pong", f"a legitimate client still works normally -> {pong.get('type')}")
w.close()

# tickets and verifier-like secrets in the server log
log = open(LOG, encoding="utf-8", errors="replace").read()
leaked = [t for t in {tk, tkb} if t and t in log]
rec("WS-23", not leaked and "ticket=" not in log.replace("ticket=[redacted]", ""), f"no ticket value appears in the server log ({len(log)} bytes scanned)")

print(f"\nSUMMARY: {sum(results)} PASS, {len(results) - sum(results)} FAIL of {len(results)}")
sys.exit(0 if all(results) else 1)
