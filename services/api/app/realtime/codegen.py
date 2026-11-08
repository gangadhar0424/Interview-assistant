"""Protocol code generation (D-41): Pydantic models -> JSON Schema -> TypeScript. No dependencies.

    python -m app.realtime.codegen            # (re)write packages/protocol/
    python -m app.realtime.codegen --check    # exit 1 if the committed files are out of date

Outputs (committed, drift-tested in tests/test_realtime_codegen.py):
  packages/protocol/protocol.schema.json   client + server message JSON Schemas, close codes
  packages/protocol/src/index.ts           TypeScript types generated from that schema

The TypeScript writer understands exactly the subset of JSON Schema that Pydantic emits for these
models (objects, arrays, strings, numbers, booleans, null, const, enum, anyOf, $ref) and raises on
anything else, so a new construct can never be silently mis-typed.
"""

import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import REPO_ROOT
from app.realtime import schemas

PACKAGE_DIR = REPO_ROOT / "packages" / "protocol"
SCHEMA_PATH = PACKAGE_DIR / "protocol.schema.json"
TS_PATH = PACKAGE_DIR / "src" / "index.ts"

HEADER = (
    "// GENERATED FILE - DO NOT EDIT.\n"
    "// Source: services/api/app/realtime/schemas.py (docs/API.md section 8).\n"
    "// Regenerate: cd services/api && uv run python -m app.realtime.codegen\n"
)


# ------------------------------------------------------------------------------ JSON Schema
def build_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Interview Assistant realtime protocol",
        "protocol_version": schemas.PROTOCOL_VERSION,
        "close_codes": dict(schemas.CLOSE_CODES),
        # Client messages are validated (defaults optional); server messages are serialized
        # (every field present), which is what a consumer receives.
        "client_message": schemas.client_adapter.json_schema(mode="validation"),
        "server_message": schemas.server_adapter.json_schema(mode="serialization"),
    }


def render_schema(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


# ------------------------------------------------------------------------------ TypeScript
class UnsupportedSchemaError(ValueError):
    pass


def _ref_name(ref: str) -> str:
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        raise UnsupportedSchemaError(f"unsupported $ref {ref!r}")
    return ref[len(prefix) :]


def ts_type(node: dict[str, Any], indent: int = 0) -> str:
    """The TypeScript type of one JSON Schema node."""
    if "$ref" in node:
        return _ref_name(node["$ref"])
    if "const" in node:
        return json.dumps(node["const"], ensure_ascii=False)
    if "enum" in node:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in node["enum"])
    if "anyOf" in node:
        return " | ".join(ts_type(n, indent) for n in node["anyOf"])
    kind = node.get("type")
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        item = ts_type(node["items"], indent)
        return f"({item})[]" if " | " in item else f"{item}[]"
    if kind == "object":
        return _object(node, indent)
    raise UnsupportedSchemaError(f"unsupported schema node: {sorted(node)}")


def _object(node: dict[str, Any], indent: int) -> str:
    props: dict[str, Any] = node.get("properties", {})
    if not props:
        if node.get("additionalProperties") is False or "additionalProperties" not in node:
            return "Record<string, never>"
        raise UnsupportedSchemaError("open objects are not supported")
    if node.get("additionalProperties", False) is not False:
        raise UnsupportedSchemaError("open objects are not supported")
    required = set(node.get("required", []))
    pad = "  " * (indent + 1)
    lines = ["{"]
    for name, child in props.items():
        optional = "" if name in required else "?"
        lines.append(f"{pad}{name}{optional}: {ts_type(child, indent + 1)};")
    lines.append("  " * indent + "}")
    return "\n".join(lines)


def _definitions(root: dict[str, Any]) -> dict[str, Any]:
    """All named types of one message schema: its $defs plus the union itself."""
    return dict(root.get("$defs", {}))


def render_typescript(schema: dict[str, Any]) -> str:
    out = [HEADER]
    out.append(f"export const PROTOCOL_VERSION = {schema['protocol_version']};\n")
    codes = ",\n".join(f"  {k}: {v}" for k, v in schema["close_codes"].items())
    out.append(f"export const CLOSE_CODES = {{\n{codes},\n}} as const;\n")

    seen: dict[str, str] = {}
    for key in ("client_message", "server_message"):
        root = schema[key]
        for name, node in _definitions(root).items():
            body = ts_type(node)
            if name in seen and seen[name] != body:
                raise UnsupportedSchemaError(f"type name {name!r} is defined twice differently")
            if name in seen:
                continue
            seen[name] = body
            if node.get("type") == "object" and node.get("properties"):
                out.append(f"export interface {name} {body}\n")
            else:
                out.append(f"export type {name} = {body};\n")
        union = ts_type({"anyOf": root["oneOf"]} if "oneOf" in root else root)
        label = "ClientMessage" if key == "client_message" else "ServerMessage"
        out.append(f"export type {label} = {union};\n")
        types = [n["$ref"] for n in root.get("oneOf", [])]
        names = " | ".join(f"{_ref_name(t)}['type']" for t in types)
        out.append(f"export type {label}Type = {names};\n")
    return "\n".join(out)


# ------------------------------------------------------------------------------ CLI
def generate() -> dict[Path, str]:
    schema = build_schema()
    return {SCHEMA_PATH: render_schema(schema), TS_PATH: render_typescript(schema)}


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None  # universal newlines


def stale_files() -> list[Path]:
    return [p for p, text in generate().items() if _read(p) != text]


def main(argv: list[str]) -> int:
    if "--check" in argv:
        stale = stale_files()
        for path in stale:
            sys.stderr.write(f"out of date: {_display(path)}\n")
        if stale:
            sys.stderr.write("Run: uv run python -m app.realtime.codegen\n")
        return 1 if stale else 0
    for path, text in generate().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        sys.stdout.write(f"wrote {_display(path)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
