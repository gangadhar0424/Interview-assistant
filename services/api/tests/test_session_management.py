"""M-9 session management: lifecycle, derived state, isolation, idempotent create, export."""

import unicodedata
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.sessions.idempotency import IdempotencyStore
from tests.conftest import RunSql
from tests.helpers import register
from tests.realtime_fakes import FakeRedis
from tests.test_sessions import SESSIONS, body, create, new_file, safe

SSE = {"Accept": "text/event-stream"}


class World:
    def __init__(self, client: TestClient, run_sql: RunSql) -> None:
        self.client, self.run_sql = client, run_sql
        self.redis = FakeRedis()
        client.app.state.idempotency_store = IdempotencyStore(self.redis)  # type: ignore[attr-defined]
        self.alice = register(client, "alice@example.com")
        self.bob = register(client, "bob@example.com")
        self.alice_resume = new_file(client, run_sql, self.alice, "resumes")
        self.bob_resume = new_file(client, run_sql, self.bob, "resumes")

    def make(self, who: str = "alice", **over: Any) -> dict[str, Any]:
        headers = self.alice if who == "alice" else self.bob
        resume = self.alice_resume if who == "alice" else self.bob_resume
        response = create(self.client, headers, body(resume, **over))
        assert response.status_code == 201, response.text
        data: dict[str, Any] = response.json()
        return data

    def get(self, sid: str, who: str = "alice") -> dict[str, Any]:
        headers = self.alice if who == "alice" else self.bob
        response = self.client.get(f"{SESSIONS}/{sid}", headers=headers)
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()
        return data

    def listing(self, who: str = "alice") -> list[dict[str, Any]]:
        headers = self.alice if who == "alice" else self.bob
        items: list[dict[str, Any]] = self.client.get(SESSIONS, headers=headers).json()["items"]
        return items

    def user_id(self, email: str) -> str:
        return str(self.run_sql("SELECT id FROM users WHERE email = :e", e=email)[0]["id"])

    def add_question(
        self,
        sid: str,
        text: str = "Tell me about a project.",
        *,
        email: str = "alice@example.com",
        answer: str | None = "I built a system.",
        answer_status: str = "complete",
        error_code: str | None = None,
        age_seconds: int = 0,
    ) -> str:
        uid = self.user_id(email)
        qid = str(uuid.uuid4())
        self.run_sql(
            "INSERT INTO detected_questions (id, session_id, user_id, raw_text, status) "
            "VALUES (:q, :s, :u, :t, 'answered')",
            q=qid,
            s=sid,
            u=uid,
            t=text,
        )
        if answer is not None:
            self.run_sql(
                "INSERT INTO generated_answers (question_id, session_id, user_id, text, "
                "generation_config, prompt_version, provider, model, status, error_code, "
                "created_at) VALUES (:q, :s, :u, :t, '{}'::jsonb, 'v5', 'fake', 'fake-model', "
                ":st, :ec, now() - make_interval(secs => :age))",
                q=qid,
                s=sid,
                u=uid,
                t=answer,
                st=answer_status,
                ec=error_code,
                age=age_seconds,
            )
        return qid


@pytest.fixture
def world(make_client: Callable[..., TestClient], run_sql: RunSql) -> World:
    return World(make_client(), run_sql)


# ---------------------------------------------------------------- auth


def test_new_endpoints_require_authentication(client: TestClient) -> None:
    sid = uuid.uuid4()
    assert client.post(f"{SESSIONS}/{sid}/end").status_code == 401
    assert client.post(f"{SESSIONS}/{sid}/reopen").status_code == 401
    assert client.get(f"{SESSIONS}/{sid}/export").status_code == 401


# ------------------------------------------------- history and derived state


def test_listing_carries_activity_fields(world: World) -> None:
    created = world.make(title="Round one")
    assert created["activity_state"] == "active"
    assert created["question_count"] == 0
    assert created["last_activity_at"] == created["updated_at"]
    world.add_question(created["id"])
    item = world.listing()[0]
    assert item["question_count"] == 1
    assert item["activity_state"] == "active"
    assert item["last_activity_at"] >= item["updated_at"]
    assert world.get(created["id"])["question_count"] == 1


