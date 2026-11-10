"""Phase 8A schema: detected_questions / generated_answers constraints, cascades, indexes, chain."""

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.db.models import ANSWER_STATUSES, QUESTION_KINDS, QUESTION_SOURCES, QUESTION_STATUSES
from app.preferences.enums import QuestionType
from tests.conftest import RunSql
from tests.test_sessions_schema import World, columns

API_DIR = Path(__file__).resolve().parents[1]


class QaWorld(World):
    def question(self, session_id: str, **cols: Any) -> str:
        values: dict[str, Any] = {
            "session_id": session_id,
            "user_id": self.user,
            "raw_text": "What is your biggest strength?",
            **cols,
        }
        names = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        sql = f"INSERT INTO detected_questions ({names}) VALUES ({params}) RETURNING id"  # noqa: S608
        return str(self.run_sql(sql, **values)[0]["id"])

    def answer(self, question_id: str, session_id: str, **cols: Any) -> str:
        values: dict[str, Any] = {
            "question_id": question_id,
            "session_id": session_id,
            "user_id": self.user,
            **cols,
        }
        names = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        sql = (
            f"INSERT INTO generated_answers ({names}, generation_config, prompt_version) "  # noqa: S608
            f"VALUES ({params}, '{{}}'::jsonb, 'v1') RETURNING id"
        )
        return str(self.run_sql(sql, **values)[0]["id"])


@pytest.fixture
def world(client: TestClient, run_sql: RunSql) -> QaWorld:
    return QaWorld(client, run_sql)


# ----------------------------------------------------------------- structure


def test_tables_have_exactly_the_typed_qa_columns(world: QaWorld) -> None:
    assert columns(world.run_sql, "detected_questions") == {
        "id", "session_id", "user_id", "source", "raw_text", "kind", "question_type", "status",
        "detected_at",
    }  # fmt: skip
    assert columns(world.run_sql, "generated_answers") == {
        "id", "question_id", "session_id", "user_id", "text", "generation_config", "prompt_version",
        "provider", "model", "retrieved_chunk_ids", "input_tokens", "output_tokens", "retrieval_ms",
        "first_token_ms", "total_ms", "fallback_used", "status", "error_code", "created_at",
        "grounding_check",
    }  # fmt: skip


def test_audio_and_screenshot_columns_are_not_created_yet(world: QaWorld) -> None:
    questions = columns(world.run_sql, "detected_questions")
    assert not questions & {"segment_ids", "screenshot_id", "confidence", "standalone_text"}
    assert "detection_latency_ms" not in questions


def test_expected_indexes_exist(world: QaWorld) -> None:
    rows = world.run_sql(
        "SELECT indexname FROM pg_indexes "
        "WHERE tablename IN ('detected_questions', 'generated_answers')"
    )
    assert {r["indexname"] for r in rows} >= {
        "ix_detected_questions_session_id_detected_at",
        "ix_generated_answers_question_id",
        "ix_generated_answers_session_id_created_at",
        "pk_detected_questions",
        "pk_generated_answers",
    }


def test_migration_chain_has_a_single_head_at_0009() -> None:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0009"]
    for rev, parent in (("0009", "0008"), ("0008", "0007"), ("0007", "0006")):
        revision = script.get_revision(rev)
        assert revision is not None and revision.down_revision == parent


# ---------------------------------------------------------------- constraints


def test_defaults_describe_a_typed_explicit_unanswered_question(world: QaWorld) -> None:
    sid = world.session()
    qid = world.question(sid)
    row = world.run_sql("SELECT * FROM detected_questions WHERE id = :i", i=qid)[0]
    assert (row["source"], row["kind"], row["status"], row["question_type"]) == (
        "typed", "explicit", "detected", None,
    )  # fmt: skip
    answer = world.run_sql(
        "SELECT * FROM generated_answers WHERE id = :i", i=world.answer(qid, sid)
    )[0]
    assert (answer["status"], answer["fallback_used"], answer["text"]) == ("streaming", False, "")
    assert answer["retrieved_chunk_ids"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"source": "radio"},
        {"kind": "rhetorical"},
        {"status": "deleted"},
        {"question_type": "trivia"},
        {"raw_text": ""},
    ],
)
def test_question_check_constraints(world: QaWorld, bad: dict[str, Any]) -> None:
    sid = world.session()
    with pytest.raises(IntegrityError):
        world.question(sid, **bad)
    assert world.n("detected_questions") == 0


def test_valid_question_domains_are_accepted(world: QaWorld) -> None:
    sid = world.session()
    for source in QUESTION_SOURCES:
        world.question(sid, source=source)
    for kind in QUESTION_KINDS:
        world.question(sid, kind=kind)
    for status in QUESTION_STATUSES:
        world.question(sid, status=status)
    for question_type in QuestionType:
        world.question(sid, question_type=question_type.value)
    assert world.n("detected_questions") == (
        len(QUESTION_SOURCES) + len(QUESTION_KINDS) + len(QUESTION_STATUSES) + len(QuestionType)
    )


