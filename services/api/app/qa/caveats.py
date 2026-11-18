"""Role guard (D-66): keep a model-written answer honest about what the candidate actually did.

Interview-preparation material often contains passages that LIMIT the candidate's role ("I have not
written the code myself; I gathered requirements and worked with the ABAP team", "do not claim
hands-on implementation you did not perform", "confirm before using"). A section answer that only
sees the section it was asked about ("Enhancement 1 ... I built a custom RF transaction") cannot
know about such a limit and could claim hands-on work the candidate disowns elsewhere.

So, whenever the model composes an answer from the user's material, the passages of the SAME
selected files that state such a limit are found and sent along as extra evidence, and the prompt
tells the model to obey them. Only the user's own selected files are searched (user-scoped SQL),
nothing is sent to any provider that was not already going into the prompt, and the ranking is a
pure function that favours passages about the same kind of work (code, development, building) or
the same topic. The patterns are English, which matches the product's supported language.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import set_tenant_context
from app.retrieval.service import ChunkHit
from app.retrieval.terms import coverage, token_set

# PostgreSQL advanced regular expression (`\m` / `\M` are word boundaries), matched case-blind.
CAVEAT_PATTERN = (
    r"\m(i|we) (have|had|did|do|was|am|were) not\M"
    r"|\m(i|we) (haven't|hadn't|didn't|wasn't|don't|never)\M"
    r"|\mnot (written|hands-on|responsible|involved in)\M"
    r"|\m(do not|don't|never) claim\M"
    r"|\mconfirm before using\M"
    r"|\mdo not invent\M"
    r"|\mhave not (written|worked|done|used|built)\M"
)

# Words that say a passage is about the kind of work a claim could be made about.
ROLE_WORDS = (
    "code",
    "coding",
    "coded",
    "develop",
    "implement",
    "built",
    "build",
    "write",
    "written",
    "wrote",
    "abap",
    "program",
    "configur",
    "design",
    "responsib",
)

CANDIDATES = 40  # caveat-looking chunks fetched before ranking
MAX_CAVEATS = 3  # passages added to a prompt
MIN_TOPIC_COVERAGE = 0.3
MIN_ROLE_WORDS = 2  # one stray role word ("Do not invent a manufacturing background") is noise

_SQL = text(
    "SELECT c.id, c.source_type, c.source_id, c.chunk_index, c.text, c.heading, "
    "       c.section_start, c.section_end "
    "FROM chunks c "
    "WHERE c.user_id = :uid AND c.source_id = ANY(:sids) AND c.text ~* :pattern "
    "ORDER BY c.source_id, c.chunk_index LIMIT :n"
).bindparams(bindparam("sids", type_=ARRAY(UUID(as_uuid=True))))


def role_word_count(passage: str) -> int:
    tokens = token_set(passage)
    count = sum(1 for word in ROLE_WORDS if any(token.startswith(word) for token in tokens))
    # "hands-on" is matched as a phrase: the stemmed word "hand" would also match "handheld".
    return count + (1 if "hands-on" in passage.lower() else 0)


def rank_caveats(
    hits: Sequence[ChunkHit],
    topic_terms: Sequence[str],
    *,
    exclude: Sequence[uuid.UUID] = (),
    limit: int = MAX_CAVEATS,
) -> list[ChunkHit]:
    """The most relevant role-limiting passages: about the same kind of work (role words) or the
    same topic. An unrelated one ("I have not worked on non-SAP tools") is left out."""
    skip = set(exclude)
    scored: list[tuple[float, ChunkHit]] = []
    for hit in hits:
        if hit.chunk_id in skip:
            continue
        passage = f"{hit.heading or ''} {hit.text}"
        roles = role_word_count(passage)
        topic = coverage(list(topic_terms), passage)
        if roles >= MIN_ROLE_WORDS or topic >= MIN_TOPIC_COVERAGE:
            scored.append((roles + 2 * topic, hit))
    scored.sort(key=lambda item: (-item[0], str(item[1].source_id), item[1].chunk_index))
    return [hit for _, hit in scored[:limit]]


async def find_caveats(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    source_ids: Sequence[uuid.UUID],
    topic_terms: Sequence[str],
    *,
    exclude: Sequence[uuid.UUID] = (),
) -> list[ChunkHit]:
    """Role-limiting passages of the user's selected files that matter for this topic."""
    if not source_ids:
        return []
    async with sessionmaker() as db:
        await set_tenant_context(db, user_id)
        rows = (
            await db.execute(
                _SQL,
                {
                    "uid": user_id,
                    "sids": list(source_ids),
                    "pattern": CAVEAT_PATTERN,
                    "n": CANDIDATES,
                },
            )
        ).all()
    hits = [
        ChunkHit(
            chunk_id=row.id,
            source_type=row.source_type,
            source_id=row.source_id,
            chunk_index=row.chunk_index,
            text=row.text,
            similarity=0.0,
            heading=row.heading,
            section_start=row.section_start,
            section_end=row.section_end,
        )
        for row in rows
    ]
    return rank_caveats(hits, topic_terms, exclude=exclude)