def test_a_cut_off_answer_marks_the_session_interrupted(world: World) -> None:
    sid = world.make()["id"]
    world.add_question(
        sid, answer="partial", answer_status="error", error_code="generation_interrupted"
    )
    assert world.get(sid)["activity_state"] == "interrupted"
    assert world.listing()[0]["activity_state"] == "interrupted"


def test_a_stale_streaming_answer_is_interrupted_but_a_fresh_one_is_active(world: World) -> None:
    stale, fresh = world.make()["id"], world.make()["id"]
    world.add_question(stale, answer="x", answer_status="streaming", age_seconds=3600)
    world.add_question(fresh, answer="x", answer_status="streaming", age_seconds=1)
    assert world.get(stale)["activity_state"] == "interrupted"
    assert world.get(fresh)["activity_state"] == "active"


def test_a_later_good_answer_clears_interrupted(world: World) -> None:
    sid = world.make()["id"]
    world.add_question(sid, answer_status="error", error_code="generation_interrupted")
    world.add_question(sid, "Another?")
    assert world.get(sid)["activity_state"] == "active"


def test_listing_pagination_still_works_with_activity(world: World) -> None:
    for n in range(3):
        world.make(title=f"S{n}")
    first = world.client.get(f"{SESSIONS}?limit=2", headers=world.alice).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = world.client.get(
        f"{SESSIONS}?limit=2&cursor={first['next_cursor']}", headers=world.alice
    ).json()
    assert len(second["items"]) == 1
    assert all("activity_state" in i for i in first["items"] + second["items"])


# ------------------------------------------------------------- operations


def test_rename_persists_and_bumps_activity(world: World) -> None:
    created = world.make(title="Old")
    response = world.client.patch(
        f"{SESSIONS}/{created['id']}", headers=world.alice, json={"title": "  New name "}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "New name"
    assert world.listing()[0]["title"] == "New name"
    assert response.json()["last_activity_at"] >= created["last_activity_at"]


def test_complete_locks_edits_but_not_the_title_and_is_idempotent(world: World) -> None:
    sid = world.make()["id"]
    ended = world.client.post(f"{SESSIONS}/{sid}/end", headers=world.alice)
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"
    assert ended.json()["activity_state"] == "completed"
    again = world.client.post(f"{SESSIONS}/{sid}/end", headers=world.alice)
    assert again.status_code == 200 and again.json()["status"] == "ended"
    assert (
        world.client.patch(
            f"{SESSIONS}/{sid}", headers=world.alice, json={"company_name": "X"}
        ).status_code
        == 409
    )
    assert (
        world.client.patch(
            f"{SESSIONS}/{sid}", headers=world.alice, json={"title": "Done"}
        ).status_code
        == 200
    )


def test_a_completed_session_refuses_questions_and_handoff(world: World) -> None:
    sid = world.make()["id"]
    world.client.post(f"{SESSIONS}/{sid}/end", headers=world.alice)
    asked = world.client.post(
        f"{SESSIONS}/{sid}/questions", headers={**world.alice, **SSE}, json={"text": "Hi?"}
    )
    assert asked.status_code == 409 and asked.json()["error"]["code"] == "session_not_ready"
    handoff = world.client.post(f"{SESSIONS}/{sid}/handoff", headers=world.alice)
    assert handoff.status_code in (409, 429, 503)  # never issues a code for an ended session


def test_reopen_returns_a_completed_session_to_ready(world: World) -> None:
    sid = world.make()["id"]
    assert world.client.post(f"{SESSIONS}/{sid}/reopen", headers=world.alice).status_code == 200
    world.client.post(f"{SESSIONS}/{sid}/end", headers=world.alice)
    reopened = world.client.post(f"{SESSIONS}/{sid}/reopen", headers=world.alice)
    assert reopened.json()["status"] == "ready"
    assert reopened.json()["activity_state"] == "active"


def test_archived_and_draft_sessions_cannot_be_ended_or_reopened(world: World) -> None:
    sid = world.make()["id"]
    world.run_sql("UPDATE sessions SET status = 'archived' WHERE id = :i", i=sid)
    for action in ("end", "reopen"):
        response = world.client.post(f"{SESSIONS}/{sid}/{action}", headers=world.alice)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_not_transitionable"
    assert world.get(sid)["status"] == "archived"


def test_delete_removes_the_session_its_history_and_exports_but_not_the_resume(
    world: World,
) -> None:
    sid = world.make()["id"]
    world.add_question(sid)
    assert world.client.delete(f"{SESSIONS}/{sid}", headers=world.alice).status_code == 204
    assert world.client.get(f"{SESSIONS}/{sid}", headers=world.alice).status_code == 404
    assert world.client.get(f"{SESSIONS}/{sid}/export", headers=world.alice).status_code == 404
    assert world.run_sql("SELECT count(*) AS n FROM detected_questions")[0]["n"] == 0
    assert world.run_sql("SELECT count(*) AS n FROM generated_answers")[0]["n"] == 0
    assert world.run_sql("SELECT count(*) AS n FROM resumes")[0]["n"] == 2


# --------------------------------------------------------------- isolation


def test_foreign_session_ids_are_rejected_like_unknown_ones(world: World) -> None:
    bobs = world.make("bob", title="Bob private")
    world.add_question(bobs["id"], "Bob secret question", email="bob@example.com")
    unknown = str(uuid.uuid4())
    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("post", "end", {}),
        ("post", "reopen", {}),
        ("get", "export", {}),
        ("get", "", {}),
        ("patch", "", {"json": {"title": "stolen"}}),
        ("delete", "", {}),
    ]
    for method, suffix, extra in calls:
        path = f"{SESSIONS}/{{}}/{suffix}".rstrip("/")
        foreign = getattr(world.client, method)(
            path.format(bobs["id"]), headers=world.alice, **extra
        )
        missing = getattr(world.client, method)(path.format(unknown), headers=world.alice, **extra)
        assert foreign.status_code == 404, (method, suffix)
        assert safe(foreign) == safe(missing), (method, suffix)
    after = world.get(bobs["id"], "bob")
    assert after["title"] == "Bob private" and after["status"] == "ready"


