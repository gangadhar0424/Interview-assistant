"""The answer `source` (D-66) is additive and identical over SSE, WebSocket and history."""

import json
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.qa.events import AnswerDone
from app.qa.source import MAX_REFS, REF_KEYS, source_payload, source_ref
from app.qa.sse import event_to_frame
from app.realtime import schemas as proto
from app.realtime.adapter import to_message

SOURCE = source_payload(
    "document",
    [source_ref("document", uuid.uuid4(), "interview-notes.pdf", "40. Enhancement in EWM")],
)
assert SOURCE is not None


def done(source: dict[str, Any] | None) -> AnswerDone:
    return AnswerDone(
        answer_id=uuid.uuid4(),
        question_id=uuid.uuid4(),
        provider="extractive",
        model="document",
        retrieval_ms=12,
        first_token_ms=14,
        total_ms=20,
        fallback_used=False,
        input_tokens=None,
        output_tokens=None,
        source=source,
    )


def sse_payload(event: AnswerDone) -> dict[str, Any]:
    frame = event_to_frame(event).decode()
    data = next(line for line in frame.splitlines() if line.startswith("data: "))
    payload: dict[str, Any] = json.loads(data.removeprefix("data: "))
    return payload


def test_the_sse_frame_carries_the_source_when_there_is_one() -> None:
    assert sse_payload(done(SOURCE))["source"] == SOURCE


def test_the_sse_frame_carries_a_null_source_for_an_answer_without_one() -> None:
    assert sse_payload(done(None))["source"] is None


def test_the_websocket_payload_matches_the_sse_payload() -> None:
    event = done(SOURCE)
    message = to_message(event, 5)
    ws = json.loads(message.model_dump_json())["payload"]
    assert ws["source"] == SOURCE
    sse = sse_payload(event)
    assert {k: v for k, v in ws.items() if k != "source"} == {
        k: v for k, v in sse.items() if k != "source"
    }
    assert json.loads(to_message(done(None), 6).model_dump_json())["payload"]["source"] is None


def test_the_protocol_rejects_an_unknown_source_kind_or_extra_fields() -> None:
    for bad in (
        {"kind": "invented", "refs": []},
        {"kind": "resume", "refs": [], "extra": 1},
        {"kind": "resume", "refs": [{"surprise": True}]},
    ):
        with pytest.raises(ValidationError):
            proto.AnswerSource.model_validate(bad)


def test_source_payload_is_none_without_a_kind_and_capped_otherwise() -> None:
    assert source_payload(None, [{"validated": True}]) is None
    refs = [source_ref("document", uuid.uuid4(), f"f{i}.pdf", None) for i in range(MAX_REFS + 3)]
    payload = source_payload("synthesis", refs)
    assert payload is not None and len(payload["refs"]) == MAX_REFS


def test_a_source_reference_holds_ids_names_and_headings_never_text() -> None:
    ref = source_ref("resume", uuid.uuid4(), "cv.pdf", "Technical Skills:")
    assert set(ref) == {"source_type", "source_id", "name", "heading"}  # unused keys: omitted
    full = source_payload("resume", [ref])
    assert full is not None and set(full["refs"][0]) == set(REF_KEYS)  # but one shape on the wire
    long_ref = source_ref("document", uuid.uuid4(), "n" * 1000, "h" * 1000)
    assert len(long_ref["name"]) == 255 and len(long_ref["heading"]) == 300
