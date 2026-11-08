"""Session preferences -> typed answer configuration -> provider-neutral prompts (Phase 7A, D-11).

`enums` is the single source of truth for the allowed preference values. `PreferenceCompiler` is
the only place mapping them to an `AnswerConfig` plus the Phase 6 sampling `GenerationConfig`.
`PromptBuilder` is the only place building prompts, from versioned template files.
"""
