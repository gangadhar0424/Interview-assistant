"""Low-cardinality Prometheus metrics for API and worker operations."""

from prometheus_client import Counter, Histogram

http_requests = Counter(
    "interview_http_requests",
    "Completed HTTP requests",
    ("method", "route", "status"),
)
http_request_duration = Histogram(
    "interview_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ("method", "route"),
)
llm_requests = Counter(
    "interview_llm_requests",
    "LLM provider requests",
    ("provider", "result"),
)
llm_request_duration = Histogram(
    "interview_llm_request_duration_seconds",
    "LLM provider request duration in seconds",
    ("provider",),
)
database_errors = Counter(
    "interview_database_errors",
    "Database operation errors",
)
ingestion_tasks = Counter(
    "interview_ingestion_tasks",
    "Ingestion worker task outcomes",
    ("result",),
)
ingestion_task_duration = Histogram(
    "interview_ingestion_task_duration_seconds",
    "Ingestion worker task duration in seconds",
)
websocket_disconnects = Counter(
    "interview_websocket_disconnects",
    "WebSocket disconnects by normalized close class",
    ("reason",),
)
audio_transcriptions = Counter(
    "interview_audio_transcriptions",
    "Utterance transcription outcomes (status only; never content)",
    ("status",),
)
screenshot_extractions = Counter(
    "interview_screenshot_extractions",
    "Screenshot extraction outcomes (status only; never content)",
    ("status",),
)
answer_grounding_checks = Counter(
    "interview_answer_grounding_checks",
    "Citation-integrity check outcomes for completed answers (not a grounding guarantee)",
    ("status",),
)
answer_routes = Counter(
    "interview_answer_routes",
    "Answer router decisions (D-66): mode and reason only; never content",
    ("mode", "reason"),
)
answer_stage_seconds = Histogram(
    "interview_answer_stage_seconds",
    "Duration of one answer-pipeline stage in seconds",
    ("stage",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)
answer_cache = Counter(
    "interview_answer_cache",
    "Per-session answer cache lookups for general answers",
    ("result",),
)

# --- M-2 performance baseline (timing only; labels are bounded; never content) -----------------
# `provider` values come from the server's own provider registry (an allow-list), never from the
# request body, so the label set is bounded. `none` stands for "no model call" (exact-text answers).
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
stt_request_seconds = Histogram(
    "interview_stt_request_seconds",
    "Speech-to-text provider call duration in seconds",
    ("provider",),
    buckets=_LATENCY_BUCKETS,
)
vision_request_seconds = Histogram(
    "interview_vision_request_seconds",
    "Vision provider call duration in seconds",
    ("provider",),
    buckets=_LATENCY_BUCKETS,
)
audio_validate_seconds = Histogram(
    "interview_audio_validate_seconds",
    "WAV validation duration in seconds",
    buckets=_LATENCY_BUCKETS,
)
screenshot_validate_seconds = Histogram(
    "interview_screenshot_validate_seconds",
    "PNG validation duration in seconds",
    buckets=_LATENCY_BUCKETS,
)
question_detection_seconds = Histogram(
    "interview_question_detection_seconds",
    "Question-detection duration in seconds",
    buckets=(0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5),
)
answer_first_token_seconds = Histogram(
    "interview_answer_first_token_seconds",
    "Question request to first visible answer text, in seconds",
    ("provider",),
    buckets=_LATENCY_BUCKETS,
)
answer_total_seconds = Histogram(
    "interview_answer_total_seconds",
    "Question request to terminal answer outcome, in seconds",
    ("result",),
    buckets=_LATENCY_BUCKETS,
)

_ANSWER_RESULTS = frozenset({"complete", "cancelled", "error"})


def record_answer_timing(
    result: str, provider: str | None, first_token_ms: int | None, total_ms: int | None
) -> None:
    """Observe one terminal answer. Unknown result values are folded into `error` so the label
    set can never grow."""
    label = result if result in _ANSWER_RESULTS else "error"
    if total_ms is not None:
        answer_total_seconds.labels(result=label).observe(total_ms / 1000)
    if first_token_ms is not None:
        answer_first_token_seconds.labels(provider=provider or "none").observe(
            first_token_ms / 1000
        )
