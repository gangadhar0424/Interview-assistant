"""Generated protocol package (Phase 9A, D-41): committed files must equal a fresh generation."""

import json
import re

import pytest

from app.realtime import codegen
from app.realtime import schemas as proto


def test_committed_protocol_files_are_up_to_date() -> None:
    stale = [str(p.name) for p in codegen.stale_files()]
    assert stale == [], f"regenerate with `uv run python -m app.realtime.codegen`: {stale}"


def test_the_check_mode_reports_drift(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    base = Path(str(tmp_path))
    monkeypatch.setattr(codegen, "SCHEMA_PATH", base / "protocol.schema.json")
    monkeypatch.setattr(codegen, "TS_PATH", base / "src" / "index.ts")
    assert codegen.main(["--check"]) == 1  # nothing generated yet
    assert codegen.main([]) == 0
    assert codegen.main(["--check"]) == 0
    codegen.TS_PATH.write_text(codegen.TS_PATH.read_text(encoding="utf-8") + "// edit\n")
    assert codegen.main(["--check"]) == 1
    assert [p.name for p in codegen.stale_files()] == ["index.ts"]


def test_the_schema_file_lists_every_message_type_and_the_close_codes() -> None:
    schema = json.loads(codegen.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["protocol_version"] == proto.PROTOCOL_VERSION
    assert schema["close_codes"] == proto.CLOSE_CODES

    def types(key: str) -> set[str]:
        defs = schema[key]["$defs"]
        return {
            d["properties"]["type"]["const"]
            for d in defs.values()
            if "type" in d.get("properties", {})
        }

    assert types("client_message") == proto.CLIENT_MESSAGE_TYPES
    assert types("server_message") == {
        "session.ready",
        "pong",
        "error",
        "question.created",
        "retrieval.done",
        "answer.started",
        "answer.delta",
        "answer.done",
        "answer.error",
        "answer.cancelled",
    }


def test_the_typescript_declares_every_event_and_the_constants() -> None:
    ts = codegen.TS_PATH.read_text(encoding="utf-8")
    assert ts.startswith("// GENERATED FILE - DO NOT EDIT.")
    assert f"export const PROTOCOL_VERSION = {proto.PROTOCOL_VERSION};" in ts
    for name, value in proto.CLOSE_CODES.items():
        assert f"  {name}: {value}," in ts
    for interface in (
        "ClientHello",
        "SessionReady",
        "AnswerDone",
        "AnswerCancelled",
        "QuestionCreated",
        "ErrorEvent",
        "Pong",
    ):
        assert re.search(rf"export interface {interface} \{{", ts), interface
    assert "export type ClientMessage = " in ts and "export type ServerMessage = " in ts
    # answer.done carries the SSE fields (owner decision 2)
    block = ts.split("export interface AnswerDonePayload {")[1].split("}\n")[0]
    for field in ("question_id", "provider", "model", "metrics", "input_tokens", "output_tokens"):
        assert field in block


def test_generation_is_deterministic() -> None:
    assert codegen.generate() == codegen.generate()


# ---------------------------------------------------------------- the converter itself
def test_the_converter_maps_the_supported_schema_subset() -> None:
    node = {
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "c": {"type": "array", "items": {"type": "string"}},
            "d": {"const": "x", "type": "string"},
            "e": {"$ref": "#/$defs/Other"},
            "f": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
            "g": {"enum": ["u", "v"]},
        },
        "required": ["a", "d"],
        "additionalProperties": False,
    }
    assert codegen.ts_type(node) == (
        '{\n  a: number;\n  b?: string | null;\n  c?: string[];\n  d: "x";\n'
        '  e?: Other;\n  f?: (string | number)[];\n  g?: "u" | "v";\n}'
    )


@pytest.mark.parametrize(
    "node",
    [
        {"type": "object", "additionalProperties": True},
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
        {"allOf": [{"type": "string"}]},
        {"$ref": "http://elsewhere/x"},
        {"type": "tuple"},
    ],
)
def test_the_converter_refuses_constructs_it_does_not_understand(node: dict[str, object]) -> None:
    with pytest.raises(codegen.UnsupportedSchemaError):
        codegen.ts_type(node)
