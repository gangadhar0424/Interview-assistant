"""PreferenceCompiler (Phase 7A): enum-to-config mapping, validation, sanitising, settings."""

import dataclasses
import itertools
import json
import math

import pytest

from app.ai.types import GenerationConfig
from app.core.config import ConfigurationError, Settings, load_settings
from app.preferences.compiler import LengthTargets, PreferenceCompiler, PreferencesInput
from app.preferences.enums import AnswerFormat, AnswerLength, AnswerTone, Language, QuestionType
from app.preferences.errors import InvalidPreference
from app.preferences.sanitize import clean_text

BANDS = {
    AnswerLength.SHORT: (60, 90),
    AnswerLength.BALANCED: (120, 180),
    AnswerLength.LONG: (250, 350),
}
ZWJ = chr(0x200D)
ZWNJ = chr(0x200C)


def compiler(max_extra: int = 1000) -> PreferenceCompiler:
    return PreferenceCompiler(LengthTargets(dict(BANDS), 2.0, 2.5), max_extra)


def prefs(**overrides: object) -> PreferencesInput:
    base: dict[str, object] = {
        "language": "en",
        "question_type": "behavioral",
        "answer_format": "full_script",
        "answer_length": "balanced",
        "answer_tone": "simple",
    }
    base.update(overrides)
    return PreferencesInput(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------- mappings


def test_enum_values_are_the_documented_identifiers() -> None:
    # These strings are stored in the database and sent over the API: a rename is a migration.
    assert [e.value for e in Language] == ["en", "hi", "te"]
    assert {e.value for e in QuestionType} == {
        "auto",
        "behavioral",
        "coding",
        "experience_check",
        "how_do_you",
        "situational",
        "system_design",
        "technical_knowledge",
        "tell_me_about_yourself",
    }
    assert [e.value for e in AnswerFormat] == ["full_script", "script_bullets", "bullets"]
    assert [e.value for e in AnswerLength] == ["short", "balanced", "long"]
    assert [e.value for e in AnswerTone] == ["simple", "formal"]


def test_every_valid_combination_compiles_and_round_trips_its_values() -> None:
    comp = compiler()
    combos = list(itertools.product(Language, QuestionType, AnswerFormat, AnswerLength, AnswerTone))
    assert len(combos) == 3 * 9 * 3 * 3 * 2
    for lang, qt, fmt, length, tone in combos:
        out = comp.compile(
            prefs(
                language=lang,
                question_type=qt,
                answer_format=fmt,
                answer_length=length,
                answer_tone=tone,
            )
        )
        a = out.answer
        assert (a.language, a.question_type, a.answer_format, a.answer_length, a.answer_tone) == (
            lang,
            qt,
            fmt,
            length,
            tone,
        )
        assert (a.target_min_words, a.target_max_words) == BANDS[length]


@pytest.mark.parametrize(("length", "band"), list(BANDS.items()))
def test_length_maps_to_its_word_band(length: AnswerLength, band: tuple[int, int]) -> None:
    out = compiler().compile(prefs(answer_length=length))
    assert (out.answer.target_min_words, out.answer.target_max_words) == band


@pytest.mark.parametrize(
    ("language", "length", "expected_tokens"),
    [
        ("en", "short", math.ceil(90 * 2.0)),
        ("en", "long", math.ceil(350 * 2.0)),
        ("hi", "short", math.ceil(90 * 2.0 * 2.5)),
        ("te", "long", math.ceil(350 * 2.0 * 2.5)),
    ],
)
def test_token_budget_scales_with_length_and_indic_scripts(
    language: str, length: str, expected_tokens: int
) -> None:
    out = compiler().compile(prefs(language=language, answer_length=length))
    assert out.generation.max_output_tokens == expected_tokens


def test_token_budget_never_exceeds_the_phase6_ceiling() -> None:
    huge = PreferenceCompiler(LengthTargets({k: (1, 40000) for k in AnswerLength}, 1.0, 1.0))
    assert huge.compile(prefs()).generation.max_output_tokens == 32768


@pytest.mark.parametrize(
    ("question_type", "temperature"),
    [
        ("auto", 0.3),
        ("behavioral", 0.5),
        ("coding", 0.2),
        ("experience_check", 0.3),
        ("how_do_you", 0.4),
        ("situational", 0.5),
        ("system_design", 0.2),
        ("technical_knowledge", 0.2),
        ("tell_me_about_yourself", 0.5),
    ],
)
def test_question_type_maps_to_sampling_temperature(question_type: str, temperature: float) -> None:
    assert compiler().compile(prefs(question_type=question_type)).generation.temperature == (
        temperature
    )


def test_phase6_generation_config_is_unchanged_and_reused() -> None:
    out = compiler().compile(prefs())
    assert type(out.generation) is GenerationConfig
    assert {f.name for f in dataclasses.fields(GenerationConfig)} == {
        "temperature",
        "max_output_tokens",
    }
    assert not hasattr(out.generation, "language")  # answer preferences live on AnswerConfig


def test_compile_is_deterministic_and_the_result_is_immutable() -> None:
    comp = compiler()
    first, second = comp.compile(prefs()), comp.compile(prefs())
    assert first == second
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.answer.language = Language.HI  # type: ignore[misc]


def test_snapshot_dict_is_json_serialisable() -> None:
    out = compiler().compile(
        prefs(language="te", extra_instructions="be brief", provider="p1", model="m1")
    )
    data = json.loads(json.dumps(out.to_dict()))
    assert data["answer"]["language"] == "te" and data["answer"]["provider"] == "p1"
    assert data["generation"] == {"temperature": 0.5, "max_output_tokens": 900}


# -------------------------------------------------------------- invalid values

FIELDS = ["language", "question_type", "answer_format", "answer_length", "answer_tone"]
BAD_VALUES: list[object] = [
    "",
    " ",
    "EN",
    "En",
    " en",
    "en ",
    "english",
    "unknown",
    None,
    5,
    True,
    "auto\n",
]


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("bad", BAD_VALUES, ids=repr)
def test_unknown_enum_values_are_rejected_without_echoing_them(field: str, bad: object) -> None:
    with pytest.raises(InvalidPreference) as exc:
        compiler().compile(prefs(**{field: bad}))
    assert exc.value.field == field


def test_rejected_value_is_not_leaked_in_the_error() -> None:
    with pytest.raises(InvalidPreference) as exc:
        compiler().compile(prefs(language="SECRET-VALUE"))
    assert "SECRET" not in str(exc.value)


def test_enum_matching_is_exact_and_not_normalised() -> None:
    # Cross-type values that are valid elsewhere must not be accepted in the wrong field.
    with pytest.raises(InvalidPreference):
        compiler().compile(prefs(answer_tone="short"))
    with pytest.raises(InvalidPreference):
        compiler().compile(prefs(answer_length="formal"))


# ------------------------------------------------------------ provider / model


def test_provider_and_model_are_optional_and_default_to_none() -> None:
    out = compiler().compile(prefs())
    assert out.answer.provider is None and out.answer.model is None


def test_provider_and_model_shape_is_validated() -> None:
    ok = compiler().compile(prefs(provider="gemini", model="some-model_1.5:latest"))
    assert (ok.answer.provider, ok.answer.model) == ("gemini", "some-model_1.5:latest")
    assert compiler().compile(prefs(provider="gemini")).answer.model is None
    for bad in ("", "a b", "../x", "x;drop", "x" * 101, 5):
        with pytest.raises(InvalidPreference) as exc:
            compiler().compile(prefs(provider=bad))
        assert exc.value.field == "provider"


def test_model_without_provider_is_rejected() -> None:
    with pytest.raises(InvalidPreference) as exc:
        compiler().compile(prefs(model="m1"))
    assert exc.value.field == "model"


# ------------------------------------------------------- extra instructions


def test_extra_instructions_default_to_empty() -> None:
    assert compiler().compile(prefs()).answer.extra_instructions == ""
    assert compiler().compile(prefs(extra_instructions="   \n\t ")).answer.extra_instructions == ""


def test_extra_instructions_length_cap_boundary() -> None:
    comp = compiler(max_extra=1000)
    assert len(comp.compile(prefs(extra_instructions="a" * 1000)).answer.extra_instructions) == 1000
    with pytest.raises(InvalidPreference) as exc:
        comp.compile(prefs(extra_instructions="a" * 1001))
    assert exc.value.field == "extra_instructions"  # rejected, never silently truncated


def test_cap_applies_after_cleaning_not_before() -> None:
    padded = "a" * 1000 + chr(0) * 50 + chr(0x200B) * 50  # 1100 raw chars, 1000 clean chars
    assert (
        len(compiler().compile(prefs(extra_instructions=padded)).answer.extra_instructions) == 1000
    )


def test_non_string_extra_instructions_rejected() -> None:
    with pytest.raises(InvalidPreference):
        compiler().compile(prefs(extra_instructions=123))


@pytest.mark.parametrize(
    "raw",
    [
        "a" + chr(0) + "b",  # NUL
        "a" + chr(0x1B) + "[31mb",  # ESC (terminal escape)
        "a" + chr(0x7F) + "b",  # DEL
        "a" + chr(0x85) + "b",  # C1 control (becomes a newline)
        "a" + chr(0x202E) + "b",  # right-to-left override
        "a" + chr(0x2066) + "b" + chr(0x2069),  # bidi isolates
        "a" + chr(0x200B) + "b",  # zero-width space
        "a" + chr(0xFEFF) + "b",  # BOM / zero-width no-break space
        "a" + chr(0x2060) + "b",  # word joiner
        "a" + chr(0xE0041) + "b",  # Unicode tag character ("ASCII smuggling")
    ],
)
def test_unsafe_characters_are_removed(raw: str) -> None:
    cleaned = clean_text(raw)
    assert all(ord(c) >= 32 or c == "\n" for c in cleaned)
    assert not any(
        ord(c) in (0x7F, 0x202E, 0x2066, 0x2069, 0x200B, 0xFEFF, 0x2060, 0xE0041) for c in cleaned
    )
    assert cleaned.startswith("a") and "b" in cleaned


def test_indic_joiners_and_combining_marks_survive_cleaning() -> None:
    hindi = "क" + chr(0x094D) + ZWJ + "ष और क" + chr(0x094D) + ZWNJ + "ष"
    telugu = "తెలుగు భాష " + "క" + chr(0x0C4D) + ZWJ + "ష"
    for text in (hindi, telugu):
        assert clean_text(text) == text
    assert compiler().compile(prefs(extra_instructions=hindi)).answer.extra_instructions == hindi


def test_whitespace_is_tidied_and_cleaning_is_idempotent() -> None:
    raw = "line one   \r\n\r\n\r\n\r\nline\ttwo" + chr(0x2028) + "end  "
    once = clean_text(raw)
    assert once == "line one\n\nline two\nend"
    assert clean_text(once) == once


def test_text_is_nfc_normalised() -> None:
    decomposed = "e" + chr(0x0301)  # e + combining acute
    assert clean_text(decomposed) == chr(0xE9)


# ----------------------------------------------------------------- settings


@pytest.fixture
def no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.mark.usefixtures("no_env_file")
def test_defaults_match_the_documented_targets() -> None:
    comp = PreferenceCompiler.from_settings(load_settings())
    for length, band in BANDS.items():
        out = comp.compile(prefs(answer_length=length))
        assert (out.answer.target_min_words, out.answer.target_max_words) == band
    assert comp.compile(prefs(extra_instructions="a" * 1000))
    with pytest.raises(InvalidPreference):
        comp.compile(prefs(extra_instructions="a" * 1001))


@pytest.mark.usefixtures("no_env_file")
def test_length_targets_and_caps_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANSWER_SHORT_MAX_WORDS", "100")
    monkeypatch.setenv("ANSWER_TOKENS_PER_WORD", "3")
    monkeypatch.setenv("ANSWER_INDIC_TOKEN_MULTIPLIER", "1")
    monkeypatch.setenv("EXTRA_INSTRUCTIONS_MAX_CHARS", "10")
    comp = PreferenceCompiler.from_settings(load_settings())
    out = comp.compile(prefs(answer_length="short", language="hi", extra_instructions="x" * 10))
    assert out.answer.target_max_words == 100 and out.generation.max_output_tokens == 300
    with pytest.raises(InvalidPreference):
        comp.compile(prefs(extra_instructions="x" * 11))


@pytest.mark.usefixtures("no_env_file")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANSWER_SHORT_MIN_WORDS", "95"),  # min above its own max (90)
        ("ANSWER_SHORT_MAX_WORDS", "130"),  # short overlaps balanced (120-180)
        ("ANSWER_BALANCED_MAX_WORDS", "300"),  # balanced overlaps long (250-350)
        ("ANSWER_LONG_MAX_WORDS", "100"),  # long max below long min
        ("ANSWER_TOKENS_PER_WORD", "0.5"),
        ("ANSWER_INDIC_TOKEN_MULTIPLIER", "9"),
        ("ANSWER_SHORT_MIN_WORDS", "0"),
        ("EXTRA_INSTRUCTIONS_MAX_CHARS", "0"),
    ],
)
def test_invalid_answer_settings_fail_clearly(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        load_settings()


def test_compiler_requires_a_band_for_every_length() -> None:
    with pytest.raises(ValueError):
        PreferenceCompiler(LengthTargets({AnswerLength.SHORT: (1, 2)}))
