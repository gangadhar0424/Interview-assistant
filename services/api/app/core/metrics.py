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
