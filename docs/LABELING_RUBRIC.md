# Issue difficulty labeling rubric (`rubric_v1`)

**What is rated.** How hard would it be for a contributor who is competent in the project's language but **new to this
codebase** to produce a fix the maintainers would accept? Rate the issue as written when it was opened. Do **not** rate
time spent, importance, how much discussion followed, who reported it, or the maintainers' difficulty labels (they are
hidden in the annotation sheet on purpose).

## Dimensions to weigh
1. **Scope** - how many files/modules would a fix plausibly touch?
2. **Codebase knowledge** - must the contributor understand the project's internals, or only one function?
3. **Technical depth** - is specialist knowledge needed (concurrency, storage engines, protocols, TLS, parsers, compilers)?
4. **Solution clarity** - does the issue already say where the problem is and what the fix is?
5. **Verification burden** - tests to write, platforms to reproduce on, compatibility to preserve.
6. **Decision risk** - will it need a design decision or consensus from maintainers?

## Levels
| Level | Name | Typical characteristics | Pilot examples (paraphrased) |
|---|---|---|---|
| 1 | beginner | Docs wording, dead link, typo, removing an unused import. The fix is stated or obvious. No behaviour change. | unused import; broken README link; outdated docs link; missing docs entry |
| 2 | easy | A small change in one function/file with clear intended behaviour; bug location given or evident; add a missing assertion; extend a docs example with code; change a default. | add a missing test assertion; cause and fix location given for a header-library bug; flaky test from a reused port |
| 3 | intermediate | Needs understanding of a module's design; investigating a bug's cause; a new small feature or option with an API decision; a few files plus tests. | new lint rule following a template; a new accessor method; parsing edge case in a core library |
| 4 | advanced | Cross-cutting or internal subsystem work: concurrency/reentrancy, storage engine behaviour, renderer lifecycle, security-sensitive code, hard-to-reproduce platform issues, or a design that needs maintainer consensus. | race in a runtime's orphan queue; TLS context regression; SSR hydration regression in an experimental mode |
| 5 | expert | Architecture-level: language/runtime/compiler/protocol internals, large redesigns, research-level problems. Reserve for rare cases. | (none in the 80-issue pilot) |
| skip | not a task | Question, support request, spam, empty template, bot dashboard, duplicate, or too unclear to understand. Recorded as unrateable on purpose. | spam; empty feature template; bot "Dependency Dashboard" |

## Decision procedure
1. Is it an actionable task? If not, write `skip` and a reason in `notes`.
2. Is the fix stated and tiny (docs/typo/unused code)? -> 1.
3. Is the location known and the change local to one place? -> 2.
4. Does it need understanding how several parts of one module interact, or an API/behaviour decision? -> 3.
5. Does it touch an internal subsystem, concurrency, security, a platform you must reproduce on, or need consensus? -> 4.
6. Does it need architecture- or language-level expertise or a redesign? -> 5.
7. Torn between two levels? Choose the higher one if the issue does **not** say how to fix it, the lower one if it does,
   and lower `confidence` by one step.

## Confidence (the annotator's own uncertainty)
- `high` - clear and typical for its level.
- `medium` - a reasonable judgement but the neighbouring level is plausible.
- `low` - not enough information, or the answer could be two or more levels away. **Low-confidence labels are kept in the
  dataset but excluded from training** and are the first items a human should review.

## Edge cases
- Feature request blocked by project policy: rate the implementation effort, `confidence=low`, say so in `notes`.
- Umbrella/"help wanted" cleanup lists: rate one typical item, `confidence=low`.
- Docs that require deep expertise to write correctly: raise one level above plain docs.
- Large mechanical change: rate a single chunk, not the total.
- Bug without a reproduction: at least level 3.
- Only an excerpt is visible: say what is missing in `notes` and lower `confidence`; **follow the link and read the whole issue**.

## Provenance (every label records who or what produced it)
| Source | Meaning |
|---|---|
| `human_review` | A person applied this rubric. The only label that may be called verified. Takes precedence. |
| `llm_annotation` | An AI model applied this rubric. Provisional. Its mistakes are correlated with any AI model evaluated on it. |
| `weak_label_hint` | Derived from maintainers' labels (e.g. good first issue). Noisy; measured below. |

`annotator` is an opaque id (`human_1`, `llm_claude_1`), never a real name or GitHub handle. Labels carry the rubric
version; changing the rubric means a new version (`rubric_v2`) and re-annotating, not editing old labels.

## Calibration before scaling (required once a human joins)
1. Two or more people (at least one human) annotate the **same blind sample of 50+ issues** with `review-sample`.
2. Run `python -m compass_ml_training agreement --a one.csv:human --b two.csv:human` (or `:llm`).
3. Scale up only if quadratic-weighted kappa is at least about 0.6 and exact agreement at least about 0.5 on 30+ shared
   issues (rules of thumb, not guarantees). Otherwise discuss every `large_disagreements` item, tighten this rubric, bump
   its version, and repeat.
4. Disagreements of two or more levels are adjudicated by a third person or discussion; the agreed file is then loaded
   (two files that label the same issue are refused by `build` until adjudicated).

## What the pilot showed, and what was changed
See [PILOT_LABELING_REPORT.md](PILOT_LABELING_REPORT.md). In short: 21% of AI labels were low-confidence because the
600-character excerpt was often insufficient (annotation sheets now show 1,500 characters by default); maintainer
"good first issue" labels agreed with careful annotation only 25% of the time (so they stay weak labels); level 5 never
occurred in 75 rated issues (expected to be rare, which makes a five-class model unlikely to be supportable).
