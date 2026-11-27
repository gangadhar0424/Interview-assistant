"""SEC-33 regression: finishing an answer must not run CPU-bound work on the event loop.

Found by the realtime slow-client investigation: after a very large answer finished, the server ran
the citation-integrity check (a pure-Python scan of about a microsecond per character) on the event
loop itself, so one user's long answer delayed every other user's requests for as long as the scan
took (about 25 s for a 25 MiB answer). Real answers are capped by the provider's token limit, so the
delay is small in practice, but the structure was wrong. This test is deterministic: it does not
depend on timing or machine load, only on WHERE the check runs.
"""

import asyncio
import threading
from typing import Any

import pytest

from app.qa.service import QuestionAnswerer
from tests.conftest import RunSql
from tests.test_qa_service import (  # noqa: F401  (fixtures reused)
    RecordingProvider,
    Scenario,
    alice,
    answers,
    bob,
    env,
    make_router,
    scenario,
)

REPLY = "I led the billing migration. Evidence (not spoken): [E1]"


def _spy(record: dict[str, Any], original: Any) -> Any:
    def spy(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
            record["on_event_loop"] = True
        except RuntimeError:
            record["on_event_loop"] = False  # no loop in this thread: a worker thread
        record["thread"] = threading.current_thread().name
        record["calls"] = record.get("calls", 0) + 1
        return original(*args, **kwargs)

    return spy


def test_the_citation_check_runs_off_the_event_loop(
    scenario: Scenario, run_sql: RunSql, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    record: dict[str, Any] = {}
    original = QuestionAnswerer._citation_check
    monkeypatch.setattr(QuestionAnswerer, "_citation_check", staticmethod(_spy(record, original)))

    events = scenario.ask(make_router([RecordingProvider(reply_fn=lambda m: REPLY)]))

    assert events[-1].__class__.__name__ == "AnswerDone"
    assert record.get("calls") == 1, "the check should have run exactly once"
    assert record["on_event_loop"] is False, (
        f"the citation check ran on the event loop (thread {record['thread']!r}): "
        "it would delay every other user's requests"
    )


def test_moving_the_check_off_the_loop_changes_nothing_the_user_sees(
    scenario: Scenario, run_sql: RunSql  # noqa: F811
) -> None:
    events = scenario.ask(make_router([RecordingProvider(reply_fn=lambda m: REPLY)]))
    assert events[-1].__class__.__name__ == "AnswerDone"
    (answer,) = answers(run_sql)
    assert answer["status"] == "complete" and answer["text"] == REPLY  # stored unchanged
    check = answer["grounding_check"]
    assert check["v"] == 1 and check["valid_ids"] == [1] and check["unknown_ids"] == []
    assert REPLY not in repr(check)  # still no answer content in the stored check
