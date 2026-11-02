# Machine learning (Stage 10)

**Status (Stage 11): real public data has been collected and a pilot labeled, but no usable model exists.**
1,485 real issues from 19 public repositories were collected; 80 were annotated by an AI annotator (not a human) using
a written rubric; training with the default sufficiency gate was **refused** for lack of data, and an explicitly
exploratory run on 58 AI-annotated issues did **not** beat its baselines (15 test issues, intervals include zero).
Full numbers and caveats: [PILOT_LABELING_REPORT.md](PILOT_LABELING_REPORT.md). Rubric: [LABELING_RUBRIC.md](LABELING_RUBRIC.md).
No label here is human-verified, and nothing may be described as a working difficulty predictor. The synthetic generator in
`backend/tests/ml_synthetic.py` is used only by tests; artifacts trained on it are flagged and refused by the loader.

## What is ML here, and what stays deterministic
| Task | Approach | Why |
|---|---|---|
| Issue difficulty from text | **ML baseline**: TF-IDF (1-2 grams) + structured features -> logistic regression | The signal is in free text; needs labels (weak ones only so far) |
| Skill compatibility / ranking | **No model.** Offline evaluation harness (P@K, NDCG@K) comparing `heuristic_v1`/`heuristic_v2` with relevance feedback | No relevance labels exist; keyword/lexical matching stays deterministic |
| Repository health, readiness, linked PRs, activity | Deterministic (Stage 9) | These are measurable facts, not predictions |
| Embeddings (Sentence Transformers), deep models | **Not implemented** | No reliable labels to justify them; heavier dependencies. Can be added behind the same artifact interface later |

The ML difficulty estimate is **not** wired into `heuristic_v1`/`heuristic_v2` scoring: there is no evidence it helps.
It is exposed only by `POST /api/v1/issues/{owner}/{repo}/{number}/difficulty`, next to (never merged with) the
maintainer-label hint.

## Data schema (`app/ml/schema.py`, `IssueRecord`, JSON lines)
`record_id, repo, issue_number, html_url, title, body, labels, author_association, repo_language, created_at, fetched_at,
source (github_api | database_export | synthetic_test), label {difficulty 1-5 | null, source, label_text}`.
Dataset folder: `dataset.jsonl` + `manifest.json` (data version = hash of ids and labels, counts, build report, notes).

**Sources and permitted use.** `collect` reads public repositories through GitHub's REST API (subject to GitHub's API
Terms and Acceptable Use Policies); `export-db` exports issues the backend already stored from public repositories.
Issue text is user-generated content: e-mails and @mentions are redacted at collection, commenters/assignees are not
stored, files are git-ignored and must not be redistributed without checking each repository's terms. Private
repositories are refused. `collect` has **not been run against live GitHub** here.

## Labels
| Source | Meaning | Reliability |
|---|---|---|
| `weak_label_hint` | Difficulty implied by the issue's maintainer labels: good first issue/beginner = 1, easy/starter = 2, medium = 3, hard/advanced = 4, expert = 5 (highest wins) | **Provisional.** Inconsistent across projects; `good first issue` often means "small", not "easy" |
| `llm_annotation` | An AI model applied the written rubric to the issue text (`:llm` annotation files) | **Provisional.** Better informed than a hint but not human; confidence and notes are recorded per label |
| `human_review` | A person applied the rubric (`review-sample` -> fill CSV -> `build --annotations file.csv:human`) | The only label that deserves the word "verified". Takes precedence over every other source |
| none | No difficulty label | Excluded from supervised training |

Saves, clicks, feedback and resolution time are **not** used as difficulty labels. Issues without a difficulty label are
excluded, so a model may not transfer to them (selection bias). Schemes: `five_class` (beginner..expert) or
`three_class` (easy = 1-2, medium = 3, hard = 4-5). `train --scheme auto` uses five classes only if every class has
enough examples, otherwise three, otherwise refuses.

