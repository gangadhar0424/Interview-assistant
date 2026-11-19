# ruff: noqa: E501, RUF001
"""Deterministic question detection for transcribed utterances (Phase 12, D-53; extended in M-7, D-73).

Pure and dependency free: the same input always gives the same answer, in tens of microseconds, with no
model or network call. It is ADVISORY. The desktop shows a "Question?" flag, and (D-59) answers a flagged
utterance at once only because the user pressed Listen; anything not flagged waits for the Ask button.
A false positive therefore costs an answer, so the rules favour precision.

English (the original rules, unchanged in structure): very short text is never a question;
acknowledgements and fillers are not; leading discourse markers are skipped; then a question word or
request opener at the start, and a trailing question mark, add confidence. M-7 additions, each justified
by failures measured on the development split (docs/QUESTION_DETECTION_EVALUATION.md):
  - indirect openers ("i was wondering how...", "i'd like to understand how...", "let's talk about...");
  - task imperatives ("write a function...", "implement an lru cache...");
  - not a question: a question word followed by "i/we/he/she/they" ("what i did was..."), a procedural
    request ("can you turn on your camera"), small talk ("how are you doing"), and an utterance cut off
    before it says what is being asked ("so what is the difference between").

Hindi and Telugu (Devanagari and Telugu script, with English terms mixed in) and romanised Hinglish /
Tenglish: a small lexicon of question words, polite request forms and the Telugu yes/no ending, with
cues that mark an embedded or reported question ("I know what you mean", "he asked why...") and
procedural requests ("turn on your camera"). It is a lexicon heuristic, not a language model: it has had
NO native-speaker review and no real speech.

Every reason is a fixed code, never text from the utterance.
"""

import re
from dataclasses import dataclass

from app.preferences.sanitize import clean_text

QUESTION_THRESHOLD = 0.6

_WORDS = re.compile(r"[a-z0-9']+")

# Words that may start an utterance without changing its meaning.
_LEADING_MARKERS = frozenset(
    {"so", "okay", "ok", "alright", "right", "and", "well", "now", "next", "then", "great",
     "good", "perfect", "thanks", "thank", "you", "um", "uh", "hmm", "yeah", "yes", "sure",
     "also", "but", "last", "final", "finally", "one", "more", "thing", "quick", "question",
     "maybe", "perhaps"}
)  # fmt: skip

_WH = frozenset({"what", "why", "how", "when", "where", "who", "whom", "whose", "which"})
_AUX = frozenset(
    {"can", "could", "would", "will", "should", "do", "does", "did", "is", "are", "was", "were",
     "have", "has", "had", "am"}
)  # fmt: skip
_REQUEST_OPENERS = (
    ("tell", "me"),
    ("tell", "us"),
    ("describe",),
    ("explain",),
    ("walk", "me", "through"),
    ("walk", "us", "through"),
    ("talk", "me", "through"),
    ("talk", "about"),
    ("give", "me"),
    ("share",),
    ("elaborate",),
    ("i", "would", "like", "you", "to"),
    ("i'd", "like", "you", "to"),
    ("i", "want", "you", "to"),
    # M-7: polite and conversational ways of asking the candidate to tell something.
    ("let's", "talk", "about"),
    ("lets", "talk", "about"),
    ("let", "us", "talk", "about"),
    ("you", "could", "tell", "me"),
    ("you", "can", "tell", "me"),
    ("if", "you", "could", "tell", "me"),
    ("if", "you", "can", "tell", "me"),
)

# M-7: "i was wondering how...", "i'd like to understand why...": an intent phrase plus a question word.
_INTENT_OPENERS = (
    ("i", "was", "wondering"),
    ("i", "am", "wondering"),
    ("i'm", "wondering"),
    ("i'd", "like", "to", "understand"),
    ("i'd", "like", "to", "know"),
    ("i'd", "like", "to", "hear"),
    ("i", "would", "like", "to", "understand"),
    ("i", "would", "like", "to", "know"),
    ("i", "would", "like", "to", "hear"),
    ("i", "am", "interested", "in", "knowing"),
    ("i", "am", "interested", "in", "hearing"),
    ("i'm", "interested", "in", "knowing"),
    ("i'm", "interested", "in", "hearing"),
    ("i'd", "be", "curious"),
    ("i", "am", "curious"),
    ("i'm", "curious"),
    ("i", "want", "to", "know"),
    ("i", "want", "to", "understand"),
    ("curious", "to", "hear"),
)  # fmt: skip

