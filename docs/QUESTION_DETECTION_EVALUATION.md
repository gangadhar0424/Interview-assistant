# Question-detection evaluation (M-7)

Status: **executed 2026-11-18; pending owner review.** Everything here was measured by running the detector (`services/api/app/qa/detection.py`) on synthetic text. **No human speech and no real speech-to-text output was used, and no native speaker has reviewed the Hindi or Telugu.** A good score here is a regression gate for a heuristic, not evidence that detection works on real interviews.

## 1. What the detector is for, and how its verdict is used (read this first)

`classify(text)` on the backend decides whether one transcribed utterance looks like a question put to the candidate. It is deterministic, dependency-free and advisory.

**This differs from the brief for this milestone**, which assumed "user confirmation" and "auto-answer off by default". That was true for Phase 12 (D-53) but was changed by **D-59**: after the user presses **Listen** (which is the consent), a flagged, complete utterance is **answered immediately**; an unflagged or cut-off utterance waits for the Ask button (`apps/desktop/src/renderer/src/store.ts`, `handleAudioEvent`). I preserved that behaviour exactly (`apps/desktop/tests/question-detection-flow.test.ts` pins it for every language). The consequence for this evaluation: **a false positive now costs an answer** (a model call and a wrong answer on screen), so precision matters more than recall; a false negative only means the user presses Ask.

## 2. Dataset

**Files:** `services/api/tests/eval/detection_set.jsonl` (main, 260 examples) and `detection_challenge.jsonl` (challenge, 72 examples). Rebuild with `python scripts/build_detection_set.py` and `python scripts/build_detection_challenge.py` (from `services/api`); they are deterministic and the committed files are their output. Evaluate with `python scripts/eval_detection.py` (see section 8).

**How it was created.** Every example was invented for this evaluation and written by the assistant (an AI). No real transcript, person, employer, recording or private data. English is written the way a speech-to-text provider returns it: mostly lower case, the question mark usually missing, no commas. Hindi is in Devanagari and Telugu in Telugu script, with English technical terms left in Latin letters (the code-switching real speakers produce); each language also has a small romanised (Hinglish / Tenglish) group. Each record carries `id`, `lang`, `category`, an explicit boolean `label`, and `split`.

**Label policy** (an owner decision to confirm; each rule changes what counts as a miss):
- **Positive:** a question, or a request or task, that the candidate is expected to answer or do (including "tell me about...", "explain...", and task prompts such as "write a function that...").
- **Negative:** statements; acknowledgements, thanks and small talk (including "how are you doing today", although it is grammatically a question); **procedural requests about the call** ("can you turn on your camera", "please share your screen"); sentences that merely contain a question word ("what i did was..."); and **incomplete utterances**, cut off before they say what is asked ("so what is the difference between"), because answering a fragment would be wrong.
- Not representable: a **question the candidate asks** (for example "what are the next steps") looks identical to one put to them, and the detector has no speaker information. It is a limitation, not a label.

**Splits.** Within every (language, category) the examples alternate `dev`, `heldout`, `dev`, ... The detector was tuned against `dev` only. `heldout` was run once, after the detector was frozen. The challenge set was written **after** the freeze and never used for tuning.

| Main set | English | Hindi | Telugu | Total |
|---|---|---|---|---|
| Positive | 44 | 49 | 49 | **142** |
| Negative | 36 | 41 | 41 | **118** |
| Total | 80 | 90 | 90 | **260** |
| of which dev / heldout | 40 / 40 | 46 / 44 | 46 / 44 | 132 / 128 |
| of which romanised | 0 | 10 | 10 | 20 |
| of which code-switched (script plus English terms) | 0 | at least 20 across hi and te | | |

Categories (every one present for every language, in both splits): positives **direct, technical, behavioral, followup, indirect, multiple (several questions in one utterance), task**; negatives **statement, keyword (question-like words that are not questions), instruction (procedural), incomplete, chatter (thanks, greetings, small talk)**; plus **roman**. The challenge set has 12 positives and 12 negatives per language (24 each, 72 total) in harder categories: declarative questions with a question mark, fillers and repeated words, a preamble before the question, tag questions, short follow-ups, imperatives such as "name three differences", exclamations, rhetorical and reported questions, a candidate's own account, announcements and closings.

