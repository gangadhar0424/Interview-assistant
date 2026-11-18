"""Adaptive question rewrite (D-66): used ONLY when the router is unsure.

A short, fast model call turns a vague question ("enhancement in ewm") into a search phrase and a
few keywords; the search then runs once more. Only the question text is sent to the provider, never
any document text. The call has a hard time cap: on timeout, error or an unreadable reply the
caller simply skips the second pass, so this step can never delay or break an answer by more than
the cap.
"""

import asyncio
import json
import uuid
from typing import Any

from app.ai.errors import LLMError
from app.ai.router import LLMRouter, Selection
from app.ai.types import GenerationConfig
from app.preferences.prompt_builder import build_rewrite_messages
from app.retrieval.terms import content_terms

MAX_KEYWORDS = 8


def parse_rewrite_reply(raw: str) -> list[str]:
    """Content words from the model's JSON reply (query + keywords); empty when unreadable."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data: Any = json.loads(raw[start : end + 1])
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    parts: list[str] = []
    query = data.get("query")
    if isinstance(query, str):
        parts.append(query)
    keywords = data.get("keywords")
    if isinstance(keywords, list):
        parts.extend(str(k) for k in keywords[:MAX_KEYWORDS] if isinstance(k, str | int))
    return content_terms(" ".join(parts))


async def rewrite_question(
    router: LLMRouter,
    selection: Selection | None,
    question: str,
    *,
    timeout_seconds: float,
    user_id: uuid.UUID,
) -> list[str]:
    """Search terms for a vague question, or [] when the step is skipped or fails."""
    try:
        async with asyncio.timeout(timeout_seconds):
            completion = await router.complete(
                build_rewrite_messages(question),
                GenerationConfig(temperature=0.0, max_output_tokens=120),
                selection=selection,
                user_id=user_id,
            )
    except (TimeoutError, LLMError):
        return []
    except Exception:  # noqa: BLE001 - an auxiliary step must never break the answer
        return []
    return parse_rewrite_reply(completion.text)
