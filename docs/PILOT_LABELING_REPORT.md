# Pilot data collection, labeling and training attempt (Stage 11)

All numbers below come from actual runs on 2026-11-01; the raw data and models are local (git-ignored). The only labels
that exist are **maintainer-label hints** and **80 AI annotations** made by Claude with the written rubric
([LABELING_RUBRIC.md](LABELING_RUBRIC.md)). **No human has labeled anything yet**, so nothing here is verified ground truth.

## 1. Data collected
- Source: GitHub REST API, public repositories, **unauthenticated** (no token configured), `collector_v1`, API version
  2022-11-28. Run window 16:53:06-16:53:49 UTC; 58 requests; stopped cleanly by the 60/hour unauthenticated rate limit.
- Per repository: the 100 most recently updated items (pull requests removed) plus one extra query for the label
  `good first issue`. **19 of 20 repositories collected**; `rubocop/rubocop` was rate-limited and is not collected (resume
  by re-running the same command; finished repositories are skipped).
- 1,490 issues collected, 5 exact-text duplicates dropped -> **1,485 records in 19 repositories** (flask 60, requests 34,
  pandas 136, scikit-learn 120, pytest 130, httpx 130, poetry 58, matplotlib 65, vuejs/core 39, TypeScript 125, prettier 40,
  eslint 141, hugo 41, cli/cli 123, prometheus 47, rust-clippy 117, tokio 17, ripgrep 31, nlohmann/json 36).
- Stored per issue: repo, issue number and URL, title, body (e-mails and @mentions redacted), maintainer label names,
  author association category, repository language, creation time, collection time, record id. **No usernames.** A
  manifest records parameters, per-file SHA-256 and request counts; `data_version` is a hash of ids and labels
  (`b07fcf0036b6` before annotation, `3f3210564014` after).
- Recency bias: items are the most recently updated per repository, and the extra label query over-represents
  `good first issue`. This is **not** a random sample of issues.

## 2. What the maintainer labels give
810 of the 1,485 records carry a difficulty-style maintainer label (before annotation): **747 are level 1** ("good first
issue" and similar), 46 level 2, 17 level 3, **none at level 4 or 5**. 92% of the weak labels are one class, partly
because of the enrichment query. They cannot support a multi-class difficulty model.

## 3. Pilot annotation (80 issues)
- Sample: repository-stratified, seed 42, 19 repositories (~4 each), maintainer labels hidden from the annotator.
- Annotator: `llm_claude_1`, reading the title and the **first 600 characters** of each body (not the full issue or the code).
- Result: 75 rated, 5 `skip` (spam, empty template, bot dashboard, a support request in Russian). Levels:
  **1: 8, 2: 25, 3: 29, 4: 13, 5: 0**. Confidence: high 11, medium 52, **low 17 (21%)**.
- The 17 low-confidence labels are excluded from training and are the priority for human review. Reasons recorded in
  `notes` (e.g. "body empty", "only the template is visible", "may already be supported").
- Committed record: `ml-training/annotations/pilot_llm_rubric_v1.csv` (ids + level + confidence + short rationale, no
  third-party text) and `ml-training/pilot_annotate_llm.py`.

## 4. Are maintainer labels trustworthy? (measured)
On the **32** pilot issues that also carried a maintainer difficulty label (almost all "good first issue"):

| exact agreement | within one level | Cohen's kappa | quadratic-weighted kappa |
|---|---|---|---|
| 25% | 62.5% | -0.01 | -0.05 |

Of the 29 issues whose maintainer label implied level 1, the annotator assigned level 1 to 7, level 2 to 11, level 3 to 9
and level 4 to 2; the other 3 maintainer-labeled issues implied level 2 (2) or 3 (1). Of the 12 disagreements of two or
more levels, 11 were a maintainer label *below* the annotation and 1 above it. Reading: "good first issue" tends to
mean "small enough for a newcomer to attempt", not "beginner-level" under this rubric. **Caveats:** one AI annotator,
excerpts only, n=32; the real disagreement could be smaller or larger. It is strong enough to keep maintainer labels
classified as *weak*, and not strong enough to claim the rubric is right.

## 5. Is the pilot good enough to scale? **No - not yet.**
- Inter-annotator agreement could not be measured: there is one annotator. The tooling (`agreement`) is built and tested.
- 21% low-confidence labels show the excerpt was too short; this was fixed in the tooling (default 1,500 characters), but
  the 80 labels were not redone.
- Level 5 never occurred and level 4 is thin (13): a five-class model is not supportable; three classes (easy 1-2,
  medium 3, hard 4-5) would have 33 / 29 / 13 rated pilot examples (27 / 21 / 10 once low-confidence ones are excluded).
- The AI annotator's errors would be inherited by anything trained on them.

## 6. Training attempts (real data, actual results)
**Run A - default sufficiency gate, all label sources:** *not trained.* Refused with:
`five_class: 'advanced' has 10 examples (need >= 30); 'expert' has 0` and `three_class: 'hard' has 10 (need >= 30)`.
Nothing was written. This is the correct outcome: the data cannot support a reliable model.

**Run B - exploratory (explicitly below the gate), AI annotations only, three classes, seed 42, thresholds 50/10/8:**
trained so the pipeline's behaviour on real text could be observed; **not usable** (metadata `exploratory: true`; the
loader refuses it unless explicitly allowed).
- Training data: 58 usable AI-annotated issues in 19 repositories (easy 27, medium 21, hard 10), after excluding 17
  low-confidence and 5 unrateable items.
