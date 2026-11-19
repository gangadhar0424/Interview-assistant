"""User-triggered session export (M-9): a documented JSON or Markdown file.

Exactly what is exported is declared in the file itself (`contents` / `excluded`) and in
docs/SESSION_MANAGEMENT.md. Only what the API already stores for the session is exported
(details, the user's own job description, preferences, typed/captured questions and answers).
Never exported: resume or document text, prompts, retrieved passages, embeddings, tokens, API
keys, e-mail or other account data. Audio, transcripts and screenshots are not stored, so they
cannot be exported. Nothing is written to disk or sent anywhere by the server.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DetectedQuestion, GeneratedAnswer
from app.sessions import repository
from app.sessions.service import get_session

FORMAT_NAME = "interview-assistant.session-export"
FORMAT_VERSION = 1
MAX_QUESTIONS = 2000

CONTENTS = [
    "session details: title, company, job link, job description, status, dates",
    "names (not text) of the resume and supporting documents",
    "answer preferences: language, question type, format, length, tone, extra instructions",
    "selected answer and voice provider and model names",
    "questions and the answers generated for them, with their status and time",
]
EXCLUDED = [
    "resume and document text",
    "audio, transcripts and screenshots (never stored by the service)",
    "prompts, retrieved passages and embeddings",
    "passwords, tokens, API keys and other credentials",
    "account details such as your e-mail address",
]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _answer(answer: GeneratedAnswer) -> dict[str, Any]:
    return {
        "status": answer.status,
        "text": answer.text,
        "provider": answer.provider,
        "model": answer.model,
        "source_kind": answer.source_kind,
        "created_at": _iso(answer.created_at),
    }


def _question(question: DetectedQuestion, answers: list[GeneratedAnswer]) -> dict[str, Any]:
    return {
        "asked_at": _iso(question.detected_at),
        "source": question.source,
        "question_type": question.question_type,
        "text": question.raw_text,
        "answers": [_answer(a) for a in answers],
    }


async def build_export(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> dict[str, Any]:
    # get_session is user-scoped: an unknown or foreign id is the same 404 session_not_found.
    detail = await get_session(db, user_id, session_id)
    questions, answers = await repository.questions_with_answers_for_user(
        db, user_id, session_id, limit=MAX_QUESTIONS + 1
    )
    truncated = len(questions) > MAX_QUESTIONS
    questions = questions[:MAX_QUESTIONS]
    p = detail.preferences
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "contents": CONTENTS,
        "excluded": EXCLUDED,
        "session": {
            "title": detail.title,
            "company_name": detail.company_name,
            "job_url": detail.job_url,
            "job_description": detail.job_description,
            "status": detail.status,
            "state": detail.activity_state,
            "created_at": _iso(detail.created_at),
            "last_activity_at": _iso(detail.last_activity_at),
            "resume_filename": detail.resume.filename,
            "document_filenames": [d.filename for d in detail.documents],
            "preferences": {
                "language": p.language,
                "question_type": p.question_type,
                "answer_format": p.answer_format,
                "answer_length": p.answer_length,
                "answer_tone": p.answer_tone,
                "extra_instructions": p.extra_instructions,
                "provider": p.provider,
                "model": p.model,
                "stt_provider": p.stt_provider,
                "stt_model": p.stt_model,
            },
        },
        "questions": [_question(q, answers.get(q.id, [])) for q in questions],
        "truncated": truncated,
    }


def _fence(text: str) -> str:
    """A code fence longer than any backtick run inside the text."""
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def render_markdown(data: dict[str, Any]) -> str:
    s = data["session"]
    out = [f"# {s['title'] or s['company_name']}", ""]
    out.append(f"- Company: {s['company_name']}")
    if s["job_url"]:
        out.append(f"- Job link: {s['job_url']}")
    out += [
        f"- Status: {s['state']}",
        f"- Created: {s['created_at']}",
        f"- Exported: {data['exported_at']}",
        f"- Resume: {s['resume_filename'] or '(none)'}",
    ]
    if s["document_filenames"]:
        out.append("- Documents: " + ", ".join(s["document_filenames"]))
    pr = s["preferences"]
    out.append(
        f"- Preferences: {pr['language']}, {pr['question_type']}, {pr['answer_format']}, "
        f"{pr['answer_length']}, {pr['answer_tone']}"
    )
    out += ["", "## What this file contains", ""]
    out += [f"- {item}" for item in data["contents"]]
    out += ["", "## Not included", ""]
    out += [f"- {item}" for item in data["excluded"]]
    jd = s["job_description"]
    fence = _fence(jd)
    out += ["", "## Job description", "", f"{fence}text", jd, fence, "", "## Questions and answers"]
    if not data["questions"]:
        out += ["", "No questions were asked in this session."]
    for number, q in enumerate(data["questions"], start=1):
        out += ["", f"### Question {number}", ""]
        out += [f"> {line}" if line else ">" for line in str(q["text"]).splitlines() or [""]]
        out += ["", f"*{q['source']} · {q['asked_at']}*"]
        for answer in q["answers"]:
            out += ["", f"**Answer ({answer['status']}, {answer['provider'] or 'n/a'})**", ""]
            body = str(answer["text"]) or "(no text)"
            fence = _fence(body)
            out += [f"{fence}text", body, fence]
    if data["truncated"]:
        out += ["", f"_Only the first {MAX_QUESTIONS} questions are included._"]
    return "\n".join(out) + "\n"
