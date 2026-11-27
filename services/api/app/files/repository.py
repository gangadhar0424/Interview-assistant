"""Data access for user-owned files.

Isolation by construction: every function requires `user_id`, and there is deliberately no
function that reads or deletes a row without it. Callers pass the identity from `CurrentUser`.
"""

import uuid
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, Resume

FileModel = type[Resume] | type[Document]
FileRow = Resume | Document


async def list_for_user(
    session: AsyncSession, model: FileModel, user_id: uuid.UUID, limit: int
) -> list[FileRow]:
    stmt = (
        select(model)
        .where(model.user_id == user_id)
        .order_by(model.created_at.desc(), model.id)
        .limit(limit)
    )
    return cast(list[FileRow], list((await session.execute(stmt)).scalars()))


async def get_for_user(
    session: AsyncSession, model: FileModel, user_id: uuid.UUID, file_id: uuid.UUID
) -> FileRow | None:
    stmt = select(model).where(model.id == file_id, model.user_id == user_id)
    return cast(FileRow | None, (await session.execute(stmt)).scalar_one_or_none())


async def delete_row(session: AsyncSession, row: FileRow) -> None:
    await session.delete(row)


async def lock_user_uploads(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialise upload quota decisions for one user until the transaction ends (SEC-15).

    A transaction-scoped advisory lock keyed by the user: two concurrent uploads by the same user
    queue here, so each sees the other's committed row and the quota cannot be passed by racing.
    Other users are unaffected. Released by commit or rollback.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"upload-quota:{user_id}"},
    )


async def usage_for_user(
    session: AsyncSession, model: FileModel, user_id: uuid.UUID
) -> tuple[int, int]:
    """(files of this kind, bytes across resumes and documents) for one user."""
    count = (
        await session.execute(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
    ).scalar_one()
    total = 0
    for table in (Resume, Document):
        total += (
            await session.execute(
                select(func.coalesce(func.sum(table.size_bytes), 0)).where(table.user_id == user_id)
            )
        ).scalar_one()
    return int(count), int(total)
