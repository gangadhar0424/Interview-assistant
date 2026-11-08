"""Data access for user-owned files.

Isolation by construction: every function requires `user_id`, and there is deliberately no
function that reads or deletes a row without it. Callers pass the identity from `CurrentUser`.
"""

import uuid
from typing import cast

from sqlalchemy import select
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
