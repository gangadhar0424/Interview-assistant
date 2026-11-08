"""Provider-neutral LLM access (Phase 6, docs/ARCHITECTURE.md section 6).

Business code depends only on `LLMRouter` / the `LLMProvider` port. Provider SDKs may be imported
ONLY inside `app/ai/adapters/` (enforced by ruff TID251 and tests/test_ai_import_boundary.py).
"""
