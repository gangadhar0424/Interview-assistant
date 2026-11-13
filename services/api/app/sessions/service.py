"""Session use cases. Ownership comes only from the authenticated user id passed in."""

import base64
import binascii
import json
import logging
import uuid
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.errors import InvalidSelection
from app.ai.registry import ProviderRegistry
from app.ai.stt_router import SttRouter
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import SESSION_STATUSES, CallSession, Document, Resume, SessionPreferences
from app.preferences.answer_config import CompiledPreferences
from app.preferences.compiler import PreferenceCompiler, PreferencesInput, parse_identifier
from app.preferences.errors import InvalidPreference
from app.preferences.prompt_builder import PROMPT_VERSION
from app.preferences.sanitize import clean_text
from app.sessions import repository
from app.sessions.materials import material_not_found, resolve_materials
from app.sessions.schemas import (
    DocumentRef,
    PreferencesOut,
    ResumeRef,
    SessionCreate,
    SessionDetail,
    SessionList,
    SessionSummary,
    SessionUpdate,
)

logger = logging.getLogger(__name__)

_LOCKED_STATUSES = ("ended", "archived")  # only the title may change (docs/API.md PATCH)
_PREFERENCE_FIELDS = (
    "language",
    "question_type",
    "answer_format",
    "answer_length",
    "answer_tone",
)


def session_not_found() -> AppError:
    return AppError(404, "session_not_found", "Session not found.")


def _invalid(message: str) -> AppError:
    return AppError(422, "validation_error", message)


# ------------------------------------------------------------------ field rules


def _clean_name(value: str, field: str) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        raise _invalid(f"{field} must not be empty.")
    if len(cleaned) > 200:
        raise _invalid(f"{field} must be at most 200 characters.")
    return cleaned


def _clean_title(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = clean_text(value)
    if len(cleaned) > 200:
        raise _invalid("title must be at most 200 characters.")
    return cleaned or None


def _clean_job_description(value: str, settings: Settings) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        raise _invalid("job_description must not be empty.")
    if len(cleaned) > settings.session_job_description_max_chars:
        raise _invalid(
            f"job_description must be at most {settings.session_job_description_max_chars} "
            "characters."
        )
    return cleaned


def _clean_job_url(value: str | None) -> str | None:
    """Stored as typed (never fetched in Phase 7B); only the shape is checked."""
    if value is None:
        return None
    cleaned = clean_text(value)
    if not cleaned:
        return None
    parts = urlsplit(cleaned)
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or any(c.isspace() for c in cleaned)
    ):
        raise _invalid("job_url must be a valid http or https URL.")
    return cleaned


def _check_provider_selection(
    registry: ProviderRegistry, provider: str | None, model: str | None
) -> None:
    """Validate an explicit provider/model against the server allowlist (no provider calls)."""
    if provider is None:
        return
    if registry.get(provider) is None:
        raise InvalidSelection()
    if model is not None:
        registry.resolve(provider, model, "streaming")  # raises InvalidSelection


def _check_voice_selection(
    voice: SttRouter, provider: str | None, model: str | None
) -> tuple[str | None, str | None]:
    """Validate the voice-listening selection (shape, then the server's voice allowlist)."""
    try:
        provider = parse_identifier(provider, "stt_provider")
        model = parse_identifier(model, "stt_model")
    except InvalidPreference as exc:
        raise _invalid(f"{exc.field} is invalid.") from None
    if provider is None:
        if model is not None:
            raise _invalid("stt_model requires an stt_provider.")
        return None, None
    voice.resolve(provider, model)  # raises InvalidSelection (422 invalid_provider_selection)
    return provider, model


def _compile(compiler: PreferenceCompiler, values: dict[str, str | None]) -> CompiledPreferences:
    try:
        prefs = PreferencesInput(
            language=str(values["language"]),
            question_type=str(values["question_type"]),
            answer_format=str(values["answer_format"]),
            answer_length=str(values["answer_length"]),
            answer_tone=str(values["answer_tone"]),
            extra_instructions=values["extra_instructions"],
            provider=values["provider"],
            model=values["model"],
        )
        return compiler.compile(prefs)
    except InvalidPreference as exc:
        if exc.field == "extra_instructions":
            raise _invalid(
                f"extra_instructions must be at most {compiler.max_extra_instructions} characters."
            ) from None
        if exc.field == "model":
            raise _invalid("model requires a provider and must be a valid identifier.") from None
        raise _invalid(f"{exc.field} is invalid.") from None


# ------------------------------------------------------------------- rendering


def _resume_ref(row: CallSession) -> ResumeRef:
    return ResumeRef(id=row.resume_id, filename=str(row.resume_snapshot.get("filename", "")))


def _summary(row: CallSession, document_count: int) -> SessionSummary:
    return SessionSummary(
        id=row.id,
        type=row.type,
        title=row.title,
        company_name=row.company_name,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        resume=_resume_ref(row),
        document_count=document_count,
    )