## Features and leakage control (`app/ml/features.py`, version `difficulty_features_v1`)
Used: scrubbed title + body text (TF-IDF), log-scaled counts (title/body length, lines, code blocks, URLs, checkboxes,
numbered steps), traceback flag, author-is-maintainer flag, repository language.
Deliberately **not** used: maintainer labels (they are the weak-label source), every difficulty-naming phrase in the text
(removed before *any* feature, including length counts, are computed), comments, state, assignees, linked PRs,
timestamps, repository identity and repository activity (known only after the fact, or would let the model memorise
projects). Dedupe removes identical texts (template copies) so a copy cannot straddle train and test. Splits are by
**repository**, never by issue. A test trains on data where the label word is the only signal and requires the model not to
beat chance; building it exposed (and the code now fixes) a leak through raw body length.

## Sufficiency gate
Training stops (exit code 2, nothing written) unless there are at least 300 labelled issues, 8 repositories with
labelled issues, 30 examples per class, and the validation and test splits each contain at least 2 classes that also
occur in training. Thresholds are CLI options; lowering them is a deliberate, recorded choice. Real data will probably
support only `three_class`, and may support nothing until more labelled issues (ideally human-reviewed) are gathered.

## Install, build, train, evaluate
Run from `backend/` (paths use forward slashes; `PYTHONPATH` gives access to the app and `ml-training`).
```
pip install -r requirements.txt -r requirements-dev.txt -r requirements-ml.txt     # ML packages are optional
export PYTHONPATH=".:../ml-training"        # Windows PowerShell: $env:PYTHONPATH = ".;../ml-training"

# 1. collect (resumable; unauthenticated = 60 requests/hour, set COMPASS_GITHUB_TOKEN yourself for more)
python -m compass_ml_training collect --repos ../ml-training/repos.txt --out-dir ../ml-training/data/raw --label-query "good first issue"
python -m compass_ml_training export-db --out data/raw.jsonl                                   # alternative: from COMPASS_DATABASE_URL

# 2. build the dataset (clean, dedupe, weak labels, versioned manifest)
python -m compass_ml_training build --raw ../ml-training/data/raw --out-dir ../ml-training/data/v1

# 3. label (see docs/LABELING_RUBRIC.md): sheet -> annotate -> build with provenance
python -m compass_ml_training review-sample --dataset ../ml-training/data/v1/dataset.jsonl --n 120 --out ../ml-training/data/sheet.csv
#    each annotator fills difficulty_1_to_5 (1-5 or skip), confidence, annotator, notes; then:
python -m compass_ml_training agreement --a one.csv:human --b two.csv:human        # calibrate BEFORE scaling up
python -m compass_ml_training build --raw ../ml-training/data/raw --annotations adjudicated.csv:human --annotations ../ml-training/annotations/pilot_llm_rubric_v1.csv:llm --out-dir ../ml-training/data/v2
python -m compass_ml_training weak-check --dataset ../ml-training/data/v2/dataset.jsonl   # how good are maintainer labels?

# 4. train + evaluate (seeded; refuses if the data is insufficient; --label-sources limits which labels may be used)
python -m compass_ml_training train --dataset ../ml-training/data/v2/dataset.jsonl --out ../ml-training/data/models --seed 42
python -m compass_ml_training evaluate --model-dir ../ml-training/data/models/<model_id> --dataset other_repos.jsonl

# 5. offline comparison of the heuristic scorers with relevance feedback (trains nothing)
python -m compass_ml_training ranking-eval --methods heuristic_v1 heuristic_v2 --k 5
```
Models trained **below** the sufficiency gate (lowered `--min-*`) are marked `exploratory`; the loader refuses them unless
`COMPASS_ML_ALLOW_EXPLORATORY_ARTIFACTS=true`, and synthetic ones unless `COMPASS_ML_ALLOW_SYNTHETIC_ARTIFACTS=true`.