- Split by repository: train 34 issues / 12 repos, validation 9 / 3, **test 15 issues / 4 unseen repositories**
  (easy 9, medium 6; no `hard` in the test set).
- Test results (against AI-annotated labels, not verified): macro-F1 **0.35**; accuracy 0.60; per class easy P 0.73 R 0.89,
  medium P 0.50 R 0.17, hard n=0. Confusion matrix (rows true, columns predicted; easy/hard/medium): [[8,0,1],[0,0,0],[3,2,1]].
- Baselines on the same test set: majority class macro-F1 0.25 (accuracy 0.60), chance-level (stratified random) 0.19.
- 95% bootstrap interval of the macro-F1 difference: vs majority [-0.03, 0.26], vs chance [-0.10, 0.40].
  **Both include zero: the model is not shown to beat either baseline.** Its accuracy equals the majority baseline.
- Calibration (uncalibrated by construction): ECE 0.25, Brier 0.50.
- Conclusion: with 15 test issues nothing can be concluded about generalisation to unseen repositories beyond "this tiny
  test did not show an advantage". No claim of generalisation is made.

## 7. Problems found and improvements made
| Problem | Improvement |
|---|---|
| 21% of labels low-confidence (excerpt too short) | Default excerpt 1,500 characters; `confidence` and `notes` are captured per label; low-confidence labels are excluded from training and flagged for review |
| Weak labels are 92% one class | Never treated as ground truth; reported separately (`by_label_source`); `weak-check` measures them against annotations |
| Single annotator, no agreement measure | Annotation files carry provenance; `agreement` computes kappa/disagreements; two annotators on one issue are refused until adjudicated |
| Majority-class baseline is structurally weak | Model must beat the chance-level baseline too, with bootstrap intervals |
| Tiny data could produce misleading models | Sufficiency gate; models below it are marked exploratory and refused by default |

## 8. What still needs human work (exactly)
1. **Re-collect/extend**: re-run `collect` after the rate limit resets for `rubocop/rubocop`, and add repositories with
   fewer `good first issue` labels and more varied labeling cultures (see `ml-training/repos.txt`). Unauthenticated
   collection is 60 requests/hour; a token you configure yourself (`COMPASS_GITHUB_TOKEN`) raises that to 5,000.
2. **A human annotator must label** `review-sample --n 120` (a fresh sheet with 1,500-character excerpts), reading each
   issue **at the link**, using `docs/LABELING_RUBRIC.md`. Then a human must review the 17 low-confidence and the 12 large-
   disagreement pilot items.
3. **Calibration**: two annotators label the same 50+ issues; run `agreement`; proceed only at roughly QWK >= 0.6.
4. **Scale to a few hundred human-labeled issues** (the gate wants 300 usable labels with 30 per class); level 4/5 examples
   will need deliberate searching (e.g. issues labeled `performance`, `security`, `concurrency`), since random sampling finds few.
5. Re-run `build` and `train`; only then is a result worth reading.
