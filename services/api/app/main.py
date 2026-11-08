import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.ai.registry import build_registry
from app.ai.router import LLMRouter, RouterConfig
from app.api import health
from app.api.v1.router import router as v1_router
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, request_id_ctx
from app.db.session import create_engine, create_sessionmaker
from app.embeddings.factory import build_embedding_provider
from app.ingestion.dispatch import DramatiqDispatcher
from app.jobs_import.fetch import SafeFetcher
from app.jobs_import.limiter import SlidingWindowLimiter
from app.jobs_import.service import JobImporter
from app.qa.limits import QuestionLimits
from app.qa.service import QuestionAnswerer
from app.realtime.limits import ConnectionRegistry
from app.realtime.session_channel import SessionChannels
from app.realtime.tickets import TicketStore
from app.storage.local import LocalFileStorage

logger = logging.getLogger("app.request")


def create_app() -> FastAPI:
    settings = get_settings()  # fails fast with a clear ConfigurationError if misconfigured
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)  # lazy: does not connect yet
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.storage = LocalFileStorage(settings.storage_root)  # no I/O until first use
        registry = build_registry(settings.llm_enabled_provider_list)
        app.state.llm_registry = registry
        app.state.llm_router = LLMRouter(
            registry,
            settings.llm_fallback_chain_list or registry.ids(),
            RouterConfig(
                request_timeout_seconds=settings.llm_request_timeout_seconds,
                first_token_timeout_seconds=settings.llm_first_token_timeout_seconds,
                stream_idle_timeout_seconds=settings.llm_stream_idle_timeout_seconds,
                breaker_failure_threshold=settings.llm_breaker_failure_threshold,
                breaker_cooldown_seconds=settings.llm_breaker_cooldown_seconds,
            ),
        )
        # Production wiring always uses the strict default SSRF policy and the system resolver.
        app.state.job_importer = JobImporter(
            SafeFetcher(
                timeout=settings.job_import_timeout_seconds,
                max_bytes=settings.job_import_max_bytes,
                max_redirects=settings.job_import_max_redirects,
            ),
            SlidingWindowLimiter(settings.job_import_rate_limit_per_minute, 60.0),
            max_description_chars=settings.session_job_description_max_chars,
            max_concurrent=settings.job_import_max_concurrent,
            overall_timeout=settings.job_import_timeout_seconds,
        )
        # Typed Q&A (Phase 8B). The embedding model loads lazily on the first question (never at
        # startup); the generator opens its own short database sessions from this sessionmaker.
        app.state.question_answerer = QuestionAnswerer(
            app.state.sessionmaker,
            settings,
            app.state.llm_router,
            build_embedding_provider(settings),
        )
        app.state.question_limits = QuestionLimits(
            settings.qa_rate_limit_per_minute, settings.qa_max_concurrent_generations
        )
        app.state.dispatcher = DramatiqDispatcher()  # imports the queue lazily on first dispatch
        # WebSocket gateway (Phase 9A, D-41). Tickets always live in Redis (the client connects
        # lazily, so startup needs no Redis); limits are per process.
        ticket_redis = aioredis.from_url(  # type: ignore[no-untyped-call]
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        app.state.ticket_store = TicketStore(ticket_redis, settings.ws_ticket_ttl_seconds)
        app.state.ws_ticket_limiter = SlidingWindowLimiter(
            settings.ws_ticket_rate_limit_per_minute, 60.0
        )
        app.state.connection_registry = ConnectionRegistry(
            settings.ws_max_connections_per_user, settings.ws_max_total_connections
        )
        # One channel per (user, session): retained events, the grace timers and the generation
        # that outlives a dropped socket (Phase 9C, D-43).
        session_channels = SessionChannels(
            max_events=settings.ws_resume_buffer_events,
            grace_seconds=settings.ws_resume_grace_seconds,
            max_detached=settings.ws_max_total_connections,
        )
        app.state.session_channels = session_channels
        yield
        await session_channels.shutdown()  # cancel detached generations; their answers are stored
        await ticket_redis.aclose()
        await engine.dispose()

    app = FastAPI(title="Interview Assistant API", version="0.2.0", lifespan=lifespan)
    register_error_handlers(app)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                },
            )
            return response
        finally:
            request_id_ctx.reset(token)

    # Explicit origins only (never "*"): credentials are allowed for the refresh cookie.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    # Outermost: rejects oversized upload bodies before they are spooled to disk. The extra 1 MiB
    # covers multipart framing; the exact per-file limit is enforced again while streaming.
    mib = 1024 * 1024
    app.add_middleware(
        BodySizeLimitMiddleware,
        limits={
            "/api/v1/resumes": settings.resume_max_bytes + mib,
            "/api/v1/documents": settings.document_max_bytes + mib,
        },
    )

    app.include_router(health.router)
    app.include_router(v1_router, prefix="/api/v1")
    return app


def get_app() -> FastAPI:
    """Factory entrypoint for uvicorn: `uvicorn app.main:get_app --factory`."""
    return create_app()