def test_a_users_listing_never_contains_another_users_sessions(world: World) -> None:
    world.make("alice", title="A1")
    world.make("bob", title="B1")
    assert [i["title"] for i in world.listing("alice")] == ["A1"]
    assert [i["title"] for i in world.listing("bob")] == ["B1"]


def test_each_session_keeps_its_own_job_description_and_resume(world: World) -> None:
    second_resume = new_file(world.client, world.run_sql, world.alice, "resumes")
    a = world.make(job_description="Role A: Rust systems", company_name="Alpha")
    response = create(
        world.client,
        world.alice,
        body(second_resume, job_description="Role B: Frontend", company_name="Beta"),
    )
    b = response.json()
    assert world.get(a["id"])["job_description"] == "Role A: Rust systems"
    assert world.get(b["id"])["job_description"] == "Role B: Frontend"
    assert world.get(a["id"])["resume"]["id"] == world.alice_resume
    assert world.get(b["id"])["resume"]["id"] == second_resume
    # Editing B never touches A.
    world.client.patch(
        f"{SESSIONS}/{b['id']}", headers=world.alice, json={"job_description": "Changed"}
    )
    assert world.get(a["id"])["job_description"] == "Role A: Rust systems"


def test_a_new_session_has_no_questions_or_answers_from_another_session(world: World) -> None:
    old = world.make(company_name="Old Co")["id"]
    world.add_question(old, "Old question text", answer="Old answer text")
    fresh = world.make(company_name="New Co")["id"]
    history = world.client.get(f"{SESSIONS}/{fresh}/questions", headers=world.alice).json()
    assert history["items"] == []
    exported = world.client.get(f"{SESSIONS}/{fresh}/export", headers=world.alice).text
    assert "Old question text" not in exported and "Old answer text" not in exported
    assert world.get(fresh)["question_count"] == 0


