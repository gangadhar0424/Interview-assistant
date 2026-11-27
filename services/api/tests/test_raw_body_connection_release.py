"""SEC-35 residual: the raw-body routes must not pin a pooled connection while the body is read.

The SEC-35 fix (authentication ends its transaction) covers `transcribe` and `screenshots` by
construction: their ready-session check uses its own short transaction that closes before the body
is read. Until now only the upload route had a regression test, and the Phase F live probe stalled
uploads only. These tests record how many pooled connections are checked out at the moment each
route starts reading its body. Any non-zero value means a slow client could pin a connection again.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.audio import router as audio_router
from app.screenshots import router as screenshots_router
from tests.conftest import RunSql
from tests.helpers import register
from tests.png_helpers import make_png
from tests.test_audio_api import install_stt
from tests.test_audio_api import post as post_audio
from tests.test_qa_service import quick_session
from tests.test_screenshot_api import install_vision
from tests.test_screenshot_api import post as post_screenshot
from tests.wav_helpers import make_wav


def _spy_on_body_read(
    monkeypatch: pytest.MonkeyPatch, module: Any, client: TestClient
) -> list[int]:
    """Wrap the module's `_read_capped` to record the pool's checked-out count when it is called."""
    engine = client.app.state.engine  # type: ignore[attr-defined]
    seen: list[int] = []
    real = module._read_capped

    async def spy(*args: Any, **kwargs: Any) -> bytes:
        seen.append(engine.sync_engine.pool.checkedout())
        result: bytes = await real(*args, **kwargs)
        return result

    monkeypatch.setattr(module, "_read_capped", spy)
    return seen


def test_no_database_connection_is_held_while_the_audio_body_is_read(
    client: TestClient, run_sql: RunSql, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = register(client, "alice@example.com")
    session_id, _, _ = quick_session(client, run_sql, alice)
    install_stt(client)
    seen = _spy_on_body_read(monkeypatch, audio_router, client)
    response = post_audio(client, alice, session_id, make_wav())
    assert response.status_code == 200, response.text
    assert seen == [0], f"connections checked out while reading the audio body: {seen}"


def test_no_database_connection_is_held_while_the_screenshot_body_is_read(
    client: TestClient, run_sql: RunSql, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = register(client, "alice@example.com")
    session_id, _, _ = quick_session(client, run_sql, alice)
    install_vision(client)
    seen = _spy_on_body_read(monkeypatch, screenshots_router, client)
    response = post_screenshot(client, alice, session_id, make_png())
    assert response.status_code == 200, response.text
    assert seen == [0], f"connections checked out while reading the screenshot body: {seen}"
