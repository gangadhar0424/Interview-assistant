"""Typed question answering core (Phase 8A, D-38): context assembly, orchestration, persistence.

Backend core only: no HTTP endpoints, SSE framing, rate limits or UI (8B/8C). The orchestrator
yields plain events (named after the documented realtime events) that 8B will serialise. It never
holds a database session while streaming, and every query is scoped by the authenticated user.
"""