def test_database_question_types_match_the_preference_enum_exactly(world: QaWorld) -> None:
    rows = world.run_sql(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conrelid = 'detected_questions'::regclass AND contype = 'c'"
    )
    definition = next(r["d"] for r in rows if "question_type" in r["d"])
    values = set(re.findall(r"'(\w+)'::character varying", definition))
    assert values == {m.value for m in QuestionType}


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "done"},
        {"retrieval_ms": -1},
        {"first_token_ms": -1},
        {"total_ms": -1},
        {"input_tokens": -1},
        {"output_tokens": -1},
    ],
)
def test_answer_check_constraints(world: QaWorld, bad: dict[str, Any]) -> None:
    sid = world.session()
    qid = world.question(sid)
    with pytest.raises(IntegrityError):
        world.answer(qid, sid, **bad)
    assert world.n("generated_answers") == 0


def test_valid_answer_statuses_and_zero_metrics_are_accepted(world: QaWorld) -> None:
    sid = world.session()
    qid = world.question(sid)
    for status in ANSWER_STATUSES:
        world.answer(qid, sid, status=status)
    world.answer(qid, sid, retrieval_ms=0, first_token_ms=0, total_ms=0, input_tokens=0)
    assert world.n("generated_answers") == len(ANSWER_STATUSES) + 1


def test_grounding_check_is_nullable_and_null_by_default(world: QaWorld) -> None:
    sid = world.session()
    qid = world.question(sid)
    aid = world.answer(qid, sid)
    row = world.run_sql(
        "SELECT grounding_check IS NULL AS is_null FROM generated_answers WHERE id = :i", i=aid
    )
    assert row[0]["is_null"] is True  # SQL NULL: never checked (not JSON null)


def test_grounding_check_accepts_a_json_object_and_rejects_other_json(world: QaWorld) -> None:
    sid = world.session()
    qid = world.question(sid)
    aid = world.answer(qid, sid)
    update = "UPDATE generated_answers SET grounding_check = CAST(:g AS jsonb) WHERE id = :i"
    world.run_sql(update, g='{"v": 1, "status": "ok"}', i=aid)
    stored = world.run_sql("SELECT grounding_check FROM generated_answers WHERE id = :i", i=aid)
    assert stored[0]["grounding_check"] == {"v": 1, "status": "ok"}
    for bad in ("[1, 2]", '"ok"', "5", "null"):
        with pytest.raises(IntegrityError):
            world.run_sql(update, g=bad, i=aid)


def test_chunk_ids_are_stored_as_an_array_without_foreign_keys(world: QaWorld) -> None:
    sid = world.session()
    qid = world.question(sid)
    ghost = str(uuid.uuid4())  # no such chunk: chunks are replaced when a file is re-indexed
    aid = world.answer(qid, sid, retrieved_chunk_ids=[uuid.UUID(ghost)])
    stored = world.run_sql("SELECT retrieved_chunk_ids FROM generated_answers WHERE id = :i", i=aid)
    assert [str(c) for c in stored[0]["retrieved_chunk_ids"]] == [ghost]


def test_foreign_keys_reject_dangling_references(world: QaWorld) -> None:
    sid = world.session()
    with pytest.raises(IntegrityError):
        world.question(str(uuid.uuid4()))
    qid = world.question(sid)
    with pytest.raises(IntegrityError):
        world.answer(str(uuid.uuid4()), sid)
    with pytest.raises(IntegrityError):
        world.answer(qid, str(uuid.uuid4()))


# ------------------------------------------------------------------- cascades


def test_deleting_a_session_removes_its_questions_and_answers_but_not_files(
    world: QaWorld,
) -> None:
    sid = world.session()
    world.preferences(sid)
    qid = world.question(sid)
    world.answer(qid, sid)
    world.run_sql("DELETE FROM sessions WHERE id = :i", i=sid)
    assert (world.n("detected_questions"), world.n("generated_answers")) == (0, 0)
    assert world.n("resumes") == 1 and world.n("documents") == 1  # the user's files survive


def test_deleting_a_question_removes_its_answers(world: QaWorld) -> None:
    sid = world.session()
    qid = world.question(sid)
    world.answer(qid, sid)
    world.run_sql("DELETE FROM detected_questions WHERE id = :i", i=qid)
    assert world.n("generated_answers") == 0 and world.n("sessions") == 1


def test_deleting_a_user_removes_all_their_qa_data(world: QaWorld) -> None:
    sid = world.session()
    world.answer(world.question(sid), sid)
    world.run_sql("DELETE FROM users WHERE id = :i", i=world.user)
    assert (world.n("detected_questions"), world.n("generated_answers")) == (0, 0)


def test_deleting_the_resume_keeps_questions_and_answers(world: QaWorld) -> None:
    sid = world.session()
    world.answer(world.question(sid), sid)
    world.run_sql("DELETE FROM resumes WHERE id = :i", i=world.resume)
    assert (world.n("detected_questions"), world.n("generated_answers")) == (1, 1)
