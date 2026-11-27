"""Prepare a user, a ready session and a bearer token on a loopback API; print them as JSON."""
import json
import subprocess
import sys
import uuid

import fitz
import httpx

BASE, DB = sys.argv[1], sys.argv[2]
c = httpx.Client(timeout=20)
pw = "Pentest-Passw0rd-" + uuid.uuid4().hex[:8]
email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
assert c.post(f"{BASE}/auth/signup", json={"email": email, "password": pw}).status_code == 201
token = c.post(f"{BASE}/auth/login", json={"email": email, "password": pw}).json()["access_token"]
h = {"Authorization": f"Bearer {token}"}
doc = fitz.open()
doc.new_page().insert_text((72, 72), "Synthetic resume for the handoff end-to-end check.")
r = c.post(f"{BASE}/resumes", headers=h, files={"file": ("r.pdf", doc.tobytes(), "application/pdf")})
assert r.status_code == 201, r.text
for sql in ("UPDATE ingestion_jobs SET status='completed'",
            "INSERT INTO ingestion_jobs (user_id, source_type, source_id, status, stage) SELECT user_id, source_type, source_id, 'completed', 'embed' FROM ingestion_jobs WHERE stage='extract' ON CONFLICT DO NOTHING"):
    subprocess.run(["docker", "exec", "interview-assistant-postgres-1", "psql", "-U", "ia_dev", "-d", DB, "-c", sql], check=True, capture_output=True)
pref = {"language": "en", "question_type": "auto", "answer_format": "bullets", "answer_length": "short",
        "answer_tone": "simple", "provider": "fake", "stt_provider": "fake"}
s = c.post(f"{BASE}/sessions", headers=h, json={"company_name": "E2eCo", "job_description": "Build secure things.", "resume_id": r.json()["id"], "preferences": pref})
assert s.status_code == 201, s.text
print(json.dumps({"token": token, "session_id": s.json()["id"]}))
