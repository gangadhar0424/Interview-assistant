"""Environment-based configuration. Fails fast with a safe, useful message if misconfigured."""

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# services/api/app/core/config.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[4]

MIN_JWT_SECRET_LENGTH = 43
PLACEHOLDER_SECRET_PREFIX = "change-me"  # noqa: S105
_JWT_SECRET_CHARS = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid (never includes values)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: str = Field(min_length=1)
    maintenance_database_url: str | None = None
    redis_url: str = Field(min_length=1)

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

    @field_validator("embedding_cache_dir", "embedding_provider", mode="before")
    @classmethod
    def _blank_embedding_value_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("file_storage_root", mode="before")
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
        if self.app_env != "local" and self.maintenance_database_url is None:
            raise ValueError(
                "MAINTENANCE_DATABASE_URL must be set outside APP_ENV=local for recovery jobs"
            )
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

    @field_validator("openai_model")
    @classmethod
    def _openai_model_not_blank(cls, value: str) -> str:
        model = value.strip()
        if not model:
            raise ValueError("OPENAI_MODEL must not be blank")
        return model

    @property
    def llm_enabled_provider_list(self) -> list[str]:
        return [p.strip() for p in self.llm_enabled_providers.split(",") if p.strip()]

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
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

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
