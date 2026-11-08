"""Phase 7B schema: constraints, enum parity with the 7A enums, cascades, indexes, migration chain.

The test database is built from the Alembic migrations (tests/conftest.py), so every test here
also proves the migration applies cleanly. Upgrade/check/downgrade are additionally validated on a
scratch database (see the Phase 7B report).
"""

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.db.models import SESSION_STATUSES, SESSION_TYPES
from app.preferences.enums import AnswerFormat, AnswerLength, AnswerTone, Language, QuestionType
from tests.conftest import RunSql
from tests.helpers import make_pdf, register
from tests.test_files import upload

API_DIR = Path(__file__).resolve().parents[1]


class World:
    """One user with a resume and a document, plus raw-SQL helpers."""

    def __init__(self, client: TestClient, run_sql: RunSql) -> None:
        self.run_sql = run_sql
        headers = register(client, "alice@example.com")
        self.user = run_sql("SELECT id FROM users")[0]["id"]
        self.resume = str(upload(client, "resumes", headers, "cv.pdf", make_pdf(b"a")).json()["id"])
        self.doc = str(
            upload(client, "documents", headers, "n.txt", b"notes", "text/plain").json()["id"]
        )

    def session(self, **cols: Any) -> str:
        values: dict[str, Any] = {
            "user_id": self.user,
            "company_name": "Acme",
            "job_description": "jd",
            "resume_id": self.resume,
            **cols,
        }
        names = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        sql = (
            f"INSERT INTO sessions ({names}, resume_snapshot) "  # noqa: S608
            f"VALUES ({params}, '{{}}'::jsonb) RETURNING id"
        )
        return str(self.run_sql(sql, **values)[0]["id"])

    def preferences(self, session_id: str, **cols: Any) -> None:
        values: dict[str, Any] = {
            "session_id": session_id,
            "language": "en",
            "question_type": "auto",
            "answer_format": "bullets",
            "answer_length": "short",
            "answer_tone": "simple",
            **cols,
        }
        names = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        self.run_sql(
            f"INSERT INTO session_preferences ({names}, generation_config, prompt_version) "  # noqa: S608
            f"VALUES ({params}, '{{}}'::jsonb, 'v1')",
            **values,
        )

    def link(self, session_id: str) -> None:
        self.run_sql(
            "INSERT INTO session_documents (session_id, document_id, user_id) VALUES (:s, :d, :u)",
            s=session_id,
            d=self.doc,
            u=self.user,
        )

    def n(self, table: str) -> int:
        return int(self.run_sql(f"SELECT count(*) AS n FROM {table}")[0]["n"])  # noqa: S608


@pytest.fixture
def world(client: TestClient, run_sql: RunSql) -> World:
    return World(client, run_sql)


# ----------------------------------------------------------------- structure


def columns(run_sql: RunSql, table: str) -> set[str]:
    rows = run_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t", t=table
    )
    return {r["column_name"] for r in rows}


def test_tables_have_exactly_the_documented_columns(world: World) -> None:
    assert columns(world.run_sql, "sessions") == {
        "id", "user_id", "type", "title", "company_name", "job_url", "job_description",
        "resume_id", "resume_snapshot", "materials_snapshot", "status", "created_at", "updated_at",
    }  # fmt: skip
    assert columns(world.run_sql, "session_documents") == {"session_id", "document_id", "user_id"}
    assert columns(world.run_sql, "session_preferences") == {
        "session_id", "language", "provider", "model", "question_type", "answer_format",
        "answer_length", "answer_tone", "extra_instructions", "generation_config",
        "prompt_version", "created_at", "updated_at",
    }  # fmt: skip


def test_deferred_columns_are_absent(world: World) -> None:
    sessions = columns(world.run_sql, "sessions")
    # Hard delete (no tombstone), job import (7C) and start/end (later phase) are not in 7B.
    assert not sessions & {"deleted_at", "job_import_status", "started_at", "ended_at"}


def test_expected_indexes_exist(world: World) -> None:
    rows = world.run_sql("SELECT indexname FROM pg_indexes WHERE tablename LIKE 'session%'")
    names = {r["indexname"] for r in rows}
    assert {
        "ix_sessions_user_id_created_at_id",
        "ix_sessions_user_id_status",
        "ix_sessions_resume_id",
        "ix_session_documents_document_id",
        "pk_sessions",
        "pk_session_documents",
        "pk_session_preferences",
    } <= names


def test_migration_chain_is_linear_and_0006_follows_0005() -> None:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert (
        len(script.get_heads()) == 1
    )  # no branches (the current head is pinned in test_qa_schema)
    revision = script.get_revision("0006")
    assert revision is not None and revision.down_revision == "0005"


