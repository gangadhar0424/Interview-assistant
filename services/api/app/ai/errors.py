"""Unified LLM error taxonomy. Messages are fixed, user-safe strings: never provider details."""


class LLMError(Exception):
    code = "llm_error"
    message = "The AI service failed."
    # Whether the router may try the next provider after this failure.
    fallback_eligible = False
    # Whether this failure says something about the provider's health (feeds the circuit breaker).
    counts_against_provider = False

    def __init__(self) -> None:
        super().__init__(self.code)


class RateLimited(LLMError):
    code = "llm_rate_limited"
    message = "The AI service is busy. Please try again shortly."
    fallback_eligible = True
    counts_against_provider = True


class Timeout(LLMError):
    code = "llm_timeout"
    message = "The AI service took too long to respond."
    fallback_eligible = True
    counts_against_provider = True


class Unavailable(LLMError):
    code = "llm_unavailable"
    message = "The AI service is unavailable."
    fallback_eligible = True
    counts_against_provider = True


class AuthError(LLMError):
    """Server-side credential problem with one provider (never the end user's fault)."""

    code = "llm_unavailable"  # users only ever see a generic availability error
    message = "The AI service is unavailable."
    fallback_eligible = True
    counts_against_provider = True


class ContentFiltered(LLMError):
    """The provider refused the content. Not retried elsewhere (no shopping for a laxer filter)."""

    code = "llm_content_filtered"
    message = "The AI service could not process this request."


class InvalidSelection(LLMError):
    """The requested provider/model is not enabled or not allowed."""

    code = "invalid_provider_selection"
    message = "The selected AI provider or model is not available."
