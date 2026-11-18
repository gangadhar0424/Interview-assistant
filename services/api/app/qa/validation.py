"""Validation of general-knowledge answers before they are sent (D-66).

A general answer is produced when neither the resume nor the selected documents cover the question.
The writer has no facts about the candidate on this path, so the answer must not claim any. Checks,
cheapest first, stop at the first failure:

1. Deterministic (milliseconds): not empty, not runaway-long, no leaked evidence tags or prompt
   delimiters, no e-mail/phone echoed, the language matches the session, no first-person claim
   about the candidate's own jobs, projects or years.
2. A fast judge model (hard time cap): on-topic, not clearly wrong, not unsafe, no candidate claim.
   It sees only the question and the draft answer, never a document.

If the judge cannot answer in time, the deterministic result stands and the caller labels the answer
as not fully validated. Validation lowers the chance of a bad answer; it cannot prove that general
knowledge is correct, which is why the source is always shown to the user.
"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from app.ai.errors import LLMError
from app.ai.router import LLMRouter, Selection
from app.ai.types import GenerationConfig
from app.preferences.enums import Language
from app.preferences.prompt_builder import build_judge_messages

REASON_OK = "ok"
REASON_EMPTY = "empty"
REASON_TOO_LONG = "too_long"
REASON_LEAK = "leak"
REASON_PII = "pii"
REASON_LANGUAGE = "wrong_language"
REASON_CANDIDATE_CLAIM = "candidate_claim"
REASON_OFF_TOPIC = "off_topic"
REASON_INCORRECT = "incorrect"
REASON_UNSAFE = "unsafe"

_TAG = re.compile(r"\[\s*E\s*\d+|<<<|>>>|UNTRUSTED", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)")
# First-person claims about the candidate's own career. A general answer has no such facts.
_CLAIM = re.compile(
    r"\bI(?:'ve| have| had| was| am)?\s+(?:been\s+)?"
    r"(?:worked|working|built|developed|designed|led|lead|managed|implemented|delivered|handled|"
    r"served|joined|created|launched|deployed|migrated|owned)\b"
    r"|\bI(?:'ve| have)\s+\d+\+?\s+years\b"
    r"|\b(?:in|at|with|during)\s+my\s+(?:current|previous|last|recent|past|earlier|first)\s+"
    r"(?:role|job|company|project|team|position|organi[sz]ation|employer)\b"
    r"|\bmy\s+(?:current|previous|last|recent)\s+(?:employer|company|project|role)\b"
    r"|\bin\s+my\s+\d+\s+years\b",
    re.IGNORECASE,
)
_MAX_WORD_FACTOR = 2.5

_DEVANAGARI = (0x0900, 0x097F)
_TELUGU = (0x0C00, 0x0C7F)


@dataclass(frozen=True, slots=True)
class Verdict:
    ok: bool
    reason: str = REASON_OK
    judged: bool = False  # True when the judge model gave the verdict (False: skipped/unavailable)


def _script_share(text: str, lo: int, hi: int) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if lo <= ord(c) <= hi) / len(letters)


def language_matches(text: str, language: Language) -> bool:
    if language is Language.HI:
        return _script_share(text, *_DEVANAGARI) >= 0.3
    if language is Language.TE:
        return _script_share(text, *_TELUGU) >= 0.3
    # English: mostly Latin letters (some foreign words or names are fine)
    non_latin = 1.0 - sum(1 for c in text if c.isalpha() and ord(c) < 0x250) / max(
        1, sum(1 for c in text if c.isalpha())
    )
    return non_latin <= 0.3


def check_deterministic(text: str, *, language: Language, max_words: int) -> Verdict:
    """The fast, rule-based checks. No model is called."""
    body = text.strip()
    if not body:
        return Verdict(False, REASON_EMPTY)
    if max_words > 0 and len(body.split()) > max_words * _MAX_WORD_FACTOR:
        return Verdict(False, REASON_TOO_LONG)
    if _TAG.search(body):
        return Verdict(False, REASON_LEAK)
    if _EMAIL.search(body) or _PHONE.search(body):
        return Verdict(False, REASON_PII)
    if not language_matches(body, language):
        return Verdict(False, REASON_LANGUAGE)
    if language is Language.EN and _CLAIM.search(body):
        return Verdict(False, REASON_CANDIDATE_CLAIM)
    return Verdict(True)


def parse_judge_reply(raw: str) -> Verdict | None:
    """The judge's JSON verdict, or None when the reply cannot be understood."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data: Any = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    reason = data.get("reason")
    if verdict == "pass":
        return Verdict(True, REASON_OK, judged=True)
    if verdict == "fail":
        known = {
            REASON_EMPTY,
            REASON_CANDIDATE_CLAIM,
            REASON_OFF_TOPIC,
            REASON_INCORRECT,
            REASON_UNSAFE,
        }
        return Verdict(False, reason if reason in known else REASON_OFF_TOPIC, judged=True)
    return None


async def judge_answer(
    router: LLMRouter,
    selection: Selection | None,
    question: str,
    answer: str,
    *,
    timeout_seconds: float,
    user_id: uuid.UUID,
) -> Verdict:
    """Ask the fast judge. Any failure, timeout or unreadable reply is "not judged", never a crash
    and never a silent pass presented as a judged one."""
    try:
        async with asyncio.timeout(timeout_seconds):
            completion = await router.complete(
                build_judge_messages(question, answer),
                GenerationConfig(temperature=0.0, max_output_tokens=120),
                selection=selection,
                user_id=user_id,
            )
    except (TimeoutError, LLMError):
        return Verdict(True, "unavailable", judged=False)
    except Exception:  # noqa: BLE001 - an auxiliary check must never break the answer
        return Verdict(True, "unavailable", judged=False)
    parsed = parse_judge_reply(completion.text)
    return parsed if parsed is not None else Verdict(True, "unavailable", judged=False)
