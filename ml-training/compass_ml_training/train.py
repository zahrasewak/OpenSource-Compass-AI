"""Train and evaluate the difficulty baseline: TF-IDF + structured features -> logistic regression.

Why this algorithm: it is the simplest model that uses the issue text, trains in seconds on CPU, is
deterministic, and has inspectable coefficients. Anything heavier (transformers, embeddings) would need
more reliable labels than exist, so it is intentionally not added.

What it learns: to reproduce *maintainer labels* (weak targets) from the issue text. Test metrics therefore
measure agreement with those labels on unseen repositories, not agreement with true difficulty.
"""

import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from app.ml.difficulty import transform
from app.ml.features import FEATURE_SCHEMA_VERSION, dense_features, text_for_model
from app.ml.labels import class_for
from app.ml.registry import METADATA_FILE, METADATA_SCHEMA, MODEL_FILE, library_versions, sha256_file
from app.ml.schema import LABEL_DEFINITIONS, SCHEMES, IssueInput, IssueRecord
from compass_ml_training import dataset as ds
from compass_ml_training import evaluate as ev

C_GRID = (0.3, 1.0, 3.0)
HUMAN_SUBSET_MIN = 20


def _inputs(records: list[IssueRecord]):
    items = [IssueInput(title=r.title, body=r.body, author_association=r.author_association, repo_language=r.repo_language)
             for r in records]
    return [text_for_model(i) for i in items], [dense_features(i) for i in items]


def _fit_vectorizers(texts: list[str], dicts: list[dict]) -> dict:
    tfidf = TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=20000, sublinear_tf=True, lowercase=False)
    tfidf.fit(texts)
    dictvec = DictVectorizer(sparse=True).fit(dicts)
    scaler = StandardScaler(with_mean=False).fit(dictvec.transform(dicts))
    return {"tfidf": tfidf, "dictvec": dictvec, "scaler": scaler}


def _choose_scheme(labelled: list[IssueRecord], scheme: str, thresholds: ds.Thresholds) -> tuple[str, dict]:
    """`auto` tries five classes, then three. Returns (scheme, {scheme: shortfalls}) or raises InsufficientDataError."""
    candidates = ["five_class", "three_class"] if scheme == "auto" else [scheme]
    rejected: dict[str, list[str]] = {}
    for s in candidates:
        problems = ds.check_sufficiency(labelled, s, thresholds)
        if not problems:
            return s, rejected
        rejected[s] = problems
    raise ds.InsufficientDataError([f"[{s}] {p}" for s, ps in rejected.items() for p in ps])


from compass_ml_training.dataset import ALL_LABEL_SOURCES, usable_labelled  # noqa: E402
SUBSET_MIN = 20


LABEL_SOURCE_WORDS = {"weak_label_hint": "WEAK (maintainer-label-derived)", "llm_annotation": "AI-annotated (provisional)",
                      "human_review": "human-reviewed"}


def describe_label_sources(sources: set[str]) -> str:
    """Plain-English description of which kinds of label a set of issues carries (for reports)."""
    if len(sources) == 1:
        return LABEL_SOURCE_WORDS.get(next(iter(sources)), "unknown-source")
    return "mixed-source (" + ", ".join(sorted(LABEL_SOURCE_WORDS.get(x, x) for x in sources)) + ")"