## 3. Acceptance thresholds (set before any change)

Recorded before the detector was modified, applied **per language** on the development and held-out splits:

| Metric | Threshold | Why |
|---|---|---|
| Precision | **>= 0.90** | A false positive triggers an immediate answer (section 1): at most about one wrong answer in ten flagged utterances |
| False-positive rate | **<= 0.10** | Same reason, measured on the negatives |
| Recall | **>= 0.75** | A miss only means pressing Ask, so it may be lower |
| Existing English tests | no change in outcome | `tests/test_question_detection.py` is untouched and passes |
| Latency | **<= 3x** the old detector | Detection must not slow the audio pipeline; avoid any model call |

The challenge set has **no pass threshold**; it exists to expose limits.

## 4. Baseline (the detector committed at `f0e1075`, before any change)

Reproduce: `git show f0e1075:services/api/app/qa/detection.py > baseline.py` then `python scripts/eval_detection.py --detector-file baseline.py`.

**All 260 examples:** TP 34, FP 13, TN 105, FN 108. **Precision 0.72, recall 0.24, F1 0.36, accuracy 0.54, false-positive rate 0.11, false-negative rate 0.76.**

| Language | n | TP | FP | TN | FN | Precision | Recall | F1 | FPR |
|---|---|---|---|---|---|---|---|---|---|
| English | 80 | 34 | 13 | 23 | 10 | 0.72 | 0.77 | 0.75 | **0.36** |
| Hindi | 90 | 0 | 0 | 41 | 49 | n/a | **0.00** | 0.00 | 0.00 |
| Telugu | 90 | 0 | 0 | 41 | 49 | n/a | **0.00** | 0.00 | 0.00 |

By split: dev precision 0.71, recall 0.24, F1 0.35 (TP 17, FP 7, TN 53, FN 55); held-out precision 0.74, recall 0.24, F1 0.37 (TP 17, FP 6, TN 52, FN 53).

**By category:** recall on direct, technical, behavioral, follow-up and multiple was only **0.33** (the English third); **indirect, task and romanised were 0**. False positives: incomplete 6 of 18, keyword 5 of 24, instruction 2 of 18; chatter and statement 0.

**Failure patterns found (development split):**
1. **Hindi and Telugu are invisible.** The detector only reads Latin letters, so every Devanagari or Telugu question is "too short"; this is also what its own docstring says. Recall 0 in two product languages.
2. English **indirect openers** ("i was wondering how you would...", "i'd like to understand how...") and **task imperatives** ("write a function...", "implement...") are missed.
3. English **false positives**: a question word that starts a statement ("what i did was...", "who i am today..."), a **procedural request** ("can you turn on your camera"), and an **incomplete** utterance ("so what is the difference between", "how would you go about").

## 5. What was changed, and what was not

Justified by the baseline (every change is in `detection.py`; same `classify()` API and reason codes; still deterministic and pure; no model call; no new dependency):
- **Hindi and Telugu** (script and code-switched): small lexicons of question words, polite request forms and the Telugu yes/no ending, with cues for **embedded or reported** questions ("I know what you mean", "he asked why...") and **procedural** requests, plus small talk and cut-off endings. **Romanised** Hinglish / Tenglish via a short lexicon.
- **English:** indirect openers, task imperatives, and rules that reject a question word followed by "i/we/he/she/they", a procedural "can you ..." request, small talk, and an utterance that stops on a dangling word ("...between", "...when you").
- A defect found by the new tests and fixed: for romanised text, a trailing "?" could lower the confidence (0.8 became 0.6).

Deliberately **not** done: a model call (not approved, and the latency budget would be gone); special-casing the individual failures that remained on the development set; any change to the existing English unit tests (their reason codes were kept: new negatives reuse `statement`); any change to answering behaviour (section 1), consent, audio, providers or answer generation. One regression of my own, adding "please" as a skippable opening word, was caught by the development set and reverted.

## 6. Results after the change

**Held-out split (run once, after freezing the detector): 128 examples, TP 70, FP 0, TN 58, FN 0. Precision 1.00, recall 1.00, F1 1.00, accuracy 1.00, false-positive rate 0.00.** English 22/0/18/0, Hindi 24/0/20/0, Telugu 24/0/20/0 (TP/FP/TN/FN). Every pre-registered threshold is met in every language, on both splits.