def test_session_specific_provider_settings_stay_with_their_session(world: World) -> None:
    a = world.make(prefs={"model": None})
    b = world.make(prefs={"answer_tone": "formal"})
    world.client.patch(
        f"{SESSIONS}/{b['id']}",
        headers=world.alice,
        json={"preferences": {"answer_tone": "simple"}},
    )
    assert world.get(a["id"])["preferences"]["answer_tone"] == "simple"
    assert world.get(b["id"])["preferences"]["answer_tone"] == "simple"
    world.client.patch(
        f"{SESSIONS}/{a['id']}",
        headers=world.alice,
        json={"preferences": {"answer_tone": "formal"}},
    )
    assert world.get(b["id"])["preferences"]["answer_tone"] == "simple"


# ------------------------------------------------------- idempotent create


def create_with_key(world: World, key: str, who: str = "alice", **over: Any) -> Any:
    headers = world.alice if who == "alice" else world.bob
    resume = world.alice_resume if who == "alice" else world.bob_resume
    return world.client.post(
        SESSIONS, headers={**headers, "Idempotency-Key": key}, json=body(resume, **over)
    )


def test_a_retry_with_the_same_key_returns_the_same_session(world: World) -> None:
    first = create_with_key(world, "retry-key-0001")
    second = create_with_key(world, "retry-key-0001")
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert world.run_sql("SELECT count(*) AS n FROM sessions")[0]["n"] == 1


def test_different_keys_make_different_sessions_and_no_key_never_dedupes(world: World) -> None:
    create_with_key(world, "retry-key-0001")
    create_with_key(world, "retry-key-0002")
    create(world.client, world.alice, body(world.alice_resume))
    create(world.client, world.alice, body(world.alice_resume))
    assert world.run_sql("SELECT count(*) AS n FROM sessions")[0]["n"] == 4


def test_the_same_key_from_another_user_is_a_separate_request(world: World) -> None:
    a = create_with_key(world, "shared-key-0001", "alice")
    b = create_with_key(world, "shared-key-0001", "bob")
    assert b.status_code == 201 and a.json()["id"] != b.json()["id"]
    assert b.json()["resume"]["id"] == world.bob_resume


def test_a_request_still_in_progress_is_not_duplicated(world: World) -> None:
    uid = uuid.UUID(world.user_id("alice@example.com"))
    from app.sessions.idempotency import PENDING, _redis_key

    world.redis.data[_redis_key(uid, "pending-key-0001")] = (PENDING, 1e18)
    response = create_with_key(world, "pending-key-0001")
    assert response.status_code == 409 and response.json()["error"]["code"] == "request_in_progress"
    assert world.run_sql("SELECT count(*) AS n FROM sessions")[0]["n"] == 0


def test_a_failed_create_releases_the_key_for_a_corrected_retry(world: World) -> None:
    bad = create_with_key(world, "fix-key-00001", job_description="   ")
    assert bad.status_code == 422
    good = create_with_key(world, "fix-key-00001")
    assert good.status_code == 201


def test_a_deleted_session_does_not_resurrect_on_retry(world: World) -> None:
    first = create_with_key(world, "del-key-000001")
    world.client.delete(f"{SESSIONS}/{first.json()['id']}", headers=world.alice)
    second = create_with_key(world, "del-key-000001")
    assert second.status_code == 201 and second.json()["id"] != first.json()["id"]


def test_a_malformed_key_is_rejected_and_an_outage_does_not_block_creation(
    world: World,
) -> None:
    assert create_with_key(world, "short").status_code == 422
    world.redis.fail = True
    response = create_with_key(world, "outage-key-0001")
    assert response.status_code == 201  # unprotected, but the user is not blocked


# ------------------------------------------------------------------ export