def train(dataset_path: str | Path, out_dir: str | Path, *, scheme: str = "auto", seed: int = 42,
          thresholds: ds.Thresholds | None = None, created_at: str | None = None, overwrite: bool = False,
          label_sources: tuple[str, ...] = ALL_LABEL_SOURCES) -> dict:
    default_thresholds = ds.Thresholds()
    thresholds = thresholds or default_thresholds
    exploratory = (thresholds.min_total < default_thresholds.min_total or thresholds.min_per_class < default_thresholds.min_per_class
                   or thresholds.min_repos < default_thresholds.min_repos)
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    records = ds.read_jsonl(dataset_path)
    labelled = usable_labelled(records, label_sources)
    scheme, rejected = _choose_scheme(labelled, scheme, thresholds)
    classes_all = sorted({class_for(r.label.difficulty, scheme) for r in labelled})  # type: ignore[arg-type]

    split = ds.split_by_repo(labelled, seed)
    problems = []
    train_classes = {class_for(r.label.difficulty, scheme) for r in split["train"]}  # type: ignore[arg-type]
    for name in ("val", "test"):
        got = {class_for(r.label.difficulty, scheme) for r in split[name]}  # type: ignore[arg-type]
        if len(got) < thresholds.min_test_classes:
            problems.append(f"{name} split contains only {len(got)} class(es) (need >= {thresholds.min_test_classes})")
        if got - train_classes:
            problems.append(f"{name} split has classes absent from train: {sorted(got - train_classes)}")
    if len(train_classes) < 2:
        problems.append("training split has fewer than 2 classes")
    if problems:
        raise ds.InsufficientDataError(problems)
    classes = sorted(train_classes)

    y = {k: [class_for(r.label.difficulty, scheme) for r in v] for k, v in split.items()}  # type: ignore[arg-type]
    texts, dicts = {}, {}
    for k, v in split.items():
        texts[k], dicts[k] = _inputs(v)

    objects = _fit_vectorizers(texts["train"], dicts["train"])
    X = {k: transform(objects, texts[k], dicts[k]) for k in split}

    search = {}
    best_c, best_f1, best_clf = None, -1.0, None
    for c in C_GRID:
        clf = LogisticRegression(C=c, class_weight="balanced", max_iter=2000, random_state=seed).fit(X["train"], y["train"])
        f1 = ev.classification_metrics(y["val"], list(clf.predict(X["val"])), classes)["macro_f1"]
        search[str(c)] = f1
        if f1 > best_f1:  # strict '>' keeps the smaller C (stronger regularisation) on ties
            best_c, best_f1, best_clf = c, f1, clf
    objects.update({"clf": best_clf, "classes": [str(c) for c in best_clf.classes_]})

    # ---- single evaluation on held-out repositories
    proba = best_clf.predict_proba(X["test"])
    pred = [str(c) for c in best_clf.classes_[proba.argmax(axis=1)]]
    majority = DummyClassifier(strategy="most_frequent").fit(X["train"], y["train"])
    stratified = DummyClassifier(strategy="stratified", random_state=seed).fit(X["train"], y["train"])
    pred_major, pred_strat = list(majority.predict(X["test"])), list(stratified.predict(X["test"]))
    test_metrics = ev.classification_metrics(y["test"], pred, classes)
    report = {
        "scheme": scheme, "scheme_rejections": rejected, "classes": classes, "seed": seed,
        "split": {k: {"n": len(v), "n_repos": len({r.repo for r in v}),
                      "class_distribution": dict(sorted({c: y[k].count(c) for c in set(y[k])}.items()))} for k, v in split.items()},
        "hyperparameter_search_val_macro_f1": search, "selected_C": best_c,
        "test": {
            "model": test_metrics,
            "baseline_majority_class": ev.classification_metrics(y["test"], pred_major, classes),
            "baseline_stratified_random": ev.classification_metrics(y["test"], pred_strat, classes),
            "calibration": ev.calibration_metrics(y["test"], proba, [str(c) for c in best_clf.classes_]),
            "model_minus_majority_macro_f1": ev.bootstrap_macro_f1_difference(y["test"], pred, pred_major, classes, seed),
            "model_minus_stratified_macro_f1": ev.bootstrap_macro_f1_difference(y["test"], pred, pred_strat, classes, seed),
        },
    }
    human = [i for i, r in enumerate(split["test"]) if r.label.source == "human_review"]
    if len(human) >= HUMAN_SUBSET_MIN:
        report["test"]["human_reviewed_subset"] = ev.classification_metrics(
            [y["test"][i] for i in human], [pred[i] for i in human], classes)
    else:
        report["test"]["human_reviewed_subset"] = {"status": "insufficient", "n": len(human), "needed": HUMAN_SUBSET_MIN}
    by_source = {}
    for src in ALL_LABEL_SOURCES:  # the same test predictions, scored against each kind of label separately
        idx = [i for i, r in enumerate(split["test"]) if r.label.source == src]
        by_source[src] = (ev.classification_metrics([y["test"][i] for i in idx], [pred[i] for i in idx], classes)
                          if len(idx) >= SUBSET_MIN else {"status": "insufficient", "n": len(idx), "needed": SUBSET_MIN})
    report["test"]["by_label_source"] = by_source
    report["label_sources_used"] = list(label_sources)
    report["excluded_from_training"] = {
        "labelled_but_other_source": sum(1 for r in records if r not in labelled and r.label.difficulty is not None
                                        and r.label.source not in label_sources),
        "low_confidence": sum(1 for r in records if r.label.difficulty is not None and r.label.confidence == "low"),
        "unrateable": sum(1 for r in records if r.label.unrateable),
        "unlabelled": sum(1 for r in records if r.label.difficulty is None and not r.label.unrateable),
    }
    # A majority-class predictor has a structurally tiny macro-F1, so beating it alone proves little (even a
    # no-information model that guesses across classes does). Chance level is the stratified-random baseline;
    # the model counts as better only if it beats BOTH with 95% bootstrap confidence.
    beats_majority = report["test"]["model_minus_majority_macro_f1"]["a_better_with_95pct_confidence"]
    beats_chance = report["test"]["model_minus_stratified_macro_f1"]["a_better_with_95pct_confidence"]
    verdict = bool(beats_majority and beats_chance)
    report["conclusions"] = [
        f"Macro-F1 {test_metrics['macro_f1']} on {len(y['test'])} held-out issues from "
        f"{report['split']['test']['n_repos']} unseen repositories, measured against "
        f"{describe_label_sources({r.label.source for r in split['test']})} labels.",
        "The model " + ("does" if verdict else "does NOT") + " beat both the majority-class and the chance-level "
        "(stratified random) baselines with 95% bootstrap confidence "
        f"(majority: {'yes' if beats_majority else 'no'}, chance: {'yes' if beats_chance else 'no'}).",
        "No comparison with heuristic_v1/v2 is made: they are not difficulty classifiers and no common ground truth exists.",
    ]

    data_ver = ds.data_version(labelled)
    synthetic = any(r.source == "synthetic_test" for r in labelled)
    sources = {r.label.source for r in labelled}
    label_source = ("human" if sources == {"human_review"} else "llm_annotation" if sources == {"llm_annotation"}
                    else "weak_labels" if sources == {"weak_label_hint"} else "mixed")
    model_id = f"ml_difficulty_lr_{scheme}_{data_ver[:8]}_s{seed}"
    out = Path(out_dir) / model_id
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} already exists")
    out.mkdir(parents=True, exist_ok=True)
    joblib.dump(objects, out / MODEL_FILE)
    metadata = {
        "metadata_schema": METADATA_SCHEMA, "model_id": model_id, "model_kind": "ml_difficulty_classifier",
        "algorithm": "tfidf(1-2gram)+structured features -> logistic regression (class_weight=balanced)",
        "created_at": created_at, "seed": seed, "label_scheme": scheme,
        "label_definitions": {"classes": SCHEMES[scheme], "sources": LABEL_DEFINITIONS},
        "label_source": label_source,
        "evaluation_status": "synthetic_not_for_use" if synthetic else "exploratory_below_gate" if exploratory else (
            {"human": "evaluated_on_human_labels", "weak_labels": "evaluated_on_weak_labels",
             "llm_annotation": "evaluated_on_ai_annotated_labels"}.get(label_source, "evaluated_on_mixed_labels")),
        "trained_on_synthetic": synthetic, "exploratory": exploratory, "calibrated": False,
        "label_sources_used": list(label_sources), "rubric_versions": sorted({r.label.rubric_version for r in labelled if r.label.rubric_version}),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "data": {"version": data_ver, "n_labelled": len(labelled), "n_repos": len({r.repo for r in labelled}),
                 "class_distribution": ds.describe(labelled, scheme)["class_distribution"],
                 "label_sources": ds.describe(labelled)["label_sources"], "record_sources": ds.describe(labelled)["record_sources"],
                 "dataset_description": ds.describe(records),
                 "train_repos": sorted({r.repo for r in split["train"]})},
        "hyperparameters": {"C": best_c, "class_weight": "balanced", "ngram_range": [1, 2], "min_df": 2, "max_features": 20000},
        "metrics_summary": {"test_macro_f1": test_metrics["macro_f1"],
                            "majority_baseline_macro_f1": report["test"]["baseline_majority_class"]["macro_f1"],
                            "beats_baselines_95ci": verdict, "test_n": len(y["test"])},
        "library_versions": library_versions(), "platform": platform.platform(),
        "model_sha256": sha256_file(out / MODEL_FILE),
    }
    (out / METADATA_FILE).write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    (out / "evaluation.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    (out / "report.md").write_text(render_report(metadata, report), encoding="utf-8")
    return {"model_dir": str(out), "metadata": metadata, "evaluation": report}


def render_report(meta: dict, rep: dict) -> str:
    t = rep["test"]
    lines = [f"# {meta['model_id']}", "",
             f"**Status: {meta['evaluation_status']}.** Label source: {meta['label_source']}. Scores are NOT calibrated probabilities.", "",
             *(f"- {c}" for c in rep["conclusions"]), "",
             f"Data version `{meta['data']['version'][:12]}`, {meta['data']['n_labelled']} labelled issues in "
             f"{meta['data']['n_repos']} repositories. Class distribution: {meta['data']['class_distribution']}.", "",
             "| | macro-F1 | accuracy |", "|---|---|---|",
             f"| model | {t['model']['macro_f1']} | {t['model']['accuracy']} |",
             f"| majority class | {t['baseline_majority_class']['macro_f1']} | {t['baseline_majority_class']['accuracy']} |",
             f"| stratified random | {t['baseline_stratified_random']['macro_f1']} | {t['baseline_stratified_random']['accuracy']} |", "",
             "Per class (test): " + "; ".join(f"{c}: P={v['precision']} R={v['recall']} n={v['support']}"
                                              for c, v in t["model"]["per_class"].items()),
             "", f"Confusion matrix (rows true, columns predicted; labels {t['model']['confusion_matrix']['labels']}): "
             f"{t['model']['confusion_matrix']['matrix']}", "",
             f"Calibration: ECE {t['calibration']['expected_calibration_error']}, Brier {t['calibration']['brier_score']} (uncalibrated).",
             "", f"Limitations: labels are provisional ({meta['label_source']}; weak = maintainer-label derived, llm_annotation = "
             "AI-annotated; only human_review is verified). Issues without a usable label were excluded, so results may not "
             "transfer to them. The test set is small and comes from the repositories listed in the split; no claim is made beyond them. "
             "See docs/ML.md."]
    return "\n".join(lines) + "\n"


def evaluate_artifact(model_dir: str | Path, dataset_path: str | Path, *, allow_synthetic: bool = False) -> dict:
    """Evaluate a saved model on another dataset file (e.g. repositories it never saw)."""
    from app.ml.registry import load_artifact

    objects, meta = load_artifact(model_dir, allow_synthetic=allow_synthetic)
    scheme, classes = meta["label_scheme"], sorted(str(c) for c in objects["classes"])
    records = [r for r in ds.read_jsonl(dataset_path) if r.label.difficulty is not None]
    if not records:
        raise ds.InsufficientDataError(["dataset has no labelled records"])
    texts, dicts = _inputs(records)
    proba = objects["clf"].predict_proba(transform(objects, texts, dicts))
    pred = [str(c) for c in np.array(objects["classes"])[proba.argmax(axis=1)]]
    truth = [class_for(r.label.difficulty, scheme) for r in records]  # type: ignore[arg-type]
    seen = set(meta["data"]["train_repos"])
    overlap = sorted({r.repo for r in records} & seen)
    return {"model_id": meta["model_id"], "metrics": ev.classification_metrics(truth, pred, classes),
            "n_repos": len({r.repo for r in records}), "repos_seen_in_training": overlap,
            "warning": ("Some evaluated repositories were in the training split; results are optimistic." if overlap else None)}
