"""Where an answer came from (D-66): the `source` shown next to it in the clients.

`kind` is `resume` or `document` (exact text from that file), `synthesis` (written by the model from
related passages), or `general` (general knowledge, validated). `refs` carry ids, file names and
headings only, never source text. Answers made before D-66, or with the router off, have no source.
"""

import uuid
from collections.abc import Sequence
from typing import Any

MAX_REFS = 5
REF_KEYS = ("source_type", "source_id", "name", "heading", "validated")


def source_ref(
    source_type: str,
    source_id: uuid.UUID,
    name: str | None,
    heading: str | None,
) -> dict[str, Any]:
    ref: dict[str, Any] = {"source_type": source_type, "source_id": str(source_id)}
    if name:
        ref["name"] = name[:255]
    if heading:
        ref["heading"] = heading[:300]
    return ref


def source_payload(
    kind: str | None, refs: Sequence[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """The API/event shape, or None when the answer has no recorded source."""
    if kind is None:
        return None
    # Every reference carries the same keys (None when unused), so the SSE, WebSocket and history
    # shapes are identical.
    full = [{key: ref.get(key) for key in REF_KEYS} for ref in list(refs or [])[:MAX_REFS]]
    return {"kind": kind, "refs": full}