async def _detail(session: AsyncSession, user_id: uuid.UUID, row: CallSession) -> SessionDetail:
    prefs = await repository.preferences_for_user(session, user_id, row.id)
    if prefs is None:  # cannot happen through the API (created together); fail closed
        raise session_not_found()
    live = await repository.document_ids_for_user(session, user_id, row.id)
    documents = [
        DocumentRef(
            id=uuid.UUID(str(item["id"])),
            filename=str(item["filename"]),
            available=uuid.UUID(str(item["id"])) in live,
        )
        for item in row.materials_snapshot
    ]
    base = _summary(row, len(live))
    return SessionDetail(
        **base.model_dump(),
        job_url=row.job_url,
        job_description=row.job_description,
        documents=documents,
        preferences=PreferencesOut(
            language=prefs.language,
            provider=prefs.provider,
            model=prefs.model,
            stt_provider=prefs.stt_provider,
            stt_model=prefs.stt_model,
            question_type=prefs.question_type,
            answer_format=prefs.answer_format,
            answer_length=prefs.answer_length,
            answer_tone=prefs.answer_tone,
            extra_instructions=prefs.extra_instructions,
            prompt_version=prefs.prompt_version,
        ),
    )


def _resume_snapshot(resume: Resume) -> dict[str, object]:
    return {"id": str(resume.id), "filename": resume.original_filename}


def _materials_snapshot(documents: list[Document]) -> list[dict[str, object]]:
    return [{"id": str(d.id), "filename": d.original_filename} for d in documents]


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError:
        # A selected file was deleted between validation and commit.
        await session.rollback()
        raise material_not_found() from None


# --------------------------------------------------------------------- use cases


async def create_session(
    session: AsyncSession,
    user_id: uuid.UUID,
    settings: Settings,
    registry: ProviderRegistry,
    voice: SttRouter,
    data: SessionCreate,
) -> SessionDetail:
    company = _clean_name(data.company_name, "company_name")
    title = _clean_title(data.title)
    job_url = _clean_job_url(data.job_url)
    job_description = _clean_job_description(data.job_description, settings)

    compiler = PreferenceCompiler.from_settings(settings)
    p = data.preferences
    values: dict[str, str | None] = {
        "language": p.language.value,
        "question_type": p.question_type.value,
        "answer_format": p.answer_format.value,
        "answer_length": p.answer_length.value,
        "answer_tone": p.answer_tone.value,
        "extra_instructions": p.extra_instructions,
        "provider": p.provider,
        "model": p.model,
    }
    compiled = _compile(compiler, values)
    _check_provider_selection(registry, compiled.answer.provider, compiled.answer.model)
    stt_provider, stt_model = _check_voice_selection(voice, p.stt_provider, p.stt_model)

    materials = await resolve_materials(
        session,
        user_id,
        resume_id=data.resume_id,
        document_ids=data.document_ids,
        max_documents=settings.session_max_documents,
    )
    assert materials.resume is not None and materials.documents is not None  # noqa: S101

    row = CallSession(
        user_id=user_id,  # always the authenticated user, never a client value
        type=data.type,
        title=title,
        company_name=company,
        job_url=job_url,
        job_description=job_description,
        resume_id=materials.resume.id,
        resume_snapshot=_resume_snapshot(materials.resume),
        materials_snapshot=_materials_snapshot(materials.documents),
        status="ready",
    )
    session.add(row)
    await session.flush()
    session.add(
        SessionPreferences(
            session_id=row.id,
            language=compiled.answer.language.value,
            provider=compiled.answer.provider,
            model=compiled.answer.model,
            stt_provider=stt_provider,
            stt_model=stt_model,
            question_type=compiled.answer.question_type.value,
            answer_format=compiled.answer.answer_format.value,
            answer_length=compiled.answer.answer_length.value,
            answer_tone=compiled.answer.answer_tone.value,
            extra_instructions=compiled.answer.extra_instructions,
            generation_config=compiled.to_dict(),
            prompt_version=PROMPT_VERSION,
        )
    )
    await repository.replace_documents(
        session, user_id, row.id, [d.id for d in materials.documents]
    )
    await _commit(session)
    logger.info("session created", extra={"user_id": str(user_id), "session_id": str(row.id)})
    return await _detail(session, user_id, row)


