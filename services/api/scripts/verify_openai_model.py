"""Local, no-secret check of the configured OpenAI models against the adapter's shape (D-57).

Run from services/api:   uv run python scripts/verify_openai_model.py [--model M] [--stt-model M]

It answers one question: can the configured reasoning model use the Chat Completions path the
adapter already uses (non-streaming and streaming, with `temperature` and `max_completion_tokens`)?
If not, a Responses API path is justified; if so, it is not. It also probes the transcription model.

Secrets: the key is read from the OPENAI_API_KEY environment variable or the repo's `.env` (that
one key only) and is never printed, logged or written anywhere. Only PASS/FAIL, HTTP status, the
vendor's short error `code` and a classification are printed: never vendor messages, which could
echo input.
Each probe sends a tiny synthetic prompt (no interview data) and costs a fraction of a cent.
"""

import argparse
import asyncio
import io
import os
import re
import sys
import wave
from pathlib import Path

from openai import APIError, APIStatusError, AsyncOpenAI

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPT = [{"role": "user", "content": "Reply with the single word: ok"}]


def read_setting(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value.strip()
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        match = re.search(rf"^{name}=(.*)$", env_file.read_text(encoding="utf-8"), re.MULTILINE)
        if match and match.group(1).strip():
            return match.group(1).strip().strip("\"'")
    return None


def silent_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def classify(error: APIError) -> str:
    """A short, content-free reason. The vendor message is inspected locally and never printed."""
    status = error.status_code if isinstance(error, APIStatusError) else None
    code = getattr(error, "code", None)
    text = str(getattr(error, "message", "")).lower()
    if status in (401, 403):
        return f"auth problem (status {status}): key missing/invalid/not permitted"
    if "v1/responses" in text or "responses api" in text:
        return f"model needs the Responses API (status {status}, code {code})"
    if code == "unsupported_parameter" or code == "unsupported_value" or "temperature" in text:
        return f"unsupported request parameter (status {status}, code {code})"
    if status == 404 or code == "model_not_found":
        return f"model not found / no access (status {status}, code {code})"
    return f"failed (status {status}, code {code})"


async def probe(name: str, call: object) -> tuple[bool, str]:
    try:
        await call  # type: ignore[misc]
    except APIError as error:
        reason = classify(error)
        print(f"  FAIL  {name}: {reason}")
        return False, reason
    print(f"  PASS  {name}")
    return True, ""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=read_setting("OPENAI_MODEL") or "gpt-4o-mini")
    parser.add_argument(
        "--stt-model", default=read_setting("OPENAI_STT_MODEL") or "gpt-4o-mini-transcribe"
    )
    args = parser.parse_args()
    key = read_setting("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set in the environment or .env; nothing was called.")
        return 2
    client = AsyncOpenAI(api_key=key, timeout=30.0, max_retries=0)
    print(f"Reasoning model: {args.model}   Transcription model: {args.stt_model}")

    async def chat() -> None:
        r = await client.chat.completions.create(
            model=args.model,
            messages=PROMPT,
            temperature=0.3,
            max_completion_tokens=64,  # type: ignore[arg-type]
        )
        if not r.choices:
            raise RuntimeError("no choices")

    async def stream() -> None:
        s = await client.chat.completions.create(
            model=args.model,
            messages=PROMPT,  # type: ignore[arg-type]
            temperature=0.3,
            max_completion_tokens=64,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for _ in s:
            pass

    async def transcribe() -> None:
        await client.audio.transcriptions.create(
            model=args.stt_model, file=("audio.wav", silent_wav(), "audio/wav"), language="en"
        )

    print("Chat Completions path (what the adapter uses):")
    results = [
        await probe("non-streaming, temperature + max_completion_tokens", chat()),
        await probe("streaming with usage", stream()),
    ]
    print("Transcription path:")
    stt_ok, _ = await probe("audio/transcriptions", transcribe())
    await client.close()

    chat_ok = all(ok for ok, _ in results)
    needs_responses = any("Responses API" in reason for _, reason in results)
    print()
    if chat_ok:
        print(
            "RESULT: the existing Chat Completions path works; a Responses API path is NOT needed."
        )
    elif needs_responses:
        print("RESULT: the model cannot use Chat Completions; a Responses API path IS justified.")
    else:
        print("RESULT: Chat Completions failed for another reason (see above); fix that first. ")
        print("        This does not by itself justify a Responses API path.")
    if not stt_ok:
        print("RESULT: the transcription probe failed; see above.")
    return 0 if chat_ok and stt_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