**Development split** (tuned on): TP 72, FP 2, TN 58, FN 0. Precision 0.97, recall 1.00, F1 0.99, false-positive rate 0.03. The two remaining false positives were left on purpose: `which is exactly what we needed at the time` (a relative "which is") and `మీరు సిద్ధంగా ఉన్నప్పుడు నాకు చెప్పండి` ("tell me when you are ready").

**All 260:** TP 142, FP 2, TN 116, FN 0. **Precision 0.99, recall 1.00, F1 0.99, accuracy 0.99, false-positive rate 0.02.**

### Before and after

| | Baseline | After | Threshold |
|---|---|---|---|
| Held-out precision / recall / F1 | 0.74 / 0.24 / 0.37 | **1.00 / 1.00 / 1.00** | 0.90 / 0.75 / n/a |
| Held-out English precision, recall, FPR | 0.74, 0.77, 0.33 | 1.00, 1.00, 0.00 | 0.90, 0.75, 0.10 |
| Held-out Hindi recall | 0.00 | 1.00 | 0.75 |
| Held-out Telugu recall | 0.00 | 1.00 | 0.75 |
| All-260 precision / recall / F1 | 0.72 / 0.24 / 0.36 | 0.99 / 1.00 / 0.99 | |

### **Do not read the perfect held-out score as real-world accuracy**

I wrote both splits, in the same style, knowing the failure modes of a lexicon heuristic; the held-out sentences are close siblings of the development ones. That is why the challenge set exists.

### Challenge set (72 examples, written after the freeze, never used for tuning)

| | n | TP | FP | TN | FN | Precision | Recall | F1 | FPR |
|---|---|---|---|---|---|---|---|---|---|
| **Baseline**, all | 72 | 5 | 3 | 33 | 31 | 0.62 | 0.14 | 0.23 | 0.08 |
| **After**, all | 72 | 28 | 14 | 22 | 8 | **0.67** | **0.78** | 0.72 | **0.39** |
| After, English | 24 | 5 | 3 | 9 | 7 | 0.62 | 0.42 | 0.50 | 0.25 |
| After, Hindi | 24 | 11 | 6 | 6 | 1 | 0.65 | 0.92 | 0.76 | 0.50 |
| After, Telugu | 24 | 12 | 5 | 7 | 0 | 0.71 | 1.00 | 0.83 | 0.42 |

**This misses the precision and false-positive thresholds** (it was not required to pass them, but it is the better estimate of difficulty on unfamiliar phrasing, and it must not be hidden). The English challenge numbers are **unchanged** by my work (precision 0.62, recall 0.42): the English rules did not generalise to new phrasings; the gain is Hindi and Telugu.

**False positives (14), by pattern:** exclamations that begin with a question word (English "what a wonderful opportunity", Hindi "कितना शानदार मौका है यह", Telugu "ఎంత అద్భుతమైన అవకాశం ఇది": 3 of 3); rhetorical questions (3 of 3); reported or remembered questions with no cue word, for example "मैं सोच रहा था कि हमने ... क्यों चुना था" (3 of 6); a speaker's own account that contains a question word (2 of 3); a statement with several question words (2 of 3); "how nice to meet you" (1). **No false positives** on fillers, closings or announcements.

**False negatives (8):** English **preamble** before the question ("...so how would you scale it", "...i would like to ask what is your approach"), **tag questions** without a question word ("you have used redis right", and Hindi "आपने redis इस्तेमाल किया है ना"), **short follow-ups** ("and what about caching"), imperatives off the task-verb list ("name three differences...", "compare ..."), and a filler-split sentence ("what is the the difference between").

I did **not** tune on these (that would turn the only independent evidence into tuning data). They are pinned as documented limitations in `tests/test_question_detection_multilingual.py` (they fail loudly if someone fixes one, so this document gets updated).

## 7. Performance

Like-for-like, one process, same inputs, old versus new detector (a laptop with little free memory: the tails are noise, treat as approximate):