def _encode_cursor(row: CallSession) -> str:
    raw = json.dumps({"c": row.created_at.isoformat(), "i": str(row.id)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(data["c"]), uuid.UUID(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise AppError(422, "invalid_cursor", "The pagination cursor is invalid.") from None


async def list_sessions(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int,
    status: str | None,
    q: str | None,
    cursor: str | None,
) -> SessionList:
    if status is not None and status not in SESSION_STATUSES:
        raise _invalid("status is invalid.")
    after = _decode_cursor(cursor) if cursor else None
    rows = await repository.list_for_user(
        session, user_id, limit=limit + 1, status=status, q=(q or "").strip() or None, after=after
    )
    page, more = rows[:limit], len(rows) > limit
    counts = await repository.document_counts(session, user_id, [r.id for r in page])
    return SessionList(
        items=[_summary(r, counts.get(r.id, 0)) for r in page],
        next_cursor=_encode_cursor(page[-1]) if more and page else None,
    )


async def get_session(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> SessionDetail:
    row = await repository.get_for_user(session, user_id, session_id)
    if row is None:
        raise session_not_found()  # foreign and unknown ids look identical
    return await _detail(session, user_id, row)


async def update_session(
    session: AsyncSession,
    user_id: uuid.UUID,
    settings: Settings,
    registry: ProviderRegistry,
    voice: SttRouter,
    session_id: uuid.UUID,
    data: SessionUpdate,
) -> SessionDetail:
    row = await repository.get_for_user(session, user_id, session_id, lock=True)
    if row is None:
        raise session_not_found()
    changed = data.model_fields_set
    if row.status in _LOCKED_STATUSES and changed - {"title"}:
        raise AppError(409, "session_not_editable", "Only the title can change after it ended.")
    for field in ("company_name", "job_description", "resume_id", "document_ids"):
        if field in changed and getattr(data, field) is None:
            raise _invalid(f"{field} cannot be null.")

    if "title" in changed:
        row.title = _clean_title(data.title)
    if data.company_name is not None:
        row.company_name = _clean_name(data.company_name, "company_name")
    if "job_url" in changed:
        row.job_url = _clean_job_url(data.job_url)
    if data.job_description is not None:
        row.job_description = _clean_job_description(data.job_description, settings)

    if data.resume_id is not None or data.document_ids is not None:
        materials = await resolve_materials(
            session,
            user_id,
            resume_id=data.resume_id,
            document_ids=data.document_ids,
            max_documents=settings.session_max_documents,
        )
        if materials.resume is not None:
            row.resume_id = materials.resume.id
            row.resume_snapshot = _resume_snapshot(materials.resume)
        if materials.documents is not None:
            row.materials_snapshot = _materials_snapshot(materials.documents)
            await repository.replace_documents(
                session, user_id, row.id, [d.id for d in materials.documents]
            )

    if data.preferences is not None and data.preferences.model_fields_set:
        await _update_preferences(session, user_id, settings, registry, voice, row, data)

    if changed:
        row.updated_at = func.now()  # also bumps on preferences/material-only edits
    await _commit(session)
    if changed:
        await session.refresh(row)  # reload the server-side timestamp for the response
    return await _detail(session, user_id, row)


async def _update_preferences(
    session: AsyncSession,
    user_id: uuid.UUID,
    settings: Settings,
    registry: ProviderRegistry,
    voice: SttRouter,
    row: CallSession,
    data: SessionUpdate,
) -> None:
    assert data.preferences is not None  # noqa: S101
    patch = data.preferences
    current = await repository.preferences_for_user(session, user_id, row.id)
    if current is None:
        raise session_not_found()
    values: dict[str, str | None] = {
        "language": current.language,
        "question_type": current.question_type,
        "answer_format": current.answer_format,
        "answer_length": current.answer_length,
        "answer_tone": current.answer_tone,
        "extra_instructions": current.extra_instructions,
        "provider": current.provider,
        "model": current.model,
    }
    for field in _PREFERENCE_FIELDS:
        if field in patch.model_fields_set:
            new = getattr(patch, field)
            if new is None:
                raise _invalid(f"{field} cannot be null.")
            values[field] = new.value
    for field in ("provider", "stt_provider"):
        if field in patch.model_fields_set and getattr(patch, field) is None:
            raise _invalid(f"{field} cannot be cleared.")  # mandatory selections (D-57)
    for field in ("extra_instructions", "provider", "model"):
        if field in patch.model_fields_set:
            values[field] = getattr(patch, field)
    if "provider" in patch.model_fields_set and "model" not in patch.model_fields_set:
        values["model"] = None  # the old model belongs to the previous provider

    compiled = _compile(PreferenceCompiler.from_settings(settings), values)
    if {"provider", "model"} & patch.model_fields_set:
        _check_provider_selection(registry, compiled.answer.provider, compiled.answer.model)

    if {"stt_provider", "stt_model"} & patch.model_fields_set:
        if "stt_provider" in patch.model_fields_set:
            new_provider = patch.stt_provider
            new_model = patch.stt_model if "stt_model" in patch.model_fields_set else None
        else:
            new_provider, new_model = current.stt_provider, patch.stt_model
        current.stt_provider, current.stt_model = _check_voice_selection(
            voice, new_provider, new_model
        )

    current.language = compiled.answer.language.value
    current.provider = compiled.answer.provider
    current.model = compiled.answer.model
    current.question_type = compiled.answer.question_type.value
    current.answer_format = compiled.answer.answer_format.value
    current.answer_length = compiled.answer.answer_length.value
    current.answer_tone = compiled.answer.answer_tone.value
    current.extra_instructions = compiled.answer.extra_instructions
    current.generation_config = compiled.to_dict()
    current.prompt_version = PROMPT_VERSION


async def delete_session(session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    """Hard delete. Never touches the user's resumes, documents, chunks or stored files."""
    if not await repository.delete_for_user(session, user_id, session_id):
        raise session_not_found()
    await session.commit()
    logger.info("session deleted", extra={"user_id": str(user_id), "session_id": str(session_id)})
