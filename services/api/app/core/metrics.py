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
