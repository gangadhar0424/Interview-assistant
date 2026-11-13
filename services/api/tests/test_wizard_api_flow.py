"""API-level tests for the Phase 7D wizard: the exact contract the web UI depends on.

The web app (apps/web) has no browser test tooling by owner decision; its logic is unit-tested with
Node and these tests pin the other side: the golden request payloads the frontend produces are
accepted by the real API, the response shapes it reads exist, its readiness rule equals the
server's, and every error code it maps is a real backend code.
"""

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import RunSql
from tests.helpers import register
from tests.job_server import LocalServer, local_server, page
from tests.test_job_import_api import JSON_LD_PAGE, install
from tests.test_sessions import SESSIONS, create, new_file, safe

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "apps" / "web" / "tests" / "fixtures"
WIZARD_TS = REPO / "apps" / "web" / "lib" / "wizard.ts"
API_APP = REPO / "services" / "api" / "app"


def golden(name: str, **ids: str) -> dict[str, Any]:
    """A payload exactly as the frontend test pins it, with real ids in place of placeholders."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    for placeholder, real in ids.items():
        text = text.replace(placeholder, real)
    data: dict[str, Any] = json.loads(text)
    return data


@pytest.fixture
def alice(client: TestClient) -> dict[str, str]:
    return register(client, "alice@example.com")


@pytest.fixture
def bob(client: TestClient) -> dict[str, str]:
    return register(client, "bob@example.com")


@pytest.fixture
def server() -> Iterator[LocalServer]:
    with local_server() as s:
        yield s


def ids(client: TestClient, run_sql: RunSql, headers: dict[str, str]) -> dict[str, str]:
    return {
        "RESUME_ID": new_file(client, run_sql, headers, "resumes"),
        "DOC_ID_1": new_file(client, run_sql, headers, "documents"),
        "DOC_ID_2": new_file(client, run_sql, headers, "documents"),
    }


# ------------------------------------------------- frontend payloads are accepted


def test_the_frontend_golden_payloads_are_accepted_by_the_api(
    make_client: Callable[..., TestClient], run_sql: RunSql
) -> None:
    client = make_client(LLM_ENABLED_PROVIDERS="fake")  # the provider golden names "fake"
    headers = register(client, "alice@example.com")
    real = ids(client, run_sql, headers)

    full = create(client, headers, golden("create_payload_full.json", **real))
    assert full.status_code == 201, full.text
    data = full.json()
    assert data["company_name"] == "Acme Corp" and data["title"] == "Backend Engineer"
    assert data["job_url"] == "https://jobs.example.com/42"
    assert [d["id"] for d in data["documents"]] == [real["DOC_ID_1"], real["DOC_ID_2"]]
    assert (
        data["preferences"]["language"] == "te" and data["preferences"]["answer_length"] == "long"
    )
    assert data["preferences"]["extra_instructions"] == "Mention my open-source work."

    minimal = create(client, headers, golden("create_payload_minimal.json", **real))
    assert minimal.status_code == 201, minimal.text
    assert minimal.json()["preferences"]["provider"] == "fake"  # D-57: mandatory, exclusive
    assert minimal.json()["preferences"]["stt_provider"] == "fake"

    provider = create(client, headers, golden("create_payload_provider.json", **real))
    assert provider.status_code == 201, provider.text
    assert provider.json()["preferences"]["provider"] == "fake"
    assert provider.json()["preferences"]["model"] is None  # the provider's configured model
    assert provider.json()["preferences"]["stt_model"] is None


def test_the_provider_golden_is_rejected_when_the_provider_is_not_enabled(
    make_client: Callable[..., TestClient], run_sql: RunSql
) -> None:
    client = make_client(LLM_ENABLED_PROVIDERS="", STT_ENABLED_PROVIDERS="")
    alice = register(client, "alice@example.com")
    payload = golden("create_payload_provider.json", **ids(client, run_sql, alice))
    response = create(client, alice, payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_provider_selection"


def test_golden_payloads_cannot_reach_another_users_files(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], bob: dict[str, str]
) -> None:
    alices = ids(client, run_sql, alice)
    stolen = create(client, bob, golden("create_payload_full.json", **alices))
    unknown = create(
        client,
        bob,
        golden(
            "create_payload_full.json",
            RESUME_ID="00000000-0000-4000-8000-000000000001",
            DOC_ID_1="00000000-0000-4000-8000-000000000002",
            DOC_ID_2="00000000-0000-4000-8000-000000000003",
        ),
    )
    assert stolen.status_code == 404 and safe(stolen) == safe(unknown)


# ---------------------------------------------------- the end-to-end wizard flow


def test_import_then_create_then_list_flow(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    bob: dict[str, str],
    server: LocalServer,
) -> None:
    server.routes["/job"] = page(JSON_LD_PAGE)
    install(client, server)
    resume = new_file(client, run_sql, alice, "resumes")

    imported = client.post(
        "/api/v1/sessions/job-import", headers=alice, json={"url": server.url("/job")}
    ).json()
    assert imported["status"] == "succeeded"

    payload = golden("create_payload_minimal.json", RESUME_ID=resume)
    payload["company_name"] = imported["company"]  # what the wizard copies into the form
    payload["job_description"] = imported["description"]
    assert create(client, alice, payload).status_code == 201

    mine = client.get(SESSIONS, headers=alice).json()["items"]
    assert [i["company_name"] for i in mine] == ["Initech"]
    assert client.get(SESSIONS, headers=bob).json()["items"] == []  # Bob's dashboard stays empty


def test_a_failed_import_does_not_stop_a_manually_entered_session(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], server: LocalServer
) -> None:
    install(client, server)
    failed = client.post(
        "/api/v1/sessions/job-import", headers=alice, json={"url": "http://10.0.0.1/job"}
    )
    assert failed.status_code == 200 and failed.json()["status"] == "failed"
    resume = new_file(client, run_sql, alice, "resumes")
    created = create(client, alice, golden("create_payload_minimal.json", RESUME_ID=resume))
    assert created.status_code == 201  # the form the user filled in by hand


# ------------------------------------------------------- readiness: UI == server


def frontend_is_ready(item: dict[str, Any]) -> bool:
    """The rule in apps/web/lib/sessions.ts `isReady`, written out independently."""
    ingestion, indexing = item["ingestion"], item["indexing"]
    return (
        ingestion is not None
        and indexing is not None
        and ingestion["status"] == "completed"
        and indexing["status"] == "completed"
    )


@pytest.mark.parametrize(
    ("extract", "embed"),
    [
        ("completed", "completed"),
        ("completed", "pending"),
        ("completed", "processing"),
        ("completed", "failed"),
        ("completed", None),
        ("failed", None),
        ("processing", None),
        ("pending", None),
    ],
)
def test_the_uis_ready_rule_matches_what_the_server_accepts(
    client: TestClient,
    run_sql: RunSql,
    alice: dict[str, str],
    extract: str,
    embed: str | None,
) -> None:
    resume = new_file(client, run_sql, alice, "resumes", ready=False)
    run_sql("UPDATE ingestion_jobs SET status = :s WHERE source_id = :i", s=extract, i=resume)
    if embed is not None:
        run_sql(
            "INSERT INTO ingestion_jobs (user_id, source_type, source_id, stage, status) "
            "SELECT user_id, source_type, source_id, 'embed', :s FROM ingestion_jobs "
            "WHERE source_id = :i",
            s=embed,
            i=resume,
        )
    listed = client.get("/api/v1/resumes?limit=200", headers=alice).json()["items"]
    item = next(i for i in listed if i["id"] == resume)
    accepted = create(client, alice, golden("create_payload_minimal.json", RESUME_ID=resume))
    assert (accepted.status_code == 201) == frontend_is_ready(item)


# ------------------------------------------------- response shapes the UI reads


def test_file_list_items_have_the_fields_the_wizard_reads(
    client: TestClient, run_sql: RunSql, alice: dict[str, str]
) -> None:
    new_file(client, run_sql, alice, "resumes")
    for kind in ("resumes", "documents"):
        body = client.get(f"/api/v1/{kind}?limit=200", headers=alice).json()
        assert set(body) == {"items"}
        for item in body["items"]:
            assert {"id", "filename", "content_type", "size_bytes", "created_at"} <= set(item)
            assert {"ingestion", "indexing"} <= set(item)
            for stage in (item["ingestion"], item["indexing"]):
                assert stage is None or {"status", "error_code", "error_message"} <= set(stage)


def test_ai_options_shape_matches_the_frontend_type(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(LLM_ENABLED_PROVIDERS="fake")
    headers = register(client, "alice@example.com")
    options = client.get("/api/v1/ai/options", headers=headers).json()
    assert set(options) == {
        "providers",
        "voice_providers",
        "languages",
        "question_types",
        "formats",
        "lengths",
        "tones",
    }
    assert set(options["providers"][0]) == {"id", "models"}
    assert set(options["providers"][0]["models"][0]) == {"id", "label", "capabilities", "languages"}
    assert set(options["voice_providers"][0]) == {"id", "models"}
    assert set(options["voice_providers"][0]["models"][0]) == {"id", "label"}


def test_session_list_and_import_shapes_match_the_frontend_types(
    client: TestClient, run_sql: RunSql, alice: dict[str, str], server: LocalServer
) -> None:
    resume = new_file(client, run_sql, alice, "resumes")
    create(client, alice, golden("create_payload_minimal.json", RESUME_ID=resume))
    listing = client.get(SESSIONS, headers=alice).json()
    assert set(listing) == {"items", "next_cursor"}
    assert set(listing["items"][0]) == {
        "id", "type", "title", "company_name", "status", "created_at", "updated_at",
        "resume", "document_count",
    }  # fmt: skip
    assert set(listing["items"][0]["resume"]) == {"id", "filename"}

    server.routes["/job"] = page(JSON_LD_PAGE)
    install(client, server)
    result = client.post(
        "/api/v1/sessions/job-import", headers=alice, json={"url": server.url("/job")}
    ).json()
    assert set(result) == {"status", "company", "title", "description", "message"}


# ----------------------------------------------- error codes the wizard maps


def test_every_error_code_the_wizard_maps_is_a_real_backend_code() -> None:
    wizard = WIZARD_TS.read_text(encoding="utf-8")
    mapped = set(re.findall(r'case "([a-z_]+)":', wizard))
    assert mapped, "no error codes found in wizard.ts (did the mapping move?)"
    backend = "\n".join(p.read_text(encoding="utf-8") for p in API_APP.rglob("*.py"))
    missing = {code for code in mapped if f'"{code}"' not in backend}
    assert missing == set()


def test_the_documented_create_errors_have_the_codes_and_statuses_the_ui_expects(
    make_client: Callable[..., TestClient], run_sql: RunSql
) -> None:
    client = make_client()
    headers = register(client, "alice@example.com")
    real = ids(client, run_sql, headers)
    pending = new_file(client, run_sql, headers, "documents", ready=False)
    base = golden("create_payload_minimal.json", RESUME_ID=real["RESUME_ID"])

    def post(**over: Any) -> Any:
        return create(client, headers, {**base, **over})

    not_found = post(document_ids=["00000000-0000-4000-8000-0000000000aa"])
    not_ready = post(document_ids=[pending])
    duplicate = post(document_ids=[real["DOC_ID_1"], real["DOC_ID_1"]])
    eleven = [new_file(client, run_sql, headers, "documents") for _ in range(11)]
    too_many = post(document_ids=eleven)
    invalid_enum = post(preferences={**base["preferences"], "language": "fr"})
    bad_provider = post(preferences={**base["preferences"], "provider": "nope"})
    long_extra = post(preferences={**base["preferences"], "extra_instructions": "a" * 1001})

    assert (not_found.status_code, not_found.json()["error"]["code"]) == (404, "material_not_found")
    assert (not_ready.status_code, not_ready.json()["error"]["code"]) == (422, "material_not_ready")
    assert (duplicate.status_code, duplicate.json()["error"]["code"]) == (422, "duplicate_material")
    assert (too_many.status_code, too_many.json()["error"]["code"]) == (422, "too_many_documents")
    assert (invalid_enum.status_code, invalid_enum.json()["error"]["code"]) == (
        422,
        "validation_error",
    )
    assert invalid_enum.json()["error"]["details"][0]["field"].startswith("preferences")
    assert (bad_provider.status_code, bad_provider.json()["error"]["code"]) == (
        422,
        "invalid_provider_selection",
    )
    assert (
        long_extra.status_code == 422
        and "extra_instructions" in long_extra.json()["error"]["message"]
    )
