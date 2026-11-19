# Coding assistance evaluation (M-8)

Status (2026-11-18): implemented, pending owner review. Decision record: **D-73**. Roadmap: `PRODUCTION_ROADMAP.md` M-8.

**Read this first.** The evaluation set is **synthetic and written by the assistant (an AI)**. Nothing in it was reviewed by a person. Every score below is **automated**: a structure check, a hidden-case run of the Python and SQL solutions, and a Big-O string match. **Human-reviewed correctness has not been done**, so no number here says the explanations are accurate, the code is readable, or the test cases are good. Each arm was run **once** on 28 problems at temperature 0.2, so a difference of one problem is noise.

## 1. What was found before changing anything

| Finding | Evidence |
|---|---|
| The coding prompt was one sentence ("approach first, then clean code, then complexity and one or two edge cases") | `templates/v1/question_type/coding.md` |
| The system rules and the format, length and tone guidance told the model to write a spoken script: "no headings", "no bullet points", "a word target of 120 to 180 words", "output only what the candidate would say" | `templates/v4/system.md`, `v1/format/full_script.md`, `v1/length/balanced.md` |
| The output token limit was derived from the word target only (180 words x 2.0 = **360 tokens**), with no allowance for code | `app/preferences/compiler.py` |
| On the live Groq model, **10 of 28 answers (36%) used the whole 360-token limit** (output tokens include a reasoning model's hidden thinking), so code, tests and complexity were at risk of being cut off | `hit_token_limit_rate` in section 6 |
| The general-answer validator (`QA_VALIDATE_GENERAL=on`) would reject correct code answers: `>>>` (a real operator) counts as a prompt leak, a row of numbers counts as a phone number, and code counts against the spoken word limit | `app/qa/validation.py`, tests in `test_coding_assistance.py` |
| The screenshot extraction prompt said nothing about constraints, examples, the required language or cut-off text | `templates/vision/v1/extract_question.md` |
| The desktop renderer drew every answer as one plain pre-wrapped paragraph: no code block, label, copy or scrolling. No Markdown renderer exists in the app | `ui/App.tsx` |
| Live model output contains typographic characters inside code (5 non-breaking hyphens, U+2011, in code blocks in each of two runs) that break pasted code | counted from the saved runs |

## 2. What changed

**Prompt v5** (`templates/v5/`; v1 to v4 are unchanged and pinned by fingerprint; non-coding question types get the same guidance as v4):
- `question_type/coding.md`: a structured answer using only the sections that apply: Problem, Assumptions, Approach, Code (one complete fenced block naming its language), Complexity, Tests, Edge cases. It says never to invent a constraint, example or requirement; to state assumptions when something is missing or the question is cut off; to use the language the question names, else Python and say so; to give expected test results "worked out by hand, not output from running the code"; and to write headings in the session language for Hindi and Telugu.
- `system.md`: rule 4 now allows fenced code for a coding question; **new rule 8** forbids saying or implying code was run, executed, compiled or tested; a final **"Coding questions"** section says the coding layout is final over the format, length and tone guidance, and that the word target counts only the explanation.
- `user.md`: one conditional sentence repeating the coding layout.
- `question_type/auto.md`: points a programming problem at the coding structure.

**Token floor** (`compiler.py`): coding answers get at least 1800 output tokens. Other question types are unchanged (360 for balanced).

**Validator** (`validation.py`): fenced code is held out of the word-count, phone, `<<<`/`>>>`, candidate-claim and language checks and bounded separately (20,000 characters). Evidence tags, prompt delimiters and email addresses are still rejected **inside** code, and prose is held to every limit as before.

**Screenshot extraction prompt v2** (`templates/vision/v2/`; v1 kept): keep the whole problem statement, input and output descriptions, constraints, every example, any function signature and the required language; mark cut-off or unreadable parts as `[cut off]` / `[unreadable]` and never complete them. Every safety rule of v1 is kept.

**Desktop renderer** (no new dependency):
- `answer/markdown.ts`: a small Markdown subset (fenced code, headings, lists, paragraphs, inline code and bold). No HTML pass-through, no links, no images, so model output is drawn only as React text.
- `answer/code.ts`: language names and aliases, language detection for unlabelled blocks, a dependency-free highlighter for Python, JavaScript, TypeScript, Java, C, C++, C#, Go, Rust, SQL, Bash and JSON, and `normalizeCode`, which replaces typographic characters with ASCII inside recognised code languages.
- `ui/AnswerBody.tsx`: language label, line count, **Copy** per code block (copies exactly the code shown, with a "Copied" or "Not copied" result), scrollable keyboard-focusable blocks, a half-streamed block shown as "writing...", used for the live answer and the history list. The existing **Copy answer** button still copies the whole raw answer.

Not changed: provider routing, audio capture, consent, screenshot capture, the answer pipeline's routing, the web app (its Q&A page still shows plain text).

## 3. Methodology

- **Arms** (same questions; session preferences English, coding, full script, balanced, simple; no candidate material, so the general-knowledge path is exercised):
  - `before`: prompt v4 and the 360-token limit the code used before M-8.
  - `after`: prompt v5 and the 1800-token coding floor (the code as it is now).
- **Provider:** the app's own Groq adapter (`openai/gpt-oss-120b`), streamed. A second run of `after` used the Gemini adapter (`gemini-3.1-flash-lite`); there is **no Gemini `before` run**, so Gemini has no comparison. OpenAI and Anthropic were not run (no vendor credit, see `PROVIDER_VALIDATION.md`). Only the synthetic problems were sent.
- **Process:** the first `after` run followed the new layout in only 2 of 24 problems because the format, length and tone sections outweighed the coding guidance. The "Coding questions" final rule and the user-message sentence were added and the arm was run again. The prompt was therefore **tuned against this same set**, and the structure score is partly a measure of obeying instructions I wrote; read it as compliance, not as independent proof of quality.
- **Complexity matcher:** after the first scoring pass showed notation-only mismatches (`m·n` against `m * n`, factor order, renamed dimensions), the matcher was made order and symbol insensitive and a short list of accepted alternatives was added (`TIME_ALTERNATIVES` in `build_coding_set.py`). Both arms are scored with the same matcher. The alternatives were chosen after seeing outputs, which is a bias to keep in mind.

## 4. Dataset

`services/api/tests/eval/coding_set.jsonl`, built by `scripts/build_coding_set.py` (28 problems, invented, no text copied from any site).

| Category | n | Notes |
|---|---|---|
| Arrays and strings | 4 | two-sum, palindrome, longest unique substring, max subarray |
| Hash maps | 2 | group anagrams, first unique character |
| Sorting and searching | 3 | merge intervals, rotated search (O(log n) required), k-th largest |
| Recursion | 2 | permutations, flatten |
| Trees and graphs | 3 | tree depth (nested tuples), islands, shortest path |
| Dynamic programming | 3 | coin change, LCS, house robber |
| Basic SQL | 2 | second highest salary, headcount per department (run on sqlite) |
| Debugging | 2 | wrong initial maximum, mutable default argument |
| Complexity analysis | 1 | nested loop with a doubling inner loop |
| Edge-case reasoning | 1 | merging sorted linked lists |
| Incomplete or ambiguous | 2 | "find the duplicates in a list"; a screenshot-extracted problem with `[cut off]` constraints |
| Screenshot-extracted | 1 | a full statement with examples, constraints and a signature |
| Language requested | 2 | Java (reverse a list), JavaScript (debounce) |

20 Python problems and 2 SQL problems carry hidden cases and a reference solution; the tests run every reference against its own cases, so the set is self-checking (a problem with a wrong reference would fail). Python is the language of the executed problems; Java and JavaScript answers are checked for structure only.

## 5. Scoring criteria

| Criterion | How it is scored | Kind |
|---|---|---|
| Problem interpretation | For incomplete input, the answer states assumptions (an Assumptions section or "assume"). The requested language is used. | automated proxy |
| Solution correctness | Python solutions run against hidden cases in a separate `python -I` process (10 s limit); SQL runs on sqlite. Pass means every case. | **automated execution** (Python, SQL only) |
| Code completeness | Balanced fences, no placeholder code (`...`, TODO), output tokens below the request limit | automated |
| Complexity accuracy | The stated Big-O equals the reference or an accepted alternative | automated string match; **does not check it fits the code written** |
| Test-case quality | A Tests section with at least two cases and one boundary case | automated **presence proxy**, not quality |
| Formatting | Sections present and in order, code in fenced blocks naming the language, label matches detected syntax, no claim that code was run | automated |
| Readability, naming, explanation accuracy, test-case quality, edge-case reasoning | A person scores each 0 to 2 per problem | **not done** |

`scripts/coding_exec.py` runs model-written code in a temp directory with no stdin and a minimal environment. It is **not a security sandbox** (it does not block file or network access); use it only on output generated from these synthetic prompts.

## 6. Results

Groq `openai/gpt-oss-120b`, 28 problems per arm, one run each. Raw scores per problem: `tests/eval/coding_results.json` (no answer text).

| Metric | before (v4, 360 tokens) | after (v5, 1800 tokens) |
|---|---|---|
| Structure checks passed (mean) | 0.90 | 1.00 |
| All of Problem, Approach, Code, Complexity and Tests present | 0 of 28 | 26 of 28 (the two conceptual questions, complexity analysis and edge cases, correctly use fewer sections) |
| Tests section with at least two cases and a boundary case (24 problems) | 0.00 | 0.92 |
| Assumptions stated on the 2 incomplete problems | 0 of 2 | 2 of 2 |
| Complexity stated (24 problems) | 0.96 | 1.00 |
| Stated complexity matches reference or accepted alternative (23 problems) | 0.91 | 1.00 |
| Output reached the token limit | **0.36 (10 of 28)** | 0.00 |
| Code fences balanced / no placeholder code | 28 of 28 | 28 of 28 |
| Code block names a language; label matches the code; requested language used | all | all |
| Claims code was run | none | none |
| **Python and SQL solutions passing every hidden case (22 problems)** | **22** (86 of 86 cases) | **21** (85 of 86 cases) |

The one hidden-case miss after the change is `rec-permutations`: for `permutations([])` the solution returned `[]` where the reference returns `[[]]`, and the answer stated no assumption for it. The statement does not specify the empty case; the reference's choice is the standard one. One problem out of 22 in a single run is **not evidence that the change reduced correctness**, and the zero misses before it are not evidence that it was perfect. Correctness on these problems was already at the ceiling for this model, so this set cannot show an improvement in it.

Gemini `gemini-3.1-flash-lite`, after only: structure 1.00, 21 of 22 problems pass every case (84 of 86 cases; `graph-islands` missed two), tests proxy 0.79, complexity match 1.00, no output reached the limit.

### Provider latency and cost (measured, streaming, one request at a time)

Time to first token is the first content token (a reasoning model's hidden thinking happens before it). Cost uses the list prices recorded in `PROVIDER_VALIDATION.md` (taken 2026-11-18, not confirmed against an invoice) and the **measured mean token counts**; output tokens include hidden reasoning where the provider reports them that way.

| Run | First token p50 / p95 | Total p50 / p95 | Mean tokens in / out | Estimated cost per answer |
|---|---|---|---|---|
| Groq, before | 0.62 s / 1.50 s | 1.32 s / 2.45 s | 1,480 / 328 | $0.00042 |
| Groq, after | 0.76 s / 1.52 s | 2.01 s / 3.17 s | 2,171 / 477 | $0.00061 |
| Gemini, after | 2.09 s / 6.47 s | 3.26 s / 7.52 s | 2,147 / 524 | $0.00132 |

The change costs about **+47% input tokens** (the longer prompt), **+45% output tokens**, about **+0.14 s** at the first token and **+0.7 s** to finish on Groq, and about +$0.0002 per coding answer. The slowest single requests were 7.3 s (Groq, after) and 9.2 s (Gemini). Measurements came from one machine on one network over about ten minutes per run; provider load changes them. A first smoke request once took 10.5 s to the first token; it was a single outlier and is not in these figures.

## 7. Renderer evidence

- Parser, highlighter, language aliases and detection, `normalizeCode`: pure tests (`answer-markdown.test.ts`, `answer-code.test.ts`).
- Rendering in a DOM (`answer-render.test.ts`): language label and line count, several blocks, a block of 300 long lines staying inside one scrollable focusable block, a half-streamed block, per-block Copy copying exactly the code, a failed or throwing clipboard shown as "Not copied", HTML, links and images shown as text, a hostile language label not echoed, a plain spoken answer unchanged.
- Ten real answers (`answer-real-output.test.ts`, fixture `tests/fixtures/coding-answers.json`): every fence is accounted for, no `###` or fence marks remain on screen, no elements beyond text and the code blocks, and no typographic characters left in code.
- **How it looks at the compact and expanded window sizes was not inspected**: the packaged app was not run for this milestone. A manual check is added to `DESKTOP_MANUAL_CHECKLIST.md`.

## 8. Known limitations

1. **No human review** of the statements, the references, the expected complexity, the explanations or the generated test cases. Correctness is measured only by running Python and SQL.
2. **One run per arm, one model per arm, 28 synthetic problems**, written by the author of the prompt. The prompt was tuned on this set. Results will not carry over unchanged to real interview questions, other models, or other languages.
3. **Correctness is at ceiling** for Groq on this set, so it cannot show a gain; harder problems are needed.
4. Java, JavaScript, C++ and other languages are not executed. The complexity matcher compares text and does not check the claim against the code; the tests proxy checks that a Tests section exists with a boundary case, not that the cases are right.
5. **Hindi and Telugu coding answers were not evaluated** (the prompt asks for translated headings; nothing was run).
6. The general-answer **judge model** (`QA_VALIDATE_GENERAL=on`) was not run on coding answers, so whether it wrongly rejects correct code is unmeasured; only the deterministic checks were changed and tested.
7. The 1800-token floor is a floor, not a guarantee: a long solution in a language with verbose syntax, on a reasoning model that thinks at length, could still reach it. `hit_token_limit_rate` should be watched.
8. The highlighter is a tokenizer, not a parser (docstrings, regular expressions and template strings can be coloured wrongly); it only affects display. `normalizeCode` also changes a typographic character that was deliberately inside a string literal.
9. Live models also put narrow no-break spaces (U+202F) in the explanation text (hundreds per run). They display fine and are left alone in prose; the full-answer Copy copies them as written.
10. Every answer includes an Assumptions section in the final run, including for fully specified problems; it is short but is noise the prompt tries to avoid ("only when something is missing").
11. The web Q&A page still shows plain text (out of scope).
12. The compact and expanded sizes were not looked at (section 7).

## 9. Reproduce

```
cd services/api
python scripts/build_coding_set.py                       # rewrites tests/eval/coding_set.jsonl
python scripts/eval_coding.py generate --arm before --provider groq --out runs/before.json   # costs money
python scripts/eval_coding.py generate --arm after  --provider groq --out runs/after.json
python scripts/eval_coding.py score runs/before.json runs/after.json [--json]                # no model call
python -m pytest tests/test_coding_assistance.py
cd ../../apps/desktop && npx vitest run tests/answer-*.test.ts
```

`generate` reads provider keys through the app settings and never prints them; answers are written only to the file you name (do not commit it).

## 10. Before and after, in one place

| | Before | After |
|---|---|---|
| Coding prompt | one sentence, in a "spoken script, no headings" prompt | structured sections, assumptions, language rule, "never claim it was run", layout is final |
| Output budget (balanced) | 360 tokens | at least 1800 |
| Answers that hit the token limit (Groq) | 36% | 0% |
| Tests and edge cases in the answer | none of 28 | 92% (proxy) |
| Assumptions on incomplete problems | 0 of 2 | 2 of 2 |
| Solutions passing all hidden cases | 22 of 22 | 21 of 22 (single run; one unstated empty-input case) |
| Cost per coding answer (Groq, list price) | $0.00042 | $0.00061 |
| Validator on code answers | could reject `>>>`, number rows, long code | code held out; leaks and email still rejected |
| Screenshot extraction | no constraints, examples or language, no cut-off marks | all of them, `[cut off]` never completed |
| Renderer | one plain paragraph | headings, lists, labelled code blocks, per-block copy, scrolling, ASCII-clean code, no HTML |
