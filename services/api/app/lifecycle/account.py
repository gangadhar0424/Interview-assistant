"""Account deletion (SEC-13 Decision 4: option 4d with 4a as the user path, D-85).

Immediate hard delete after the password and a typed phrase. Order, chosen so every step after the
database commit is retryable from the user id alone and a failure never leaves the account usable:

1. Check the typed phrase (cheap, before any password hashing).
2. Verify the password against the authenticated user's row.
3. In ONE transaction, as the runtime role with the user's tenant context, delete the user row.
   Every user-owned table cascades from `users` (12 tables, `docs/OWNER_DECISIONS_SEC13_SEC20.md`
   A.4.1), including refresh tokens, so every sign-in is revoked in the same commit. Access tokens
   stop working on the next request because authentication re-reads the user.
4. Remove `uploads/<user_id>/` (storage). A failure is logged and counted; the orphan sweep removes
   files whose owner no longer exists (`app.lifecycle.orphans`), and the ledger replay removes the
   tree again after a restore.
5. Purge the user's cached answers and append the ledger entry (`DeletionRecorder`).

Logs carry the user id only: never the email, the password or any content. The same function serves
the operator path (`python -m app.lifecycle.operator_delete`), which skips steps 1 and 2 only.
"""

import asyncio
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import security
from app.core.errors import AppError
from app.core.metrics import deletion_steps
from app.db.models import User
from app.db.session import set_tenant_context
from app.lifecycle.recorder import DeletionRecorder
from app.storage.base import FileStorage

logger = logging.getLogger(__name__)

CONFIRMATION_PHRASE = "DELETE MY ACCOUNT"


def confirmation_mismatch() -> AppError:
    return AppError(
        422,
        "confirmation_mismatch",
        f'Type "{CONFIRMATION_PHRASE}" exactly to confirm that the account should be deleted.',
    )


def password_incorrect() -> AppError:
    # 403, not 401: the caller IS authenticated, and clients treat 401 as an expired session.
    return AppError(403, "password_incorrect", "The password is not correct.")


async def remove_account(
    session: AsyncSession,
    storage: FileStorage,
    recorder: DeletionRecorder,
    user_id: uuid.UUID,
) -> int:
    """Steps 3 to 5. Returns how many stored files were removed. Raises AppError(404) when the
    account does not exist (already deleted), so a retried request is reported, not repeated."""
    await set_tenant_context(session, user_id)
    result = await session.execute(delete(User).where(User.id == user_id).returning(User.id))
    if result.scalar_one_or_none() is None:
        await session.rollback()
        raise AppError(404, "not_found", "The account does not exist.")
    await session.commit()
    logger.info("account deleted", extra={"user_id": str(user_id)})

    removed = 0
    try:
        removed = await asyncio.to_thread(storage.delete_user_tree, user_id)
        deletion_steps.labels(step="files", result="ok").inc()
    except Exception:  # noqa: BLE001 - the account is already gone; the orphan sweep retries
        deletion_steps.labels(step="files", result="failed").inc()
        logger.error("account files could not be removed", extra={"user_id": str(user_id)})
    await recorder.account_deleted(user_id)
    return removed


async def delete_own_account(
    session: AsyncSession,
    storage: FileStorage,
    recorder: DeletionRecorder,
    user_id: uuid.UUID,
    password: str,
    confirmation: str,
) -> None:
    if confirmation != CONFIRMATION_PHRASE:
        raise confirmation_mismatch()
    await set_tenant_context(session, user_id)
    password_hash = (
        await session.execute(select(User.password_hash).where(User.id == user_id))
    ).scalar_one_or_none()
    if password_hash is None:
        security.burn_password_check(password)
        raise AppError(404, "not_found", "The account does not exist.")
    if not security.verify_password(password_hash, password):
        raise password_incorrect()
    await remove_account(session, storage, recorder, user_id)
