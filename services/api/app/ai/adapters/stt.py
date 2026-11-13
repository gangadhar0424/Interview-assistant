"""Speech-to-text adapters for the `STTProvider` port (D-57): OpenAI, Groq Whisper and Sarvam.

OpenAI and Groq share the OpenAI-compatible `/audio/transcriptions` endpoint (one class, different
base URL, key and model). Sarvam has its own REST endpoint and is called with plain `httpx`.

Privacy: audio and transcripts never appear in logs, errors or exception text. Every failure is
mapped to a fixed `LLMError`; vendor messages (which could echo request content) are dropped.
"""

import httpx
from openai import APIError, AsyncOpenAI

from app.ai.adapters.openai import map_openai_error
from app.ai.errors import AuthError, InvalidSelection, LLMError, RateLimited, Unavailable
from app.ai.errors import Timeout as ProviderTimeout
from app.ai.stt import AudioInput, SttResult, SttSpec

OPENAI_STT_PROVIDER_ID = "openai"
GROQ_STT_PROVIDER_ID = "groq"
SARVAM_STT_PROVIDER_ID = "sarvam"
DEFAULT_OPENAI_STT_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_GROQ_STT_MODEL = "whisper-large-v3-turbo"
DEFAULT_SARVAM_STT_MODEL = "saarika:v2.5"
OPENAI_BASE_URL = "https://api.openai.com/v1"
GROQ_STT_BASE_URL = "https://api.groq.com/openai/v1"
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"

# The product's languages as each vendor spells them. Unknown -> let the vendor detect.
_ISO = {"en": "en", "hi": "hi", "te": "te"}
_SARVAM_LANGUAGE = {"en": "en-IN", "hi": "hi-IN", "te": "te-IN"}


def stt_spec(provider_id: str, model: str, label: str) -> SttSpec:
    return SttSpec(provider_id, model, label)


class OpenAICompatibleSTT:
    """`/audio/transcriptions` via the OpenAI SDK; serves OpenAI and Groq."""

    def __init__(
        self,
        provider_id: str,
        api_key: str,
        model: str,
        timeout: float,
        *,
        base_url: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._model = model
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )

    async def transcribe(self, audio: AudioInput, language: str | None, model: str) -> SttResult:
        if model != self._model:
            raise InvalidSelection()
        try:
            response = await self._client.audio.transcriptions.create(
                model=model,
                file=("audio.wav", audio.data, audio.mime),
                language=_ISO.get(language or "") or "",
                response_format="json",
            )
        except APIError as error:
            raise map_openai_error(error) from None
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise Unavailable()
        return SttResult(text)

    async def aclose(self) -> None:
        await self._client.close()


class SarvamSTT:
    """Sarvam `speech-to-text` (multipart `file`, `model`, `language_code`)."""

    provider_id = SARVAM_STT_PROVIDER_ID

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float,
        *,
        url: str = SARVAM_STT_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = url
        self._client = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = http_client is None

    async def transcribe(self, audio: AudioInput, language: str | None, model: str) -> SttResult:
        if model != self._model:
            raise InvalidSelection()
        try:
            response = await self._client.post(
                self._url,
                headers={"api-subscription-key": self._api_key},
                data={
                    "model": model,
                    "language_code": _SARVAM_LANGUAGE.get(language or "", "unknown"),
                },
                files={"file": ("audio.wav", audio.data, audio.mime)},
            )
        except httpx.TimeoutException:
            raise ProviderTimeout() from None
        except httpx.HTTPError:
            raise Unavailable() from None
        raise_for_status(response.status_code)
        try:
            body = response.json()
            text = body["transcript"]
        except (ValueError, KeyError, TypeError):
            raise Unavailable() from None
        if not isinstance(text, str):
            raise Unavailable()
        return SttResult(text)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def raise_for_status(status: int) -> None:
    """Map an HTTP status to the fixed error taxonomy (no body is ever inspected or surfaced)."""
    error: LLMError | None = None
    if status in (401, 403):
        error = AuthError()
    elif status == 429:
        error = RateLimited()
    elif status in (408, 504):
        error = ProviderTimeout()
    elif status >= 400:
        error = Unavailable()
    if error is not None:
        raise error