# ---------------------------------------------------------------- constraints


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "bogus"},
        {"type": "chat"},
        {"company_name": ""},
        {"company_name": "x" * 201},
        {"job_description": ""},
    ],
)
def test_sessions_check_constraints(world: World, bad: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        world.session(**bad)
    assert world.n("sessions") == 0


def test_valid_session_statuses_and_types_are_accepted(world: World) -> None:
    for status in SESSION_STATUSES:
        world.session(status=status)
    for kind in SESSION_TYPES:
        world.session(type=kind)
    assert world.n("sessions") == len(SESSION_STATUSES) + len(SESSION_TYPES)


@pytest.mark.parametrize(
    "bad",
    [
        {"language": "fr"},
        {"question_type": "trivia"},
        {"answer_format": "essay"},
        {"answer_length": "huge"},
        {"answer_tone": "rude"},
        {"model": "m1"},  # a model without a provider
    ],
)
def test_preference_check_constraints(world: World, bad: dict[str, Any]) -> None:
    sid = world.session()
    with pytest.raises(IntegrityError):
        world.preferences(sid, **bad)
    assert world.n("session_preferences") == 0


def test_every_valid_enum_value_is_accepted_by_the_database(world: World) -> None:
    sid = world.session()
    world.preferences(sid, provider="p", model="m")  # provider + model together is fine
    world.run_sql("DELETE FROM session_preferences")
    for column, enum in (
        ("language", Language),
        ("question_type", QuestionType),
        ("answer_format", AnswerFormat),
        ("answer_length", AnswerLength),
        ("answer_tone", AnswerTone),
    ):
        for member in enum:
            world.preferences(sid, **{column: member.value})
            world.run_sql("DELETE FROM session_preferences")


def test_database_check_values_match_the_preference_enums_exactly(world: World) -> None:
    """The migration's literal lists must stay in lock-step with app.preferences.enums."""
    rows = world.run_sql(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conrelid = 'session_preferences'::regclass AND contype = 'c'"
    )
    in_lists: dict[str, set[str]] = {}
    for row in rows:
        column = re.search(r"\((\w+)\)::text = ANY", row["d"])
        if column:  # `model IS NULL OR ...` has no value list and is skipped
            in_lists[column.group(1)] = set(re.findall(r"'(\w+)'::character varying", row["d"]))
    assert in_lists == {
        "language": {m.value for m in Language},
        "question_type": {m.value for m in QuestionType},
        "answer_format": {m.value for m in AnswerFormat},
        "answer_length": {m.value for m in AnswerLength},
        "answer_tone": {m.value for m in AnswerTone},
    }


def test_session_documents_primary_key_prevents_duplicate_links(world: World) -> None:
    sid = world.session()
    world.link(sid)
    with pytest.raises(IntegrityError):
        world.link(sid)


def test_foreign_keys_reject_dangling_references(world: World) -> None:
    with pytest.raises(IntegrityError):
        world.session(resume_id=str(uuid.uuid4()))
    sid = world.session()
    with pytest.raises(IntegrityError):
        world.run_sql(
            "INSERT INTO session_documents (session_id, document_id, user_id) VALUES (:s, :d, :u)",
            s=sid,
            d=str(uuid.uuid4()),
            u=world.user,
        )
    with pytest.raises(IntegrityError):
        world.run_sql(
            "INSERT INTO session_documents (session_id, document_id, user_id) VALUES (:s, :d, :u)",
            s=str(uuid.uuid4()),
            d=world.doc,
            u=world.user,
        )


# ------------------------------------------------------------------ cascades


def test_deleting_a_session_cascades_to_its_children_but_not_to_files(world: World) -> None:
    sid = world.session()
    world.preferences(sid)
    world.link(sid)
    world.run_sql("DELETE FROM sessions WHERE id = :i", i=sid)
    assert (world.n("session_preferences"), world.n("session_documents")) == (0, 0)
    assert world.n("resumes") == 1 and world.n("documents") == 1  # the user's files survive


def test_deleting_a_document_removes_only_its_link(world: World) -> None:
    sid = world.session()
    world.link(sid)
    world.run_sql("DELETE FROM documents WHERE id = :i", i=world.doc)
    assert world.n("session_documents") == 0 and world.n("sessions") == 1


def test_deleting_a_resume_clears_the_reference_and_keeps_the_session(world: World) -> None:
    sid = world.session()
    world.run_sql("DELETE FROM resumes WHERE id = :i", i=world.resume)
    row = world.run_sql("SELECT resume_id FROM sessions WHERE id = :i", i=sid)[0]
    assert row["resume_id"] is None and world.n("sessions") == 1


def test_deleting_a_user_removes_all_their_session_data(world: World) -> None:
    sid = world.session()
    world.preferences(sid)
    world.link(sid)
    world.run_sql("DELETE FROM users WHERE id = :i", i=world.user)
    assert [world.n(t) for t in ("sessions", "session_preferences", "session_documents")] == [
        0,
        0,
        0,
    ]
