"""Operator-initiated account deletion (SEC-13 Decision 4, option 4d operator path, D-85).

For a support request. The operator must first verify the requester's identity by the documented
procedure (`docs/DATA_LIFECYCLE.md`, "Operator deletion"); this tool cannot do that. It runs the
same steps as the user path after the password check: database (cascade), files, cache purge,
ledger entry.

    uv run python -m app.lifecycle.operator_delete --user-id <uuid>            # shows what exists
    uv run python -m app.lifecycle.operator_delete --user-id <uuid> --confirm  # deletes

Uses DATABASE_URL (the runtime login, under the target user's tenant context), FILE_STORAGE_ROOT,
REDIS_URL and the ledger settings of the environment it runs in. Prints and logs the user id only.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence

import redis.asyncio as aioredis
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.models import User
from app.db.session import create_engine, create_sessionmaker, set_tenant_context
from app.lifecycle.account import remove_account
from app.lifecycle.recorder import DeletionRecorder, build_deletion_ledger
from app.qa.cache import AnswerCache
from app.storage.local import LocalFileStorage


async def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Operator account deletion (SEC-13)")
    parser.add_argument("--user-id", required=True, type=uuid.UUID)
    parser.add_argument("--confirm", action="store_true", help="delete (default: only check)")
    args = parser.parse_args(argv)
    user_id: uuid.UUID = args.user_id
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings.database_url, settings)
    redis = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
    )
    sessions = create_sessionmaker(engine)
    try:
        async with sessions() as session:
            await set_tenant_context(session, user_id)
            exists = (await session.execute(select(User.id).where(User.id == user_id))).first()
        if not args.confirm:
            print(f"user_id={user_id} exists={exists is not None} (dry run)")  # noqa: T201
            return 0
        recorder = DeletionRecorder(
            AnswerCache(redis, settings.qa_answer_cache_ttl_seconds),
            build_deletion_ledger(settings),
        )
        async with sessions() as session:
            try:
                removed = await remove_account(
                    session, LocalFileStorage(settings.storage_root), recorder, user_id
                )
            except AppError as exc:
                if exc.status_code == 404:
                    print(f"user_id={user_id} not found; nothing deleted")  # noqa: T201
                    return 1
                raise
        print(f"user_id={user_id} deleted files_removed={removed}")  # noqa: T201
        return 0
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
