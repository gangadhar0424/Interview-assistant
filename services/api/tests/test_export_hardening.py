"""SEC-15 and SEC-19: the export does not block the event loop; metadata cannot inject Markdown."""

import asyncio
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.sessions import export as session_export
from tests.conftest import RunSql
from tests.helpers import register
from tests.test_sessions import body, create, new_file


def _data(**session: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": None,
        "company_name": "Acme",
        "job_url": None,
        "state": "active",
        "created_at": "2026-11-23T00:00:00Z",
        "resume_filename": "resume.pdf",
        "document_filenames": [],
        "job_description": "text",
        "preferences": {
            "language": "en",
            "question_type": "auto",
            "answer_format": "bullets",
            "answer_length": "short",
            "answer_tone": "simple",
        },
    }
    base.update(session)
    return {
        "session": base,
        "exported_at": "2026-11-23T00:00:00Z",
        "contents": ["x"],
        "excluded": ["y"],
        "questions": [],
        "truncated": False,
    }


# ---- SEC-19 -------------------------------------------------------------------------------------


def test_a_newline_in_a_header_field_cannot_start_a_heading_or_list_item() -> None:
    text = session_export.render_markdown(_data(company_name="Acme\n# Injected heading\n- item"))
    assert "\n# Injected" not in text
    assert "\n- item" not in text
    assert text.splitlines()[0].startswith("# ")
    assert "Company: Acme" in text


def test_markdown_and_html_punctuation_in_names_is_escaped() -> None:
    bs = chr(92)  # a backslash, without writing one in the source
    hostile = "[click](https://evil.example)<script>alert(1)</script>*bold*_it_`code`|"
    text = session_export.render_markdown(
        _data(title=hostile, resume_filename=hostile, document_filenames=[hostile])
    )
    header = chr(10).join(text.splitlines()[:12])
    # Every special character is preceded by a backslash, so none can start Markdown or HTML.
    assert f"{bs}[click{bs}]" in header and f"{bs}<script{bs}>" in header
    assert f"{bs}*bold{bs}*" in header and f"{bs}`code{bs}`" in header
    unescaped = header
    for special in "[]<>*_`|#":
        unescaped = unescaped.replace(bs + special, "")
    assert not any(ch in unescaped for ch in "[]<>*`|"), unescaped


def test_the_job_link_is_shown_literally_in_a_code_span() -> None:
    text = session_export.render_markdown(_data(job_url="https://x.example/a`b\n- injected"))
    line = next(line for line in text.splitlines() if line.startswith("- Job link:"))
    assert line == "- Job link: `https://x.example/a'b - injected`"
    assert "\n- injected" not in text


def test_ordinary_names_are_unchanged() -> None:
    data = _data(company_name="AT&T Labs, Inc.", title="Interview 1")
    text = session_export.render_markdown(data)
    assert text.startswith("# Interview 1\n")
    assert "- Company: AT&T Labs, Inc." in text


# ---- SEC-15 -------------------------------------------------------------------------------------


def test_pdf_rendering_is_serialised(monkeypatch: pytest.MonkeyPatch) -> None:
    running = 0
    overlap = False
    guard = threading.Lock()

    def fake(_data: dict[str, Any]) -> bytes:
        nonlocal running, overlap
        with guard:
            running += 1
            overlap = overlap or running > 1
        time.sleep(0.05)
        with guard:
            running -= 1
        return b"%PDF"

    monkeypatch.setattr(session_export, "_render_pdf_unlocked", fake)

    async def go() -> None:
        await asyncio.gather(*[asyncio.to_thread(session_export.render_pdf, {}) for _ in range(4)])

    asyncio.run(go())
    assert overlap is False


def test_the_pdf_endpoint_renders_off_the_event_loop(
    client: TestClient, run_sql: RunSql, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = register(client, "export-loop@example.com")
    resume = new_file(client, run_sql, headers, "resumes")
    created = create(client, headers, body(resume, []))
    assert created.status_code == 201, created.text
    seen: list[bool] = []

    def spy(_data: dict[str, Any]) -> bytes:
        try:
            asyncio.get_running_loop()
            seen.append(True)  # on the event loop: blocks every other request
        except RuntimeError:
            seen.append(False)  # in a worker thread
        return b"%PDF-1.7 spy"

    monkeypatch.setattr(session_export, "render_pdf", spy)
    response = client.get(
        f"/api/v1/sessions/{created.json()['id']}/export?format=pdf", headers=headers
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert seen == [False]