# M-7: imperatives that give the candidate a task ("write a function ...", "design a url shortener").
_TASK_VERBS = frozenset(
    {"write", "implement", "design", "build", "develop", "create", "code", "solve", "optimize",
     "refactor", "debug", "sketch", "draw"}
)  # fmt: skip
_TASK_OBJECT_STARTS = frozenset(
    {"a", "an", "the", "me", "us", "your", "this", "that", "some", "one", "two", "three"}
)

# M-7: "can you turn on your camera" is a request about the call, not a question for the candidate.
_PROCEDURAL_VERBS = frozenset(
    {"turn", "mute", "unmute", "share", "open", "close", "send", "hear", "see", "repeat", "scroll",
     "zoom", "click", "enable", "disable", "switch", "hold", "wait", "pause"}
)  # fmt: skip

_SMALL_TALK = (("how", "are", "you"), ("how", "is", "it", "going"), ("how", "have", "you", "been"))
_RELATIVE_SUBJECTS = frozenset({"i", "we", "he", "she", "they"})

# M-7: an utterance that stops on one of these has not finished asking ("what is the difference between").
_DANGLING_LAST = frozenset(
    {"the", "a", "an", "of", "to", "and", "or", "when", "between", "if", "whether", "because", "than",
     "your", "my", "our", "their", "his", "her", "its"}
)  # fmt: skip
_DANGLING_LAST_TWO = frozenset(
    {
        ("when", "you"),
        ("if", "you"),
        ("that", "you"),
        ("because", "you"),
        ("so", "you"),
        ("go", "about"),
        ("going", "about"),
    }
)

