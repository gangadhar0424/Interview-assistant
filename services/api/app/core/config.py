"""Environment-based configuration. Fails fast with a safe, useful message if misconfigured."""

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


def find_repo_root(config_file: Path) -> Path:
    """The repository root in a checkout; the application directory inside a container image.

    In a checkout the file is services/api/app/core/config.py, four levels below the root. In the
    container image it is /app/app/core/config.py, which has no such ancestor (it used to crash
    with an IndexError on import), so the application directory /app is used instead.
    """
    parents = config_file.resolve().parents
    if len(parents) > 4 and (parents[4] / "services" / "api").is_dir():
        return parents[4]
    return parents[2]


REPO_ROOT = find_repo_root(Path(__file__))

MIN_JWT_SECRET_LENGTH = 43
PLACEHOLDER_SECRET_PREFIX = "change-me"  # noqa: S105
_JWT_SECRET_CHARS = re.compile(r"^[A-Za-z0-9_-]+$")


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


def _is_loopback_host(host: str | None) -> bool:
    return host is not None and host.lower() in _LOOPBACK_HOSTS


def _has_secret(value: SecretStr | None) -> bool:
    return value is not None and bool(value.get_secret_value().strip())


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid (never includes values)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "staging", "production"] = "local"
    # SEC-07: `local` switches off every production safeguard, so it is only accepted when the
    # database, Redis and CORS origins are all on this machine. Set true ONLY for a development
    # setup that deliberately uses a remote database; never in a deployment.
    local_allow_remote_services: bool = False
    # SEC-18: when set, GET /metrics requires `Authorization: Bearer <token>` (defence in depth next
    # to the proxy rule that keeps /metrics off the public hosts). Unset = open, as before.
    metrics_bearer_token: SecretStr | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: str = Field(min_length=1)
    maintenance_database_url: str | None = None
    redis_url: str = Field(min_length=1)

    # --- Database connection hardening (M-12, D-79) ---
    # Every API process and every worker process has its own pool: size them against Postgres'
    # max_connections (processes x (pool_size + max_overflow)). Waiting for a free connection is
    # bounded, so exhaustion becomes a fast 503 instead of requests hanging.
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    db_pool_recycle_seconds: int = Field(default=1800, ge=30, le=86400)
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    # Server-side limits applied to every connection: a runaway query or a transaction left open
    # by a crashed request cannot hold locks or a connection forever.
    db_statement_timeout_ms: int = Field(default=60_000, ge=1_000, le=600_000)
    db_idle_in_transaction_timeout_ms: int = Field(default=300_000, ge=1_000, le=3_600_000)
    # The interactive API documentation and the OpenAPI schema are served only in local
    # development; outside it they are always off (they describe every endpoint to anyone).
    api_docs_enabled: bool = True

    # --- Authentication (Phase 2) ---
    # HMAC signing secret for access tokens. Required; at least 256 bits of encoded material.
    jwt_secret: SecretStr
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_ttl_days: int = Field(default=14, ge=1, le=90)
    # Comma-separated browser origins allowed for CORS and for cookie-authenticated POSTs.
    cors_origins: str = "http://localhost:3000"

    # --- File storage (Phase 3, D-26) ---
    # Root of the persistent local file store. Defaults to <repo>/app-data in local development;
    # REQUIRED (explicit) in staging/production, where it must be a persistent volume.
    file_storage_root: Path | None = None
    # Upload size limits in MiB.
    resume_max_upload_mb: int = Field(default=10, ge=1, le=100)
    document_max_upload_mb: int = Field(default=20, ge=1, le=200)
    # Per-user upload quotas (SEC-15). Enforced server-side under a per-user lock. The defaults are
    # engineering assumptions sized for an internal tool, not product decisions: the owner should
    # confirm or change them. Storage counts resumes and documents together.
    user_max_resumes: int = Field(default=20, ge=1, le=10_000)
    user_max_documents: int = Field(default=100, ge=1, le=10_000)
    user_max_storage_mb: int = Field(default=500, ge=1, le=100_000)

    # --- Data lifecycle (SEC-13, D-85) ---
    # Deletion ledger: append-only files of anonymous ids (kind, object id, owner id, time) that are
    # re-applied after a database restore so deleted data is not brought back. DELETION_LEDGER_PATH
    # is a DIRECTORY outside the database and the uploads volume that a restore replaces, and it is
    # copied off-host with the backups. Each line carries an HMAC made with DELETION_LEDGER_KEY.
    # Both are required outside local (app/lifecycle/ledger.py).
    deletion_ledger_path: Path | None = None
    deletion_ledger_key: SecretStr | None = None
    # Orphan-file sweep: a stored file or temp part younger than this is never treated as an orphan,
    # because an upload writes the file before it inserts the database row.
    orphan_sweep_min_age_seconds: int = Field(default=3600, ge=300, le=7 * 24 * 3600)

    # --- Ingestion (Phase 4) ---
    # A job may be claimed this many times (initial run + automatic recoveries) before failing.
    ingestion_max_attempts: int = Field(default=3, ge=1, le=10)
    # A `processing` job whose lock is older than this is considered stuck (crashed worker).
    ingestion_processing_timeout_seconds: int = Field(default=900, ge=30)
    # A `pending` job older than this was probably never delivered to the queue.
    ingestion_pending_redispatch_seconds: int = Field(default=120, ge=10)

    # --- Embeddings, chunking and retrieval (Phase 5) ---
    # `fake` = deterministic hashed vectors (tests/local only, no semantics). `fastembed` = local
    # multilingual model (needs `uv sync --extra embeddings`). Must be set outside local.
    embedding_provider: Literal["fake", "fastembed"] | None = None
    embedding_model: str = "intfloat/multilingual-e5-large"
    # Where fastembed caches downloaded model files (persist this on the worker host).
    embedding_cache_dir: Path | None = None
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    # Chunk size/overlap in characters (see app/ingestion/chunking.py).
    chunk_size_chars: int = Field(default=800, ge=200, le=4000)
    chunk_overlap_chars: int = Field(default=100, ge=0)
    # Hard ceiling for the number of chunks returned by one retrieval call.
    retrieval_max_limit: int = Field(default=20, ge=1, le=100)

    # --- LLM provider layer (Phase 6, D-33) ---
    # Comma-separated provider ids to enable. Empty = no LLM provider enabled.
    llm_enabled_providers: str = ""
    # Comma-separated fallback order (subset of the enabled providers). Empty = enabled order.
    llm_fallback_chain: str = ""
    openai_api_key: SecretStr | None = None
    ollama_api_key: SecretStr | None = None
    # Server-side keys only (D-57). One configurable model per provider per role.
    groq_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    sarvam_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    gemini_model: str = Field(default="gemini-3.1-flash-lite", min_length=1, max_length=100)
    groq_model: str = Field(default="openai/gpt-oss-120b", min_length=1, max_length=100)
    anthropic_model: str = Field(default="claude-sonnet-5-5", min_length=1, max_length=100)
    openai_base_url: AnyHttpUrl = AnyHttpUrl("https://api.openai.com/v1")
    openai_model: str = Field(default="gpt-4o-mini", min_length=1, max_length=100)
    openai_timeout: float = Field(default=30.0, gt=0, le=300)
    llm_request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    llm_first_token_timeout_seconds: float = Field(default=4.0, gt=0, le=60)
    llm_stream_idle_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    llm_breaker_failure_threshold: int = Field(default=3, ge=1, le=50)
    llm_breaker_cooldown_seconds: float = Field(default=30.0, gt=0, le=3600)

    # --- Screenshot input (Phase 11A, D-51) ---
    # Vision providers are a SEPARATE allow-list from the text LLM providers; empty (the default)
    # disables screenshot upload. Only the deterministic `fake` provider exists.
    vision_enabled_providers: str = ""
    vision_fallback_chain: str = ""
    vision_request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    screenshot_max_bytes: int = Field(default=5 * 1024 * 1024, ge=10_000, le=10 * 1024 * 1024)
    screenshot_max_long_edge: int = Field(default=4096, ge=64, le=16384)
    screenshot_max_pixels: int = Field(default=16_000_000, ge=4096, le=100_000_000)
    screenshot_rate_limit_per_minute: int = Field(default=6, ge=1, le=600)
    screenshot_max_concurrent: int = Field(default=1, ge=1, le=10)

    # --- Audio input (Phase 12, D-53) ---
    # Speech-to-text providers are a SEPARATE allow-list from the text and vision providers; empty
    # (the default) disables utterance transcription. Only the deterministic `fake` provider exists.
    stt_enabled_providers: str = ""
    openai_stt_model: str = Field(default="gpt-4o-mini-transcribe", min_length=1, max_length=100)
    groq_stt_model: str = Field(default="whisper-large-v3-turbo", min_length=1, max_length=100)
    sarvam_stt_model: str = Field(default="saarika:v2.5", min_length=1, max_length=100)
    gemini_stt_model: str = Field(default="gemini-3.1-flash-lite", min_length=1, max_length=100)
    stt_request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    audio_max_bytes: int = Field(default=1_500_000, ge=10_000, le=5_000_000)
    audio_max_seconds: int = Field(default=30, ge=1, le=120)
    audio_rate_limit_per_minute: int = Field(default=40, ge=1, le=600)
    audio_max_concurrent: int = Field(default=2, ge=1, le=10)

    # --- Answer preferences (Phase 7A, D-34) ---
    # Word targets per answer length. The compiler turns them into prompt guidance and into a
    # `max_output_tokens` budget (words x tokens-per-word, with a multiplier for Hindi/Telugu).
    answer_short_min_words: int = Field(default=60, ge=1, le=2000)
    answer_short_max_words: int = Field(default=90, ge=1, le=2000)
    answer_balanced_min_words: int = Field(default=120, ge=1, le=2000)
    answer_balanced_max_words: int = Field(default=180, ge=1, le=2000)
    answer_long_min_words: int = Field(default=250, ge=1, le=2000)
    answer_long_max_words: int = Field(default=350, ge=1, le=2000)
    answer_tokens_per_word: float = Field(default=2.0, ge=1.0, le=4.0)
    answer_indic_token_multiplier: float = Field(default=2.5, ge=1.0, le=4.0)
    # Maximum length of a session's free-text extra instructions (after cleaning).
    extra_instructions_max_chars: int = Field(default=1000, ge=1, le=5000)

    # --- Sessions (Phase 7B, D-35) ---
    # Maximum length of a session's job description (characters) and of selected documents.
    session_job_description_max_chars: int = Field(default=20000, ge=100, le=200000)
    session_max_documents: int = Field(default=10, ge=1, le=50)

    # --- Auth abuse protection (Phase 13A Batch 2, D-54). All limits are per API process. ---
    # Failed logins per normalized email / per client address within the window, signup attempts
    # per client per hour, and failed signup gate checks per hour across all clients.
    login_failures_per_email: int = Field(default=5, ge=1, le=100)
    login_failures_per_client: int = Field(default=20, ge=1, le=1000)
    login_window_seconds: int = Field(default=900, ge=60, le=86400)
    signup_attempts_per_client_per_hour: int = Field(default=10, ge=1, le=1000)
    signup_failures_global_per_hour: int = Field(default=30, ge=1, le=10000)
    # Signup access. Outside APP_ENV=local signup is CLOSED unless this invite code is configured
    # (at least 16 characters). In local development, no code configured means open signup.
    signup_invite_code: SecretStr | None = None
    # Read the client address from X-Forwarded-For ONLY when true (and run uvicorn with
    # --no-proxy-headers). `hops` is the number of proxies that append to the header.
    trusted_proxy_enabled: bool = False
    trusted_proxy_hops: int = Field(default=1, ge=1, le=5)

    # --- Job import (Phase 7C, D-36) ---
    # Total time budget for one import (DNS + connect + download), body size cap in bytes, and the
    # number of redirects followed (each hop is re-validated against the SSRF policy).
    job_import_timeout_seconds: float = Field(default=8.0, gt=0, le=30)
    job_import_max_bytes: int = Field(default=1_048_576, ge=10_000, le=5_000_000)
    job_import_max_redirects: int = Field(default=3, ge=0, le=5)
    # Per-user attempts per minute and concurrent imports per API process (in-process limits).
    job_import_rate_limit_per_minute: int = Field(default=10, ge=1, le=600)
    job_import_max_concurrent: int = Field(default=4, ge=1, le=50)

    # --- Typed Q&A context assembly (Phase 8A, D-38) ---
    # Passages put in a prompt: at most `top_k` chunks, of which the best `resume_quota` come from
    # the session's resume (so profile facts are always present), within a character budget.
    # History = the last `history_pairs` answered Q&A pairs of the session (each side truncated).
    qa_retrieval_top_k: int = Field(default=8, ge=1, le=20)
    qa_resume_quota: int = Field(default=3, ge=0, le=20)
    qa_context_max_chars: int = Field(default=6000, ge=500, le=100000)
    # D-64: optional floor on a selected document's passage similarity to the question (cosine,
    # 0-1; the resume is never filtered). OFF by default (0): measured scores with
    # multilingual-e5-large are low and close together (related ~0.3-0.5, unrelated ~0.2-0.3), so a
    # floor drops relevant passages. The v4 prompt already tells the model to ignore unhelpful ones.
    qa_min_document_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    # --- Answer pipeline (D-66): hybrid retrieval, router, exact-text answers, validation ---
    # Every switch defaults to the previous behaviour; flip them in .env to adopt a stage.
    qa_retrieval_mode: Literal["dense", "hybrid"] = "dense"
    qa_router_mode: Literal["off", "shadow", "on"] = "off"
    qa_validate_general: Literal["off", "on"] = "off"
    # Hybrid search: candidates per branch (dense / lexical) and the rank-fusion constant (RRF).
    qa_hybrid_candidates: int = Field(default=30, ge=5, le=200)
    qa_rrf_k: int = Field(default=60, ge=1, le=1000)
    # At most this many chunks of ONE document may reach the candidate list (the resume is exempt),
    # so one large file cannot crowd out the other selected documents.
    qa_per_document_cap: int = Field(default=3, ge=1, le=20)
    # Router confidence (calibrated on the evaluation set, tests/eval): the share of the question's
    # content words that must appear in a passage's heading / body, and the minimum lead of the
    # best fused score over the next candidate.
    qa_router_heading_coverage: float = Field(default=0.6, gt=0.0, le=1.0)
    qa_router_body_coverage: float = Field(default=0.75, gt=0.0, le=1.0)
    qa_router_margin: float = Field(default=1.0, ge=1.0, le=10.0)
    # Adaptive question rewrite (only for an unsure router) and the general-answer judge: tight
    # caps so an auxiliary call can never dominate the response time.
    qa_rewrite_timeout_seconds: float = Field(default=0.8, gt=0, le=10)
    qa_judge_timeout_seconds: float = Field(default=0.8, gt=0, le=10)
    # Per-session answer cache (Redis) for validated general answers; 0 disables it. Capped at 300 s
    # outside local (SEC-13 Decision 1, D-85): entries are purged on deletion, and the lifetime
    # bounds anything a failed purge leaves behind.
    qa_answer_cache_ttl_seconds: int = Field(default=300, ge=0, le=3600)
    # A question that names a section (a title match) whose text is longer than the session's length
    # setting is answered from the WHOLE section by the model (grounded, complete, in the requested
    # format) instead of a cut-off slice of raw text. Section text sent to the model is capped at
    # this many characters; the answer may be up to this many words.
    qa_section_context_max_chars: int = Field(default=12000, ge=1000, le=100000)
    qa_section_answer_max_words: int = Field(default=450, ge=100, le=2000)
    # Per-question answer detail (D-66): "full" answers a named section completely, "summary" keeps
    # every model-written answer to the short length band (a spoken half-minute). The request may
    # choose; this is the default when it does not.
    qa_default_detail: Literal["full", "summary"] = "full"
    qa_history_pairs: int = Field(default=3, ge=0, le=10)
    qa_history_item_max_chars: int = Field(default=1000, ge=100, le=10000)

    # --- Typed Q&A API protection (Phase 8B, D-39) ---
    # Question requests per user per minute, and simultaneous generations per user (per process).
    qa_rate_limit_per_minute: int = Field(default=20, ge=1, le=600)
    qa_max_concurrent_generations: int = Field(default=2, ge=1, le=20)
    # Total time budget of one generation. A `streaming` answer older than this is read as `error`.
    qa_request_timeout_seconds: float = Field(default=120.0, ge=1, le=900)

    # --- WebSocket gateway (Phase 9A, D-41). All limits are per API process. ---
    # Single-use ticket lifetime and how many tickets one user may request per minute.
    ws_ticket_ttl_seconds: int = Field(default=30, ge=5, le=120)
    ws_ticket_rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    # --- Web -> desktop handoff (D-55). Single-use code lifetime; per-user request limits. ---
    handoff_code_ttl_seconds: int = Field(default=60, ge=10, le=120)
    # SEC-08: when true, a handoff code must be bound to a desktop-held secret (PKCE-style challenge
    # at issue, verifier at redemption); unbound codes are refused. Off until the web and desktop
    # clients send the challenge, so today's link flow keeps working.
    handoff_require_challenge: bool = False
    handoff_issue_rate_limit_per_minute: int = Field(default=10, ge=1, le=600)
    handoff_redeem_rate_limit_per_minute: int = Field(default=20, ge=1, le=600)
    # The client pings about every heartbeat interval; silence for the idle timeout closes the
    # socket; the first message (`client.hello`) must arrive within the hello timeout.
    ws_heartbeat_interval_seconds: float = Field(default=20.0, gt=0, le=120)
    ws_idle_timeout_seconds: float = Field(default=45.0, gt=0, le=600)
    ws_hello_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # Largest accepted text frame (bytes) and message rate per connection.
    ws_max_frame_bytes: int = Field(default=16384, ge=256, le=1_048_576)
    ws_max_messages_per_second: int = Field(default=10, ge=1, le=1000)
    # Connection caps: per user (across their sessions) and for the whole process.
    ws_max_connections_per_user: int = Field(default=3, ge=1, le=100)
    ws_max_total_connections: int = Field(default=200, ge=1, le=100_000)
    # Backpressure (9B, D-42): messages waiting to be written to one socket, and how long one write
    # may take. A client that exceeds either is a slow consumer and is closed (4010).
    ws_send_queue_events: int = Field(default=1000, ge=10, le=100_000)
    ws_send_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # Detached generation and resume (9C, D-43): how long a generation keeps running after its
    # socket drops, and how many sequenced events a (user, session) channel retains for replay.
    ws_resume_grace_seconds: float = Field(default=30.0, gt=0, le=600)
    ws_resume_buffer_events: int = Field(default=2000, ge=10, le=100_000)

    @model_validator(mode="after")
    def _ws_settings(self) -> Self:
        if self.ws_idle_timeout_seconds <= self.ws_heartbeat_interval_seconds:
            raise ValueError("WS_IDLE_TIMEOUT_SECONDS must exceed WS_HEARTBEAT_INTERVAL_SECONDS")
        if self.ws_max_total_connections < self.ws_max_connections_per_user:
            raise ValueError(
                "WS_MAX_TOTAL_CONNECTIONS must be at least WS_MAX_CONNECTIONS_PER_USER"
            )
        return self

    @model_validator(mode="after")
    def _qa_context_settings(self) -> Self:
        if self.qa_resume_quota > self.qa_retrieval_top_k:
            raise ValueError("QA_RESUME_QUOTA must be at most QA_RETRIEVAL_TOP_K")
        if self.qa_retrieval_top_k > self.retrieval_max_limit:
            raise ValueError("QA_RETRIEVAL_TOP_K must be at most RETRIEVAL_MAX_LIMIT")
        return self

    @model_validator(mode="after")
    def _answer_length_targets(self) -> Self:
        bands = [
            (self.answer_short_min_words, self.answer_short_max_words),
            (self.answer_balanced_min_words, self.answer_balanced_max_words),
            (self.answer_long_min_words, self.answer_long_max_words),
        ]
        if any(low > high for low, high in bands):
            raise ValueError("each ANSWER_*_MIN_WORDS must be <= its ANSWER_*_MAX_WORDS")
        if any(prev[1] > nxt[0] for prev, nxt in zip(bands, bands[1:], strict=False)):
            raise ValueError("answer length bands must not overlap (short < balanced < long)")
        return self

    @field_validator("metrics_bearer_token", mode="before")
    @classmethod
    def _blank_metrics_token_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _metrics_token_is_strong(self) -> Self:
        token = self.metrics_bearer_token
        if token is not None and len(token.get_secret_value()) < 24:
            raise ValueError("METRICS_BEARER_TOKEN must be at least 24 characters when set")
        return self

    @field_validator("embedding_cache_dir", "embedding_provider", mode="before")
    @classmethod
    def _blank_embedding_value_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator(
        "file_storage_root", "deletion_ledger_path", "deletion_ledger_key", mode="before"
    )
    @classmethod
    def _blank_storage_root_is_unset(cls, v: object) -> object:
        # `FILE_STORAGE_ROOT=` (empty) must mean "not set", never "the current directory".
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("database_url")
    @classmethod
    def _async_postgres_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("must start with 'postgresql+asyncpg://'")
        return v

    @field_validator("maintenance_database_url", mode="before")
    @classmethod
    def _blank_maintenance_url_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("maintenance_database_url")
    @classmethod
    def _async_maintenance_postgres_driver(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("postgresql+asyncpg://"):
            raise ValueError("must start with 'postgresql+asyncpg://'")
        return v

    @field_validator("redis_url")
    @classmethod
    def _redis_scheme(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://")):
            raise ValueError("must start with 'redis://' or 'rediss://'")
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_length(cls, v: SecretStr) -> SecretStr:
        secret = v.get_secret_value()
        if len(secret) < MIN_JWT_SECRET_LENGTH:
            raise ValueError("must contain at least 43 URL-safe characters")
        return v

    @field_validator("cors_origins")
    @classmethod
    def _cors_origins_valid(cls, v: str) -> str:
        for origin in (o.strip() for o in v.split(",") if o.strip()):
            if origin == "*" or not origin.startswith(("http://", "https://")):
                raise ValueError("must be a comma-separated list of explicit http(s) origins")
        return v

    @field_validator("signup_invite_code")
    @classmethod
    def _blank_invite_code_is_unset(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and not v.get_secret_value().strip():
            return None
        return v

    @model_validator(mode="after")
    def _invite_code_strength_outside_local(self) -> Self:
        code = self.signup_invite_code
        if self.app_env != "local" and code is not None and len(code.get_secret_value()) < 16:
            raise ValueError("SIGNUP_INVITE_CODE must be at least 16 characters outside local")
        return self

    @model_validator(mode="after")
    def _database_pool_is_consistent(self) -> Self:
        if self.db_pool_timeout_seconds <= self.db_connect_timeout_seconds / 10:
            raise ValueError("DB_POOL_TIMEOUT_SECONDS is unreasonably short")
        if self.db_statement_timeout_ms >= self.db_idle_in_transaction_timeout_ms * 10:
            raise ValueError("DB_STATEMENT_TIMEOUT_MS must be far below the idle-transaction limit")
        return self

    @property
    def docs_enabled(self) -> bool:
        """API docs only in local development, whatever API_DOCS_ENABLED says."""
        return self.app_env == "local" and self.api_docs_enabled

    @model_validator(mode="after")
    def _local_environment_is_really_local(self) -> Self:
        """SEC-07: APP_ENV defaults to `local`, which disables every production safeguard.

        A deployment that forgets APP_ENV must not start with those relaxed defaults, so `local`
        is refused when the database, Redis or an allowed web origin is not on this machine.
        """
        if self.app_env != "local" or self.local_allow_remote_services:
            return self
        remote: list[str] = []
        for name, url in (("DATABASE_URL", self.database_url), ("REDIS_URL", self.redis_url)):
            if not _is_loopback_host(urlsplit(url).hostname):
                remote.append(name)
        for origin in self.cors_origin_list:
            if not _is_loopback_host(urlsplit(origin).hostname):
                remote.append("CORS_ORIGINS")
                break
        if remote:
            raise ValueError(
                "APP_ENV is 'local' (the default) but "
                + ", ".join(remote)
                + (" points" if len(remote) == 1 else " point")
                + " away from this machine. Set APP_ENV=production (or staging) for a "
                "deployment. Local mode turns off the production security checks."
            )
        return self

    @model_validator(mode="after")
    def _no_placeholder_secret_outside_local(self) -> Self:
        secret = self.jwt_secret.get_secret_value()
        if self.app_env != "local":
            counts: dict[str, int] = {}
            for char in secret:
                counts[char] = counts.get(char, 0) + 1
            entropy = -sum(
                (count / len(secret)) * math.log2(count / len(secret)) for count in counts.values()
            )
            if (
                secret.startswith(PLACEHOLDER_SECRET_PREFIX)
                or not _JWT_SECRET_CHARS.fullmatch(secret)
                or len(counts) < 20
                or entropy < 3.5
            ):
                raise ValueError(
                    "JWT_SECRET must be generated from at least 32 cryptographically random "
                    "bytes outside APP_ENV=local"
                )
        if self.app_env != "local" and self.redis_url.startswith("redis://"):
            raise ValueError("REDIS_URL must use rediss:// outside APP_ENV=local")
        if self.app_env != "local" and self.file_storage_root is None:
            raise ValueError(
                "FILE_STORAGE_ROOT must be set outside APP_ENV=local (use a persistent volume)"
            )
        if (
            self.app_env != "local"
            and self.file_storage_root is not None
            and not self.file_storage_root.is_absolute()
        ):
            # A relative path would resolve against the process working directory, which in a
            # container is part of the replaceable (ephemeral) filesystem.
            raise ValueError("FILE_STORAGE_ROOT must be an absolute path outside APP_ENV=local")
        return self

    @model_validator(mode="after")
    def _data_lifecycle_settings(self) -> Self:
        """SEC-13 (D-85): the cache cap and the deletion ledger are mandatory outside local."""
        if self.app_env == "local":
            return self
        if self.qa_answer_cache_ttl_seconds > 300:
            raise ValueError(
                "QA_ANSWER_CACHE_TTL_SECONDS must be at most 300 outside APP_ENV=local"
            )
        if self.deletion_ledger_path is None or not self.deletion_ledger_path.is_absolute():
            raise ValueError(
                "DELETION_LEDGER_PATH must be an absolute path outside APP_ENV=local (a volume "
                "that a database restore does not replace)"
            )
        key = self.deletion_ledger_key
        if key is None or len(key.get_secret_value()) < 32:
            raise ValueError(
                "DELETION_LEDGER_KEY must be at least 32 characters outside APP_ENV=local"
            )
        return self

    @model_validator(mode="after")
    def _embedding_and_chunk_settings(self) -> Self:
        if self.chunk_overlap_chars * 2 > self.chunk_size_chars:
            raise ValueError("CHUNK_OVERLAP_CHARS must be at most half of CHUNK_SIZE_CHARS")
        if self.app_env != "local" and self.embedding_provider in (None, "fake"):
            raise ValueError(
                "EMBEDDING_PROVIDER must be set to a real provider (fastembed) outside "
                "APP_ENV=local; the fake provider has no semantic meaning"
            )
        return self

    @model_validator(mode="after")
    def _llm_settings(self) -> Self:
        from app.ai.registry import KNOWN_PROVIDER_IDS

        enabled = self.llm_enabled_provider_list
        if len(set(enabled)) != len(enabled):
            raise ValueError("LLM_ENABLED_PROVIDERS contains duplicates")
        if any(p not in KNOWN_PROVIDER_IDS for p in enabled):
            raise ValueError("LLM_ENABLED_PROVIDERS contains an unknown provider id")
        if self.app_env != "local" and "fake" in enabled:
            raise ValueError("the fake LLM provider may only be enabled with APP_ENV=local")
        if "openai" in enabled:
            is_ollama = self.openai_base_url.host == "ollama.com"
            api_key = self.ollama_api_key if is_ollama else self.openai_api_key
            required_key_name = "OLLAMA_API_KEY" if is_ollama else "OPENAI_API_KEY"
            if api_key is None or not api_key.get_secret_value().strip():
                raise ValueError(
                    f"{required_key_name} is required for the configured OpenAI-compatible endpoint"
                )
        for provider_id, key, key_name in (
            ("groq", self.groq_api_key, "GROQ_API_KEY"),
            ("anthropic", self.anthropic_api_key, "ANTHROPIC_API_KEY"),
            ("gemini", self.gemini_api_key, "GEMINI_API_KEY"),
        ):
            if provider_id in enabled and not _has_secret(key):
                raise ValueError(f"{key_name} is required when the {provider_id} provider is on")
        if any(p not in enabled for p in self.llm_fallback_chain_list):
            raise ValueError("LLM_FALLBACK_CHAIN may only list providers in LLM_ENABLED_PROVIDERS")
        return self

    @model_validator(mode="after")
    def _vision_settings(self) -> Self:
        from app.ai.vision_router import KNOWN_VISION_PROVIDER_IDS

        enabled = self.vision_enabled_provider_list
        if len(set(enabled)) != len(enabled):
            raise ValueError("VISION_ENABLED_PROVIDERS contains duplicates")
        if any(p not in KNOWN_VISION_PROVIDER_IDS for p in enabled):
            raise ValueError("VISION_ENABLED_PROVIDERS contains an unknown provider id")
        if self.app_env != "local" and "fake" in enabled:
            raise ValueError("the fake vision provider may only be enabled with APP_ENV=local")
        if any(p not in enabled for p in self.vision_fallback_chain_list):
            raise ValueError(
                "VISION_FALLBACK_CHAIN may only list providers in VISION_ENABLED_PROVIDERS"
            )
        return self

    @model_validator(mode="after")
    def _stt_settings(self) -> Self:
        from app.ai.stt_router import KNOWN_STT_PROVIDER_IDS

        enabled = self.stt_enabled_provider_list
        if len(set(enabled)) != len(enabled):
            raise ValueError("STT_ENABLED_PROVIDERS contains duplicates")
        if any(p not in KNOWN_STT_PROVIDER_IDS for p in enabled):
            raise ValueError("STT_ENABLED_PROVIDERS contains an unknown provider id")
        if self.app_env != "local" and "fake" in enabled:
            raise ValueError(
                "the fake speech-to-text provider may only be enabled with APP_ENV=local"
            )
        for provider_id, key, key_name in (
            ("openai", self.openai_api_key, "OPENAI_API_KEY"),
            ("groq", self.groq_api_key, "GROQ_API_KEY"),
            ("sarvam", self.sarvam_api_key, "SARVAM_API_KEY"),
            ("gemini", self.gemini_api_key, "GEMINI_API_KEY"),
        ):
            if provider_id in enabled and not _has_secret(key):
                raise ValueError(f"{key_name} is required when {provider_id} voice is enabled")
        return self

    @field_validator(
        "openai_model",
        "groq_model",
        "anthropic_model",
        "gemini_model",
        "openai_stt_model",
        "groq_stt_model",
        "sarvam_stt_model",
        "gemini_stt_model",
    )
    @classmethod
    def _model_not_blank(cls, value: str) -> str:
        model = value.strip()
        if not model:
            raise ValueError("a model setting must not be blank")
        return model

    @property
    def llm_enabled_provider_list(self) -> list[str]:
        return [p.strip() for p in self.llm_enabled_providers.split(",") if p.strip()]

    @property
    def stt_enabled_provider_list(self) -> list[str]:
        return [p.strip() for p in self.stt_enabled_providers.split(",") if p.strip()]

    @property
    def vision_enabled_provider_list(self) -> list[str]:
        return [p.strip() for p in self.vision_enabled_providers.split(",") if p.strip()]

    @property
    def vision_fallback_chain_list(self) -> list[str]:
        return [p.strip() for p in self.vision_fallback_chain.split(",") if p.strip()]

    @property
    def llm_fallback_chain_list(self) -> list[str]:
        return [p.strip() for p in self.llm_fallback_chain.split(",") if p.strip()]

    @property
    def embedding_provider_name(self) -> Literal["fake", "fastembed"]:
        return self.embedding_provider or "fake"

    @property
    def storage_root(self) -> Path:
        return self.file_storage_root or (REPO_ROOT / "app-data")

    @property
    def resume_max_bytes(self) -> int:
        return self.resume_max_upload_mb * 1024 * 1024

    @property
    def document_max_bytes(self) -> int:
        return self.document_max_upload_mb * 1024 * 1024

    @property
    def user_max_storage_bytes(self) -> int:
        return self.user_max_storage_mb * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def require_maintenance_database_url(self) -> str:
        """The privileged operations identity, for the recovery and reindex commands ONLY (SEC-12).

        Ordinary API and worker processes never need it and must not be given it, so it is
        enforced here, where it is used, and not at start-up. Local mode may fall back to the
        runtime URL.
        """
        if self.maintenance_database_url is not None:
            return self.maintenance_database_url
        if self.app_env == "local":
            return self.database_url
        raise ConfigurationError(
            "MAINTENANCE_DATABASE_URL must be set to run this maintenance command outside "
            "APP_ENV=local (use the operations identity; it is not part of the API or worker)."
        )

    @property
    def cookie_secure(self) -> bool:
        """Refresh cookie `Secure` flag: off only for plain-http local development."""
        return self.app_env != "local"


def _describe(error: ValidationError) -> str:
    # Report field names and reasons only; never echo input values (they may be secrets).
    lines = []
    for err in error.errors(include_input=False, include_url=False):
        name = ".".join(str(p) for p in err["loc"]).upper() or "SETTINGS"
        reason = "is required but missing" if err["type"] == "missing" else err["msg"]
        lines.append(f"  - {name}: {reason}")
    return "\n".join(lines)


def load_settings() -> Settings:
    try:
        return Settings()  # values come from the environment
    except ValidationError as exc:
        raise ConfigurationError(
            "Invalid or missing configuration:\n"
            f"{_describe(exc)}\n"
            "Copy .env.example to .env at the repository root and set the values."
        ) from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