Each model folder contains `model.joblib`, `metadata.json` (model id, seed, library versions, label scheme and
definitions, data version and class distribution, training repositories, hyper-parameters, feature-schema version,
sha256 of the model file, evaluation status), `evaluation.json` and `report.md`.

**Evaluation.** Hyper-parameter C is chosen on the validation repositories; the held-out test repositories are scored
once. Reported: macro-F1, per-class precision/recall/F1/support, confusion matrix, accuracy, expected calibration
error and Brier score (the scores are *not* calibrated and no calibration step exists), a majority-class baseline, a
stratified-random (chance) baseline, and 95% bootstrap intervals for the macro-F1 difference. The model counts as
better only if it beats **both** baselines with confidence (a majority predictor's macro-F1 is structurally tiny, so
beating it alone proves little). A separate human-reviewed subset report appears once the test split has >= 20
human-labelled issues. Macro-F1 here measures agreement with weak labels on unseen repositories, not true difficulty.
No comparison with `heuristic_v1/v2` is made: they do not predict difficulty and there is no shared ground truth.

## Inference and integration
1. Set `COMPASS_ML_DIFFICULTY_MODEL_DIR=models/<model_id>` (unset = feature off; no auto-discovery).
2. `POST /api/v1/issues/{owner}/{repo}/{number}/difficulty` returns `deterministic_signals.difficulty_label_hint`
   (maintainer label, noisy) and `ml_prediction` separately: `status` `predicted | unsupported_input | unavailable`,
   `label`, `class_scores` (uncalibrated, **not probabilities**), `top_score`, `margin`, `calibrated: false`, the model
   id, label source and evaluation status. `fallback` says which signal is being relied on.
3. Unavailable reasons (all return HTTP 200 with the fallback): `not_configured`, `artifact_missing`, `artifact_invalid`
   (corrupt file, bad checksum, unreadable), `ml_dependencies_missing`, `feature_schema_mismatch`,
   `library_version_mismatch`, `synthetic_artifact_refused`, `exploratory_artifact_refused`. Too little text gives `unsupported_input`.
4. Predictions are stored in `issue_predictions` with the model version and status `ml_weak_labels`; `confidence` is
   left empty because no calibrated confidence exists. Identical input with the same model version is stored once.
5. The lightweight backend never imports scikit-learn/numpy/scipy (a test checks this in a clean interpreter); without
   the ML packages everything except this model works, and CI runs the suite both ways.

**Security.** `model.joblib` is a pickle; loading it runs code. The sha256 in `metadata.json` catches corruption and
single-file swaps, not an attacker who can write both files. Only load artifacts you trained or trust, and keep the
model directory write-protected. `models/` is git-ignored.

## Dataset size, class distribution, missing data
See [PILOT_LABELING_REPORT.md](PILOT_LABELING_REPORT.md): 1,485 records / 19 repositories, 810 with weak labels (92% level 1),
80 AI-annotated (8/25/29/13/0 across levels 1-5, 5 unrateable, 17 low-confidence). Every run writes these to `manifest.json` and to
the model's `metadata.json`/`evaluation.json`.

## Limitations
- Weak targets: the best the model can do is imitate maintainers' labelling habits, which differ between projects.
- Selection bias: only labelled issues are used; unlabelled issues (most of them) may look different.
- Only exploratory real-data results exist (n=15 test issues); no claim of accuracy, generalisation, or superiority over any heuristic is made.
- Class imbalance: "good first issue" dominates real data; hard/expert are rare. Expect three classes at best.
- Text-only features cannot see code size, affected modules or required expertise; these are the things that actually
  determine difficulty and are not captured.
- Duplicate detection is exact-match only; near-duplicates (templated issues) may still leak across splits.
- `collect` has run once against live GitHub (unauthenticated, 19 repositories); `export-db` has not run on real data; the ranking
  evaluation needs many users' ratings and inherits strong selection bias (see `ml-training/compass_ml_training/ranking.py`).
