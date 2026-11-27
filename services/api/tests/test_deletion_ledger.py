"""SEC-13 Decision 3-R2 (D-85): the deletion ledger holds anonymous ids only, detects altered or
forged lines, survives concurrent appends, and prunes by whole monthly files."""

import json
import os
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.lifecycle.ledger import ALLOWED_FIELDS, DeletionLedger, LedgerError

KEY = "k" * 16 + "0123456789abcdefghij"  # 36 characters
OWNER, SUBJECT = uuid.uuid4(), uuid.uuid4()


def ledger(tmp_path: Path, key: str = KEY) -> DeletionLedger:
    return DeletionLedger(tmp_path / "ledger", key)


def lines(tmp_path: Path) -> list[str]:
    out: list[str] = []
    for path in sorted((tmp_path / "ledger").glob("*.jsonl")):
        out += path.read_text(encoding="utf-8").splitlines()
    return out


def test_an_entry_holds_exactly_the_allowed_fields_and_nothing_personal(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    led.append("account", OWNER, OWNER)
    led.append("document", SUBJECT, OWNER)
    for line in lines(tmp_path):
        raw = json.loads(line)
        assert set(raw) == ALLOWED_FIELDS | {"mac"}
        assert ALLOWED_FIELDS == {
            "v",
            "entry_id",
            "kind",
            "subject_id",
            "owner_user_id",
            "deleted_at",
        }
        for name in ("entry_id", "subject_id", "owner_user_id"):
            uuid.UUID(raw[name])  # every id is a UUID, nothing else
        assert "@" not in line


def test_entries_round_trip_in_time_order(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    led.append("session", SUBJECT, OWNER, now=t0 + timedelta(hours=2))
    led.append("resume", OWNER, OWNER, now=t0)
    report = led.read()
    assert report.invalid_lines == 0
    assert [e.kind for e in report.entries] == ["resume", "session"]
    assert report.entries[1].subject_id == SUBJECT and report.entries[1].owner_user_id == OWNER


@pytest.mark.parametrize(
    "tamper",
    [
        lambda raw: {**raw, "subject_id": str(uuid.uuid4())},  # changed target
        lambda raw: {**raw, "kind": "account"},  # escalated kind
        lambda raw: {**raw, "mac": "0" * 64},  # forged mac
        lambda raw: {**raw, "email": "a@example.com"},  # extra field
        lambda raw: {k: v for k, v in raw.items() if k != "mac"},  # no mac
    ],
)
def test_an_altered_or_forged_line_is_never_returned(tmp_path: Path, tamper: object) -> None:
    led = ledger(tmp_path)
    led.append("session", SUBJECT, OWNER)
    path = next((tmp_path / "ledger").glob("*.jsonl"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(tamper(raw)) + "\n", encoding="utf-8")  # type: ignore[operator]
    report = led.read()
    assert report.entries == [] and report.invalid_lines == 1


def test_a_line_signed_with_another_key_is_rejected(tmp_path: Path) -> None:
    ledger(tmp_path, "x" * 40).append("session", SUBJECT, OWNER)
    report = ledger(tmp_path).read()
    assert report.entries == [] and report.invalid_lines == 1


def test_garbage_and_truncated_lines_are_counted_and_skipped(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    led.append("session", SUBJECT, OWNER)
    path = next((tmp_path / "ledger").glob("*.jsonl"))
    good = path.read_text(encoding="utf-8")
    path.write_text(good + "not json\n" + good[: len(good) // 2] + "\n", encoding="utf-8")
    report = led.read()
    assert len(report.entries) == 1 and report.invalid_lines == 2


def test_bad_arguments_are_refused(tmp_path: Path) -> None:
    with pytest.raises(LedgerError):
        DeletionLedger(tmp_path, "short")
    led = ledger(tmp_path)
    with pytest.raises(LedgerError):
        led.append("user", SUBJECT, OWNER)  # type: ignore[arg-type]
    with pytest.raises(LedgerError):
        led.append("session", "not-a-uuid", OWNER)  # type: ignore[arg-type]


def test_concurrent_appends_never_interleave_or_lose_a_line(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    other_process_view = ledger(tmp_path)  # a second instance, as another API process would be

    def write(target: DeletionLedger) -> None:
        for _ in range(50):
            target.append("session", uuid.uuid4(), OWNER)

    threads = [threading.Thread(target=write, args=(t,)) for t in (led, other_process_view) * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    report = led.read()
    assert len(report.entries) == 400 and report.invalid_lines == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_ledger_is_private_to_its_owner(tmp_path: Path) -> None:
    ledger(tmp_path).append("session", SUBJECT, OWNER)
    path = next((tmp_path / "ledger").glob("*.jsonl"))
    assert os.stat(path).st_mode & 0o077 == 0
    assert os.stat(tmp_path / "ledger").st_mode & 0o077 == 0


def test_prune_removes_only_whole_months_older_than_the_retention(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    led.append("session", SUBJECT, OWNER, now=datetime(2026, 9, 30, 23, 0, tzinfo=UTC))
    led.append("session", SUBJECT, OWNER, now=datetime(2026, 10, 15, tzinfo=UTC))
    # The September file can hold entries up to 1 October; 42 days later is 12 November.
    now = datetime(2026, 11, 12, 0, 0, 1, tzinfo=UTC)
    assert led.prune(42, now=now) == ["ledger-2026-09.jsonl"]  # dry run by default
    assert len(led.read().entries) == 2
    assert led.prune(42, now=now - timedelta(seconds=2), apply=True) == []  # one second early
    assert led.prune(42, now=now, apply=True) == ["ledger-2026-09.jsonl"]
    remaining = led.read().entries
    assert len(remaining) == 1 and remaining[0].deleted_at.month == 10


def test_prune_refuses_a_zero_retention(tmp_path: Path) -> None:
    with pytest.raises(LedgerError):
        ledger(tmp_path).prune(0)


def test_reading_a_missing_ledger_is_empty_not_an_error(tmp_path: Path) -> None:
    report = ledger(tmp_path).read()
    assert report.entries == [] and report.files == 0
