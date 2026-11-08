"""Data access for sessions.

Isolation by construction (same rule as app/files/repository.py): every function requires
`user_id` and filters on it in SQL; there is deliberately no lookup by session id alone. Child
rows (preferences, document links) are only reached through a user-scoped session row.
"""

import uuid
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CallSession, SessionDocument, SessionPreferences


async def get_for_user(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, *, lock: bool = False
) -> CallSession | None:
    stmt = select(CallSession).where(CallSession.id == session_id, CallSession.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()  # serialise concurrent edits of the same session
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_for_user(session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
    """Hard delete. Preferences and document links cascade; resumes/documents are untouched."""
    result = await session.execute(
        delete(CallSession)
        .where(CallSession.id == session_id, CallSession.user_id == user_id)
        .returning(CallSession.id)
    )
    return result.first() is not None


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int,
    status: str | None = None,
    q: str | None = None,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> list[CallSession]:
    """Newest first with a stable (created_at, id) keyset; fetches `limit` rows."""
    stmt = select(CallSession).where(CallSession.user_id == user_id)
    if status is not None:
        stmt = stmt.where(CallSession.status == status)
    if q:
        pattern = f"%{_escape_like(q)}%"
        stmt = stmt.where(
            or_(
                CallSession.company_name.ilike(pattern, escape="\\"),
                CallSession.title.ilike(pattern, escape="\\"),
            )
        )
    if after is not None:
        created_at, row_id = after
        stmt = stmt.where(
            or_(
                CallSession.created_at < created_at,
                and_(CallSession.created_at == created_at, CallSession.id < row_id),
            )
        )
    stmt = stmt.order_by(CallSession.created_at.desc(), CallSession.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars())


async def preferences_for_user(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> SessionPreferences | None:
    stmt = (
        select(SessionPreferences)
        .join(CallSession, CallSession.id == SessionPreferences.session_id)
        .where(SessionPreferences.session_id == session_id, CallSession.user_id == user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def document_ids_for_user(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> set[uuid.UUID]:
    stmt = select(SessionDocument.document_id).where(
        SessionDocument.session_id == session_id, SessionDocument.user_id == user_id
    )
    return set((await session.execute(stmt)).scalars())


async def document_counts(
    session: AsyncSession, user_id: uuid.UUID, session_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not session_ids:
        return {}
    stmt = (
        select(SessionDocument.session_id, func.count())
        .where(SessionDocument.user_id == user_id, SessionDocument.session_id.in_(session_ids))
        .group_by(SessionDocument.session_id)
    )
    return {sid: count for sid, count in (await session.execute(stmt)).all()}


async def replace_documents(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, document_ids: list[uuid.UUID]
) -> None:
    await session.execute(
        delete(SessionDocument).where(
            SessionDocument.session_id == session_id, SessionDocument.user_id == user_id
        )
    )
    session.add_all(
        SessionDocument(session_id=session_id, document_id=doc_id, user_id=user_id)
        for doc_id in document_ids
    )
