"""D-57 API behaviour: mandatory exclusive selections, PATCH rules, legacy sessions, isolation."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.ai.errors import Unavailable
from app.ai.fake_stt import FakeSTTProvider
from tests.conftest import RunSql
from tests.helpers import register
from tests.test_audio_api import install_stt, post
from tests.test_qa_service import quick_session
from tests.test_sessions import PREFS, SESSIONS, body, create, new_file
from tests.wav_helpers import make_wav


@pytest.fixture
def alice(client: TestClient) -> dict[str, str]:
    return register(client, "alice@example.com")


@pytest.fixture
def bob(client: TestClient) -> dict[str, str]:
    return register(client, "bob@example.com")


def new_body(client: TestClient, run_sql: RunSql, headers: dict[str, str], **prefs: Any) -> Any:
    resume = new_file(client, run_sql, headers, "resumes")
    return body(resume, [], prefs=prefs)


def prefs_without(*names: str) -> dict[str, Any]:
    return {k: v for k, v in PREFS.items() if k not in names}


def patch(client: TestClient, headers: dict[str, str], sid: str, **prefs: Any) -> Response:
    response: Response = client.patch(
        f"{SESSIONS}/{sid}", headers=headers, json={"preferences": prefs}
    )
    return response


def make(client: TestClient, run_sql: RunSql, headers: dict[str, str], **prefs: Any) -> str:
    response = create(client, headers, new_body(client, run_sql, headers, **prefs))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def make_legacy(client: TestClient, run_sql: RunSql, headers: dict[str, str]) -> str:
    """A session as created before D-57: no reasoning provider and no voice provider."""
    sid = make(client, run_sql, headers)
    run_sql(
        "UPDATE session_preferences SET provider = NULL, model = NULL, "
        "stt_provider = NULL, stt_model = NULL WHERE session_id = :s",
        s=sid,
    )
    return sid


# ------------------------------------------------------------------------ create


@pytest.mark.parametrize("missing", ["provider", "stt_provider"])
def test_both_selections_are_mandatory_for_new_sessions(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], missing: str
) -> None:
    resume = new_file(client, run_sql, alice, "resumes")
    payload = body(resume, [])
    payload["preferences"] = prefs_without(missing)
    response = create(client, alice, payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"][0]["field"].endswith(missing)


@pytest.mark.parametrize("value", [None, ""])
def test_a_blank_selection_is_not_a_selection(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], value: Any
) -> None:
    for field in ("provider", "stt_provider"):
        payload = new_body(client, run_sql, alice)
        payload["preferences"][field] = value
        assert create(client, alice, payload).status_code == 422


@pytest.mark.parametrize(
    "bad",
    [
        {"stt_provider": "nope"},
        {"stt_provider": "fake", "stt_model": "not-the-configured-model"},
        {"provider": "nope"},
        {"provider": "fake", "model": "not-the-configured-model"},
    ],
)
def test_selections_outside_the_allow_lists_are_rejected(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], bad: dict[str, str]
) -> None:
    response = create(client, alice, new_body(client, run_sql, alice, **bad))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_provider_selection"


def test_the_reasoning_provider_cannot_be_used_as_a_voice_provider(
    make_client: Any, run_sql: RunSql
) -> None:
    client = make_client(STT_ENABLED_PROVIDERS="")  # voice registry empty, reasoning has "fake"
    headers = register(client, "alice@example.com")
    response = create(client, headers, new_body(client, run_sql, headers))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_provider_selection"


def test_the_selections_are_stored_and_returned(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = make(client, run_sql, alice, model="fake-echo", stt_model="fake-stt")
    prefs = client.get(f"{SESSIONS}/{sid}", headers=alice).json()["preferences"]
    assert (prefs["provider"], prefs["model"]) == ("fake", "fake-echo")
    assert (prefs["stt_provider"], prefs["stt_model"]) == ("fake", "fake-stt")
    sid2 = make(client, run_sql, alice)
    prefs2 = client.get(f"{SESSIONS}/{sid2}", headers=alice).json()["preferences"]
    assert prefs2["model"] is None and prefs2["stt_model"] is None  # the provider's configured one


# ------------------------------------------------------------------------- patch


def test_providers_can_be_changed_but_not_cleared(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = make(client, run_sql, alice, model="fake-echo", stt_model="fake-stt")
    for field in ("provider", "stt_provider"):
        response = patch(client, alice, sid, **{field: None})
        assert response.status_code == 422, field
    ok = patch(client, alice, sid, stt_provider="fake")
    assert ok.status_code == 200, ok.text
    assert ok.json()["preferences"]["stt_provider"] == "fake"
    assert ok.json()["preferences"]["stt_model"] is None  # a provider change resets its model
    assert ok.json()["preferences"]["model"] == "fake-echo"  # the other role is untouched
    reset = patch(client, alice, sid, provider="fake")
    assert reset.json()["preferences"]["model"] is None


def test_patching_to_an_unavailable_provider_changes_nothing(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = make(client, run_sql, alice)
    for bad in ({"stt_provider": "nope"}, {"provider": "nope"}, {"stt_model": "other"}):
        response = patch(client, alice, sid, **bad)
        assert response.status_code == 422 and (
            response.json()["error"]["code"] == "invalid_provider_selection"
        ), bad
    prefs = client.get(f"{SESSIONS}/{sid}", headers=alice).json()["preferences"]
    assert (prefs["provider"], prefs["stt_provider"]) == ("fake", "fake")


def test_another_users_session_cannot_be_patched(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], bob: dict[str, str]
) -> None:
    sid = make(client, run_sql, alice)
    assert patch(client, bob, sid, stt_provider="fake").status_code == 404


# --------------------------------------------------------------- legacy sessions


def test_a_legacy_session_has_no_voice_provider_and_audio_is_refused(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = make_legacy(client, run_sql, alice)
    fakes, _, _ = install_stt(client)
    detail = client.get(f"{SESSIONS}/{sid}", headers=alice).json()["preferences"]
    assert detail["provider"] is None and detail["stt_provider"] is None
    response = post(client, alice, sid, make_wav())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stt_provider_required"
    assert fakes[0].calls == 0  # nothing was sent to any provider


def test_choosing_a_voice_provider_unlocks_audio_for_a_legacy_session(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = make_legacy(client, run_sql, alice)
    fakes, _, _ = install_stt(client, [FakeSTTProvider(reply="hello there")])
    assert patch(client, alice, sid, stt_provider="fake").status_code == 200
    response = post(client, alice, sid, make_wav())
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "fake" and fakes[0].calls == 1


def test_a_legacy_session_keeps_its_reasoning_chain_until_a_provider_is_chosen(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = make_legacy(client, run_sql, alice)
    row = run_sql("SELECT provider, model FROM session_preferences WHERE session_id = :s", s=sid)
    assert [(r["provider"], r["model"]) for r in row] == [(None, None)]  # nothing was backfilled
    assert patch(client, alice, sid, provider="fake").status_code == 200


# ------------------------------------------------------------ voice is exclusive


def selected_voice_session(
    client: TestClient, run_sql: RunSql, headers: dict[str, str], provider: str
) -> str:
    sid, _, _ = quick_session(client, run_sql, headers)
    run_sql(
        "UPDATE session_preferences SET stt_provider = :p WHERE session_id = :s", p=provider, s=sid
    )
    return sid


def test_the_sessions_own_voice_provider_is_used_and_nothing_else(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = selected_voice_session(client, run_sql, alice, "other")
    fakes, _, _ = install_stt(
        client,
        [FakeSTTProvider("fake", reply="from fake"), FakeSTTProvider("other", reply="from other")],
    )
    response = post(client, alice, sid, make_wav())
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "from other" and response.json()["provider"] == "other"
    assert [f.calls for f in fakes] == [0, 1]


def test_a_failing_voice_provider_is_an_error_not_a_fallback(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = selected_voice_session(client, run_sql, alice, "other")
    fakes, _, _ = install_stt(
        client,
        [
            FakeSTTProvider("fake", reply="from fake"),
            FakeSTTProvider("other", failures=[Unavailable()]),
        ],
    )
    response = post(client, alice, sid, make_wav())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"
    assert [f.calls for f in fakes] == [0, 1]  # the healthy provider was never asked


def test_a_voice_provider_that_is_no_longer_enabled_is_refused(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = selected_voice_session(client, run_sql, alice, "groq")  # not in this server's allow-list
    fakes, _, _ = install_stt(client)
    response = post(client, alice, sid, make_wav())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_provider_selection"
    assert fakes[0].calls == 0


def test_the_sessions_answer_language_is_passed_to_the_voice_provider(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    sid = make(client, run_sql, alice, language="te")
    fakes, _, _ = install_stt(client)
    assert post(client, alice, sid, make_wav()).status_code == 200
    assert fakes[0].languages_seen == ["te"]


def test_another_users_session_cannot_use_audio(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], bob: dict[str, str]
) -> None:
    sid = make(client, run_sql, alice)
    fakes, _, _ = install_stt(client)
    assert post(client, bob, sid, make_wav()).status_code == 404
    assert fakes[0].calls == 0
