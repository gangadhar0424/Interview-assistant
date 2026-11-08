"""Best-effort job-page import for the session wizard (Phase 7C, D-36).

The user pastes a public job link; the server fetches it under strict SSRF controls and returns
suggested company/title/description for the user to review. Nothing is stored (no schema change),
no LLM is involved, and any failure leaves manual entry fully available.
"""