def test_markdown_export_is_documented_complete_and_private(world: World) -> None:
    sid = world.make(title="Round 2", company_name="Acme")["id"]
    world.add_question(sid, "Describe a conflict.", answer="I listened first.")
    response = world.client.get(f"{SESSIONS}/{sid}/export", headers=world.alice)  # default
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition == f'attachment; filename="session-{sid[:8]}.md"'
    assert "Acme" not in disposition and "Round" not in disposition
    text = response.text
    assert "## What this file contains" in text and "## Not included" in text
    assert "Company: Acme" in text and "Resume: cv.pdf" in text
    assert "Describe a conflict." in text and "I listened first." in text


def test_pdf_export_is_a_real_pdf_with_the_session_content_and_no_secrets(world: World) -> None:
    import pymupdf

    sid = world.make(title="Round 2", company_name="Acme")["id"]
    world.add_question(sid, "Describe a conflict.", answer="I listened first.\n    indented line")
    response = world.client.get(f"{SESSIONS}/{sid}/export?format=pdf", headers=world.alice)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition == f'attachment; filename="session-{sid[:8]}.pdf"'
    assert "Acme" not in disposition and "Round" not in disposition
    assert response.content.startswith(b"%PDF")
    document = pymupdf.open(stream=response.content, filetype="pdf")
    # The PDF font typesets "fi"/"fl" as ligatures; NFKC turns them back for comparison.
    text = unicodedata.normalize(
        "NFKC", " ".join(document[i].get_text() for i in range(document.page_count))
    )
    assert "Describe a conflict." in text and "I listened first." in text
    assert "Not included" in text and "Acme" in text
    uid = world.user_id("alice@example.com")
    for forbidden in ("alice@example.com", uid, "Bearer "):
        assert forbidden not in text, forbidden


def test_pdf_export_spans_pages_for_long_sessions(world: World) -> None:
    import pymupdf

    sid = world.make(title="Long")["id"]
    for index in range(40):
        world.add_question(sid, f"Question number {index}", answer="word " * 150)
    response = world.client.get(f"{SESSIONS}/{sid}/export?format=pdf", headers=world.alice)
    assert response.status_code == 200
    document = pymupdf.open(stream=response.content, filetype="pdf")
    assert document.page_count > 3
    assert "Question number 39" in " ".join(
        document[i].get_text() for i in range(document.page_count)
    )


def test_export_excludes_credentials_account_data_and_internal_fields(world: World) -> None:
    sid = world.make()["id"]
    world.add_question(sid)
    markdown = world.client.get(
        f"{SESSIONS}/{sid}/export?format=markdown", headers=world.alice
    ).text
    uid = world.user_id("alice@example.com")
    # The file's own "not included" list names these words, so check only the real content.
    assert "## Job description" in markdown
    content = markdown.split("## Job description", 1)[1]
    for forbidden in (
        "alice@example.com",
        uid,
        "Bearer ",
        "password",
        "api_key",
        "storage_key",
        "generation_config",
        "retrieved_chunk",
        "embedding",
    ):
        assert forbidden not in content, forbidden
    assert world.alice["Authorization"].split()[-1] not in markdown


def test_markdown_export_survives_code_fences_in_answers(world: World) -> None:
    sid = world.make(title="Fences")["id"]
    world.add_question(sid, "Write code", answer="```python\nprint(1)\n```\nDone")
    response = world.client.get(f"{SESSIONS}/{sid}/export?format=markdown", headers=world.alice)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"].endswith('.md"')
    assert "````text" in response.text  # the outer fence is longer than the inner one
    assert "## Not included" in response.text


def test_export_rejects_unknown_formats_and_foreign_sessions(world: World) -> None:
    sid = world.make()["id"]
    assert (
        world.client.get(f"{SESSIONS}/{sid}/export?format=json", headers=world.alice).status_code
        == 422
    )
    assert world.client.get(f"{SESSIONS}/{sid}/export", headers=world.bob).status_code == 404


def test_export_only_contains_the_requested_sessions_content(world: World) -> None:
    a, b = world.make(title="A")["id"], world.make(title="B")["id"]
    world.add_question(a, "Question for A only")
    world.add_question(b, "Question for B only")
    exported = world.client.get(f"{SESSIONS}/{a}/export", headers=world.alice).text
    assert "Question for A only" in exported and "Question for B only" not in exported