| Input | Old p50 / mean | New p50 / mean | Ratio (p50 / mean) |
|---|---|---|---|
| English only | 80.6 / 143.7 us | 115.5 / 293.9 us | **1.43x / 2.05x** |
| Hindi and Telugu | 66.9 / 217.2 us | 331.8 / 541.1 us | **4.96x / 2.49x** |
| All | 57.4 / 261.6 us | 220.4 / 381.9 us | 3.84x / 1.46x |

New p95: 1.03 ms overall, 1.57 ms for Hindi and Telugu. **The pre-registered 3x p50 ratio is met for English and NOT met for Hindi and Telugu (about 5x).** The ratio is not meaningful there, because the old detector did no work on those languages (it returned "too short" immediately). In absolute terms detection costs well under 2 ms, against speech-to-text that took 250 ms to 3.4 s in `PROVIDER_VALIDATION.md`; no model call was added and the audio pipeline is unchanged. I left it unoptimised.

## 8. Reproduce

```powershell
cd services/api
python scripts/build_detection_set.py
python scripts/build_detection_challenge.py
python scripts/eval_detection.py                          # all 260: metrics by language, category, split
python scripts/eval_detection.py --split dev --failures   # the tuning set's misses
python scripts/eval_detection.py --split heldout --failures --detector-frozen   # only after freezing
python scripts/eval_detection.py --set challenge --failures
python scripts/eval_detection.py --latency
python scripts/eval_detection.py --detector-file baseline.py   # score another detector (the baseline)
pytest tests/test_question_detection.py tests/test_question_detection_multilingual.py
cd ../../apps/desktop; npx vitest run tests/question-detection-flow.test.ts
```

## 9. Tests added

`services/api/tests/test_question_detection_multilingual.py` (97 tests with the existing file): dataset integrity, size and distribution; the per-language threshold gates on both splits; a floor on the challenge set (precision 0.60, recall 0.70, false-positive rate 0.45; not a quality claim); Hindi, Telugu, code-switched and romanised questions; statements and procedural requests; follow-ups; several questions in one utterance; known false positives and false negatives now rejected or detected; the documented limitations pinned; confidence rises with a question mark in every language; short and empty input; hostile and huge multilingual text (combining marks, zero-width and bidirectional controls, floods); determinism; a generous latency guard. `apps/desktop/tests/question-detection-flow.test.ts` (18 tests): a flagged question is answered at once with the exact heard text in seven language and script combinations, unflagged or cut-off lines wait for Ask, nothing is asked for a session that is not ready or while another answer streams, and an utterance never starts audio or the consent dialog. The existing English tests were not changed.

## 10. Known limitations

- Synthetic, single-author, **no native-speaker review**: some Hindi or Telugu may be unnatural, and dialect and register variation is not covered.
- Text written like speech-to-text output, **not real STT output**: real providers misspell, switch scripts, transliterate English terms ("डेटाबेस"), drop words and add or omit punctuation unpredictably.
- Lexicon coverage is partial (question words, polite forms, the Telugu yes/no ending). It cannot understand meaning, so **embedded, reported, rhetorical and exclamatory** uses of question words remain the main source of false positives, and questions with **no question word** (tag questions, rising intonation) the main source of misses.
- English rules did not generalise to the challenge phrasings.
- **No speaker information:** a question the candidate asks is indistinguishable from one put to them.
- The label policy (incomplete, procedural and small talk are negative) is a judgement; a different policy changes the scores.
- The Telugu yes/no ending rule and the cut-off-ending lists were written from general knowledge and are untested on real speech.
- Latency measured on a loaded laptop, once.

## 11. Remaining human-speech validation

1. A native Hindi and a native Telugu speaker review the dataset (naturalness, labels, the policy above).
2. Real **spoken** Hindi and Telugu through the actual speech-to-text providers (M-3 could not: no Hindi or Telugu voices were available), then run the detector on the genuine transcripts and compare with the synthetic results.
3. A consented sample of at least 200 human utterances per language, from different speakers, accents and noise conditions, labelled by two people, to measure the real false-positive rate, which is what costs a wrong immediate answer.
4. Decide whether the owner wants a higher bar before an automatic answer (for example, requiring a question word or request form rather than only a question mark, or asking for confirmation in Hindi and Telugu) given the challenge-set false positives.
5. Revisit the English preamble and tag-question misses only with that real data.