_ACKNOWLEDGEMENTS = frozenset(
    {"okay", "ok", "right", "thanks", "thank", "you", "sure", "yes", "no", "yeah", "yep", "nope",
     "got", "it", "sounds", "good", "great", "nice", "alright", "i", "see", "mm", "mhm", "hmm",
     "uh", "um", "perfect", "cool", "wonderful", "awesome", "fine", "welcome", "bye", "goodbye",
     "hello", "hi", "hey", "morning", "afternoon", "evening", "please", "excuse", "me", "sorry",
     "pardon", "understood", "makes", "sense", "that", "s", "thats", "is", "all", "for", "now",
     "later"}
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Detection:
    is_question: bool
    confidence: float
    reason: str  # a fixed code, never text from the utterance


def _starts_with(words: list[str], opener: tuple[str, ...]) -> bool:
    return tuple(words[: len(opener)]) == opener


def _contains_run(words: list[str], run: tuple[str, ...]) -> bool:
    n = len(run)
    return any(tuple(words[i : i + n]) == run for i in range(len(words) - n + 1))


# ---------------------------------------------------------------------------------------- English


def _classify_english(words: list[str], has_mark: bool) -> Detection:
    if len(words) < 3:
        return Detection(False, 0.0, "too_short")
    if all(word in _ACKNOWLEDGEMENTS for word in words) and not has_mark:
        return Detection(False, 0.0, "acknowledgement")
    core = list(words)
    while len(core) > 3 and core[0] in _LEADING_MARKERS:
        core.pop(0)

    # Not questions, whatever else they look like (M-7). A question mark overrides all of them.
    if not has_mark:
        if len(core) >= 2 and core[0] in _WH and core[1] in _RELATIVE_SUBJECTS:
            return Detection(False, 0.2, "statement")  # "what i did was ...", "who i am today ..."
        if (
            len(core) >= 3
            and core[0] in {"can", "could", "would", "will"}
            and core[1] == "you"
            and core[2] in _PROCEDURAL_VERBS
        ):
            return Detection(False, 0.2, "statement")  # "can you turn on your camera"
        if len(words) <= 8 and any(_contains_run(words, run) for run in _SMALL_TALK):
            return Detection(False, 0.2, "statement")  # "how are you doing today"
        if words[-1] in _DANGLING_LAST or tuple(words[-2:]) in _DANGLING_LAST_TWO:
            return Detection(False, 0.2, "statement")  # cut off before it says what is asked

    wh = core[0] in _WH
    aux = core[0] in _AUX and len(core) >= 4
    request = any(_starts_with(core, opener) for opener in _REQUEST_OPENERS)
    intent = (
        any(_starts_with(core, opener) for opener in _INTENT_OPENERS)
        and len(core) >= 5
        and any(word in _WH or word in {"if", "whether"} for word in core[3:])
    )
    task = (
        len(core) >= 4
        and core[0] in _TASK_VERBS
        and core[1] in _TASK_OBJECT_STARTS
        or (len(core) >= 6 and core[0] == "given" and core[1] in _TASK_OBJECT_STARTS)
    )
    opener = wh or aux or request or intent or task
    if opener and has_mark:
        return Detection(True, 0.95, "opener_and_mark")
    if wh and len(core) >= 4:
        return Detection(True, 0.8, "question_word")
    if request or intent or task:
        return Detection(True, 0.8, "request")
    if aux and len(core) >= 5:
        return Detection(True, 0.7, "auxiliary_opener")
    if has_mark and len(core) >= 4:
        return Detection(True, 0.6, "question_mark")
    return Detection(False, 0.2 if opener else 0.0, "statement")


# ------------------------------------------------------------------------- Hindi / Telugu (and romanised)

_INDIC_SCRIPT = re.compile("[ऀ-ॿఀ-౿]")
_TOKEN_SPLIT = re.compile(r"[\s,.;:!?\"'()\[\]{}।॥/\\\-–—]+")
_STRIP = dict.fromkeys(map(ord, "​‌‍़"))  # zero-width marks and the nukta
_CANDRABINDU = str.maketrans({"ँ": "ं"})  # chandrabindu -> anusvara (कहाँ / कहां)


def _norm(token: str) -> str:
    return token.lower().translate(_STRIP).translate(_CANDRABINDU)


def _set(*items: str) -> frozenset[str]:
    return frozenset(_norm(i) for i in items)


_HI_WH = _set(
    "क्या", "कैसे", "कैसा", "कैसी", "क्यों", "क्यूँ", "कब", "कहाँ", "कौन", "किस", "किसने", "किसके", "किसकी",
    "किसको", "कितना", "कितने", "कितनी", "कौनसा", "कौनसी", "कौनसे", "किधर", "किसलिए",
)  # fmt: skip
_HI_STRONG_WH = _HI_WH - _set("क्या")  # "क्या" alone is also the yes/no particle and an exclamation
_HI_POLITE_IMPERATIVE = re.compile("(ाइए|ाइये|ीजिए|ीजिये|ोजिए|ोजिये|िखिए|िखिये|ोलिए|ोलिये|ुनिए|ुनिये)$")
_HI_REQUEST_WORDS = _set("बताएँ", "बताएं", "बताओ", "समझाओ", "वर्णन", "कहिए", "कहिये")
_HI_INTENT = _set("जानना", "जानने", "समझना", "समझने", "सुनना", "सुनने")
_HI_PROCEDURAL = _set(
    "कैमरा", "माइक", "म्यूट", "स्क्रीन", "शेयर", "चैट", "लिंक", "रुकिए", "रुकिये", "ठहरिए", "सेकंड"
)
_HI_EMBED_CUES = _set("पता", "मालूम", "बताऊँगा", "बताऊंगा", "बताता", "बताती", "पूछा")
_HI_DANGLING_LAST = _set("कि", "जब", "क्योंकि", "का", "की", "के", "और", "या", "अगर", "तब")
# "sorry / excuse me" and "let me know" use polite imperative forms but ask nothing of the candidate.
_HI_NOT_REQUEST_PAIRS = frozenset(
    {
        (_norm("माफ"), _norm("कीजिए")), (_norm("माफ"), _norm("कीजिये")),
        (_norm("क्षमा"), _norm("कीजिए")), (_norm("क्षमा"), _norm("करें")),
        (_norm("बता"), _norm("दीजिए")), (_norm("बता"), _norm("दीजिये")),
    }
)  # fmt: skip
_HI_DANGLING_PAIRS = frozenset({(_norm("जब"), _norm(p)) for p in ("आप", "मैं", "हम", "वह")})

_TE_WH = _set(
    "ఏమిటి", "ఏంటి", "ఏమి", "ఎలా", "ఎందుకు", "ఎప్పుడు", "ఎక్కడ", "ఎవరు", "ఎవరిని", "ఎవరి", "ఏది", "ఏ", "ఎంత",
    "ఎన్ని", "ఎప్పటి", "ఏవి", "ఎటు", "ఎందుకని",
)  # fmt: skip
_TE_NOT_REQUEST = _set("క్షమించండి", "ఆగండి", "అవునండి", "సరేనండి", "రండి", "ధన్యవాదాలండి")
_TE_INTENT = re.compile("ాలని$")  # తెలుసుకోవాలని / వినాలని / చేసుకోవాలని ("wanting to know / hear")
_TE_PROCEDURAL_NOUNS = _set("స్క్రీన్", "షేర్", "కెమెరా", "మైక్", "మ్యూట్", "ఆన్", "ఓపెన్", "లింక్", "చాట్")
_TE_EMBED_CUES = _set(
    "తెలుసు", "తెలియదు", "చెబుతాను", "చెప్తాను", "చెప్పాను", "చెబుతా", "చెప్తా", "అడిగాడు", "అడిగింది", "అడిగితే"
)
_TE_DANGLING_PAIRS = frozenset({(_norm("అప్పుడు"), _norm(p)) for p in ("మీరు", "నేను", "మేము")})

_MANNER_WH = _set("कैसे", "ఎలా")
_SMALL_TALK_INDIC = (
    tuple(_norm(w) for w in ("आप", "कैसे", "हैं")),
    tuple(_norm(w) for w in ("आप", "कैसे", "हो")),
    tuple(_norm(w) for w in ("ఎలా", "ఉన్నారు")),
)
_HI_LETS_TALK = (_norm("चलिए"), _norm("बात"))

_ROMAN_WH = frozenset({"kya", "kaise", "kyun", "kyon", "kyu", "kab", "kahan", "kaun", "kitna", "kitne", "kitni",
                       "konsa", "enti", "emiti", "ela", "enduku", "eppudu", "ekkada", "evaru", "entha", "enni"})  # fmt: skip
_ROMAN_REQUEST = frozenset({"bataiye", "bataiyen", "batao", "batayiye", "samjhaiye", "samjhao", "batayen",
                            "cheppandi", "vivarinchandi", "cheppagalara", "vivarinchagalara"})  # fmt: skip
_ROMAN_YES_NO = frozenset({"chesara", "unda", "telusa"})
_ROMAN_EMBED_CUES = frozenset(
    {"bataunga", "bataungi", "pata", "chebutanu", "cheptanu", "chebuta", "pucha"}
)


def _classify_indic(tokens: list[str], has_mark: bool) -> Detection:
    if len(tokens) < 3:
        return Detection(False, 0.0, "too_short")
    toks = [_norm(t) for t in tokens]
    last = toks[-1]

    # Not questions, whatever else they look like. A question mark overrides all of them.
    if not has_mark:
        if any(_contains_run(toks, run) for run in _SMALL_TALK_INDIC) and len(toks) <= 7:
            return Detection(False, 0.2, "statement")  # "आप कैसे हैं", "ఎలా ఉన్నారు"
        if (
            last in _HI_DANGLING_LAST
            or tuple(toks[-2:]) in _HI_DANGLING_PAIRS
            or tuple(toks[-2:]) in _TE_DANGLING_PAIRS
        ):
            return Detection(False, 0.2, "statement")  # cut off ("...जब आप", "...అప్పుడు మీరు")
        if any(t.endswith("ంటే") for t in toks[-2:]):
            return Detection(
                False, 0.2, "statement"
            )  # Telugu "...ఏమిటంటే" ("that is...") with nothing after it
    if not has_mark and last in _MANNER_WH and len(toks) <= 3:
        return Detection(False, 0.2, "statement")  # "आप इसे कैसे": a how-question with no verb yet
    yes_no_ending = last.endswith("ా")  # Telugu question ending (సిద్ధంగా ఉన్నారా, చెప్పగలరా)

    hi_wh_positions = [
        i
        for i, t in enumerate(toks)
        if t in _HI_WH
        and not (i + 1 < len(toks) and toks[i + 1] == _norm("भी"))  # "कैसे भी" = "somehow"
    ]
    if toks[:3] == [_norm("क्या"), _norm("बात"), _norm("है")]:
        hi_wh_positions = []  # "क्या बात है": an exclamation
    te_wh = any(t in _TE_WH for t in toks)
    hi_wh = bool(hi_wh_positions)
    wh = hi_wh or te_wh
    strong_wh = any(toks[i] in _HI_STRONG_WH for i in hi_wh_positions) or te_wh

    not_request = {
        i
        for i, pair in enumerate(zip(toks, toks[1:], strict=False))
        if pair in _HI_NOT_REQUEST_PAIRS
    }
    hi_request = any(
        (_HI_POLITE_IMPERATIVE.search(t) or t in _HI_REQUEST_WORDS) and (i - 1) not in not_request
        for i, t in enumerate(toks)
    )
    hi_request = hi_request or (
        len(toks) >= 2 and toks[0] == _HI_LETS_TALK[0] and _HI_LETS_TALK[1] in toks
    )
    hi_intent = any(t in _HI_INTENT for t in toks) and any(t.startswith(_norm("चाह")) for t in toks)
    te_request = any(t.endswith("ండి") and t not in _TE_NOT_REQUEST for t in toks)
    te_request = te_request or any(t.endswith("ుకుందాం") for t in toks)
    te_intent = any(_TE_INTENT.search(t) for t in toks) and any(t in _set("ఉంది") for t in toks)
    request = hi_request or hi_intent or te_request or te_intent

    # A request about the call itself ("turn on your camera"), not something to answer.
    procedural_hi = any(t in _HI_PROCEDURAL for t in toks)
    procedural_te = any(
        t in _TE_PROCEDURAL_NOUNS and any(u.startswith(_norm("చేయ")) for u in toks[i + 1 : i + 3])
        for i, t in enumerate(toks)
    )
    procedural_te = procedural_te or any(t == _norm("ఆగండి") for t in toks)
    if (procedural_hi and not strong_wh) or (procedural_te and not strong_wh):
        return Detection(False, 0.2, "statement")

    # An embedded or reported question ("I know what you mean", "he asked why ...").
    cues = _HI_EMBED_CUES | _TE_EMBED_CUES
    initial_particle = toks[0] == _norm("क्या")
    if not has_mark and not yes_no_ending and not (hi_intent or te_intent):
        for t in toks:
            if t in cues and not (initial_particle and t == _norm("पता")):
                return Detection(False, 0.2, "statement")

    if wh or request or yes_no_ending:
        if has_mark:
            return Detection(True, 0.95, "opener_and_mark")
        if wh:
            return Detection(True, 0.8, "question_word")
        if request:
            return Detection(True, 0.8, "request")
        return Detection(True, 0.7, "auxiliary_opener")
    if has_mark and len(toks) >= 4:
        return Detection(True, 0.6, "question_mark")
    return Detection(False, 0.0, "statement")


def _classify_roman(words: list[str], has_mark: bool) -> Detection | None:
    """Romanised Hindi / Telugu (Hinglish, Tenglish). Returns None when it finds nothing to add."""
    if len(words) < 3:
        return None
    has_wh = any(w in _ROMAN_WH for w in words)
    request = any(w in _ROMAN_REQUEST for w in words)
    yes_no = any(w in _ROMAN_YES_NO for w in words)
    if not (has_wh or request or yes_no):
        return None
    if not has_mark and any(w in _ROMAN_EMBED_CUES for w in words):
        return Detection(False, 0.2, "statement")
    if has_mark:
        return Detection(True, 0.95, "opener_and_mark")
    if has_wh:
        return Detection(True, 0.8, "question_word")
    if request:
        return Detection(True, 0.8, "request")
    return Detection(True, 0.7, "auxiliary_opener")


def classify(text: str) -> Detection:
    """Decide whether one utterance looks like a question put to the candidate."""
    cleaned = clean_text(text).strip()
    has_mark = cleaned.endswith("?")
    if _INDIC_SCRIPT.search(cleaned):
        return _classify_indic([t for t in _TOKEN_SPLIT.split(cleaned) if t], has_mark)
    words = _WORDS.findall(cleaned.lower())
    english = _classify_english(words, has_mark)
    if english.is_question and english.confidence >= 0.8:
        return english
    roman = _classify_roman(words, has_mark)
    if roman is None:
        return english
    if not english.is_question:
        return roman
    # Only the weak "question mark" evidence from the English rules: romanised Hindi / Telugu may be stronger.
    return roman if roman.is_question and roman.confidence > english.confidence else english
