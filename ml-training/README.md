# ml-training

Data collection, labeling support, training and evaluation for the issue-difficulty baseline. Kept apart from the
production code in `backend/app/ml/`, which only does inference.

Start with:
- [docs/ML.md](../docs/ML.md) - design, leakage rules, commands
- [docs/LABELING_RUBRIC.md](../docs/LABELING_RUBRIC.md) - how to label (read before annotating)
- [docs/PILOT_LABELING_REPORT.md](../docs/PILOT_LABELING_REPORT.md) - what has actually been collected, labeled and trained

Layout
- `compass_ml_training/` - the pipeline (`python -m compass_ml_training --help`, run from `backend/` with `PYTHONPATH=".;../ml-training"`)
- `repos.txt` - repositories to collect (edit freely)
- `annotations/` - committed annotation files (ids + level + confidence + short rationale only; no third-party text)
- `pilot_annotate_llm.py` - the AI pilot's judgements as code, for audit
- `data/` - **git-ignored**: raw issues (third-party text), datasets, models

Quick labeling workflow for a person
1. `review-sample --dataset data/v1/dataset.jsonl --n 120 --out data/sheet.csv`
2. Open each `html_url`, read the **whole** issue, then fill `difficulty_1_to_5` (1-5, or `skip`), `confidence`, `annotator`
   (an opaque id such as `human_1`) and `notes`.
3. `build --annotations data/sheet.csv:human ...` to fold your labels in (human labels override AI and maintainer labels).
