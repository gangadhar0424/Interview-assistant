# Realtime protocol package

`protocol.schema.json` and `src/index.ts` are **generated** from the Pydantic models in
`services/api/app/realtime/schemas.py` (docs/API.md section 8, docs/DECISIONS.md D-41). Do not edit them.

```powershell
cd services/api
uv run python -m app.realtime.codegen           # regenerate
uv run python -m app.realtime.codegen --check   # exit 1 if the committed files are stale
```

A backend test (`tests/test_realtime_codegen.py`) fails when the committed files differ from a fresh
generation. The generator is in-repository and adds no dependency. This directory has no `package.json`
yet: the first consumer (the Phase 10 desktop client) decides how it is imported.
