"""Command line: python -m compass_ml_training <command>   (run from backend/ with PYTHONPATH=".;../ml-training").

Commands: collect | export-db | build | review-sample | agreement | weak-check | train | evaluate | ranking-eval
Exit code 2 means "not enough reliable data / refused"; nothing is written in that case.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from compass_ml_training import dataset as ds

TRAIN_SOURCES = ("human_review", "llm_annotation", "weak_label_hint")


def _split_kind(spec: str) -> tuple[str, str]:
    path, _, kind = spec.rpartition(":")
    if not path or kind not in {"human", "llm"}:
        raise ValueError(f"annotation spec {spec!r} must look like path/to/file.csv:human or :llm")
    return path, kind


def _build(a) -> int:
    from compass_ml_training.collect import MANIFEST_NAME, load_raw_dir

    raw_path = Path(a.raw)
    raw = load_raw_dir(raw_path) if raw_path.is_dir() else ds.read_jsonl(raw_path)
    annotations: dict = {}
    sources_info = []
    for spec in a.annotations or []:  # later files must not silently override earlier ones
        path, kind = _split_kind(spec)
        loaded = ds.import_annotations(path, kind)
        clash = set(loaded) & set(annotations)
        if clash:
            raise ValueError(f"{path}: {len(clash)} record(s) already annotated by an earlier file; adjudicate first")
        annotations.update(loaded)
        sources_info.append({"file": Path(path).name, "kind": kind, "n": len(loaded),
                             "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()})
    reviews = ds.import_reviews(a.reviews) if a.reviews else None
    records, report = ds.build_dataset(raw, reviews, annotations)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ds.write_jsonl(out / "dataset.jsonl", records)
    notes = ["Weak labels come from maintainer labels and are provisional, not verified.",
             "AI annotations (llm_annotation) are provisional; only human_review labels are verified.",
             "Issues without a difficulty label, low-confidence labels and 'skip' (unrateable) issues are excluded from training."]
    man = ds.manifest(records, report, datetime.now(timezone.utc).isoformat(), notes)
    man["annotation_files"] = sources_info
    if raw_path.is_dir() and (raw_path / MANIFEST_NAME).exists():
        man["collection_manifest_sha256"] = hashlib.sha256((raw_path / MANIFEST_NAME).read_bytes()).hexdigest()
    (out / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(man["summary"], indent=2))
    return 0


def _train(a) -> int:
    from compass_ml_training.train import train

    try:
        result = train(a.dataset, a.out, scheme=a.scheme, seed=a.seed,
                       thresholds=ds.Thresholds(a.min_total, a.min_per_class, a.min_repos), overwrite=a.overwrite,
                       label_sources=tuple(a.label_sources))
    except ds.InsufficientDataError as exc:
        print("NOT TRAINED - the dataset cannot support a reliable model:", file=sys.stderr)
        for p in exc.problems:
            print(f"  - {p}", file=sys.stderr)
        print("See docs/ML.md and docs/LABELING_RUBRIC.md for how to build a suitable dataset.", file=sys.stderr)
        return 2
    meta = result["metadata"]
    print(f"Trained {meta['model_id']} -> {result['model_dir']}"
          + ("  [EXPLORATORY: below the data-sufficiency gate; will not be served by default]" if meta["exploratory"] else ""))
    for line in result["evaluation"]["conclusions"]:
        print(" *", line)
    return 0


def _agreement(a) -> int:
    from compass_ml_training.agreement import agreement

    def levels(spec):
        path, kind = _split_kind(spec)
        return {k: v.difficulty for k, v in ds.import_annotations(path, kind).items() if v.difficulty}

    print(json.dumps(agreement(levels(a.a), levels(a.b)), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="compass_ml_training")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="collect public issues via the GitHub API (needs network; token optional)")
    c.add_argument("--repos", required=True, help="text file, one owner/name per line")
    c.add_argument("--out-dir", required=True)
    c.add_argument("--max-pages", type=int, default=1, help="pages of 100 per repository (1 page = 2 API requests per repo)")
    c.add_argument("--label-query", action="append", default=[], help="extra single-page query, e.g. 'good first issue'")
    c.add_argument("--refresh", action="store_true", help="re-collect repositories that already have a file")

    e = sub.add_parser("export-db", help="export issues stored by the backend (uses COMPASS_DATABASE_URL)")
    e.add_argument("--out", required=True)

    b = sub.add_parser("build", help="clean, deduplicate and label raw records (file or collect directory)")
    b.add_argument("--raw", required=True)
    b.add_argument("--annotations", action="append", help="annotation CSV with its provenance: file.csv:human | file.csv:llm")
    b.add_argument("--reviews", help="legacy level-only review CSV (treated as human)")
    b.add_argument("--out-dir", required=True)

    r = sub.add_parser("review-sample", help="write an annotation CSV for a repository-stratified sample")
    r.add_argument("--dataset", required=True)
    r.add_argument("--n", type=int, default=100)
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--excerpt-chars", type=int, default=ds.DEFAULT_EXCERPT_CHARS, help="body characters shown per issue")
    r.add_argument("--out", required=True)

    g = sub.add_parser("agreement", help="annotator agreement (kappa) between two annotation CSVs")
    g.add_argument("--a", required=True, help="file.csv:human|llm")
    g.add_argument("--b", required=True, help="file.csv:human|llm")

    w = sub.add_parser("weak-check", help="how well do maintainer labels agree with annotator labels?")
    w.add_argument("--dataset", required=True)

    t = sub.add_parser("train", help="train + evaluate the difficulty baseline (needs requirements-ml.txt)")
    t.add_argument("--dataset", required=True)
    t.add_argument("--out", required=True, help="directory that will contain the model folder")
    t.add_argument("--scheme", default="auto", choices=["auto", "five_class", "three_class"])
    t.add_argument("--seed", type=int, default=42)
    t.add_argument("--label-sources", nargs="+", default=list(TRAIN_SOURCES), choices=TRAIN_SOURCES)
    t.add_argument("--min-total", type=int, default=ds.Thresholds.min_total)
    t.add_argument("--min-per-class", type=int, default=ds.Thresholds.min_per_class)
    t.add_argument("--min-repos", type=int, default=ds.Thresholds.min_repos)
    t.add_argument("--overwrite", action="store_true")

    v = sub.add_parser("evaluate", help="evaluate a saved model on another dataset file")
    v.add_argument("--model-dir", required=True)
    v.add_argument("--dataset", required=True)

    k = sub.add_parser("ranking-eval", help="compare stored scorers against relevance feedback (trains nothing)")
    k.add_argument("--methods", nargs="+", default=["heuristic_v1", "heuristic_v2"])
    k.add_argument("--k", type=int, default=5)
    k.add_argument("--min-users", type=int, default=5)

    a = p.parse_args(argv)
    try:
        return _dispatch(a)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:  # bad input is a message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(a) -> int:
    if a.cmd == "build":
        return _build(a)
    if a.cmd == "train":
        return _train(a)
    if a.cmd == "agreement":
        return _agreement(a)
    if a.cmd == "weak-check":
        from compass_ml_training.agreement import weak_vs_annotation

        print(json.dumps(weak_vs_annotation(ds.read_jsonl(a.dataset)), indent=2))
        return 0
    if a.cmd == "review-sample":
        n = ds.export_review_sample(ds.read_jsonl(a.dataset), a.n, a.seed, a.out, excerpt_chars=a.excerpt_chars)
        print(f"wrote {n} rows to {a.out}")
        return 0
    if a.cmd == "evaluate":
        from compass_ml_training.train import evaluate_artifact

        print(json.dumps(evaluate_artifact(a.model_dir, a.dataset), indent=2))
        return 0
    from app.core.config import get_settings
    from app.db.session import get_sessionmaker

    if a.cmd == "collect":
        from app.services.github import create_github_client
        from compass_ml_training.collect import CollectParams, collect_many, read_repo_list

        with create_github_client(get_settings()) as gh:
            run = collect_many(gh, read_repo_list(a.repos), a.out_dir,
                               CollectParams(a.max_pages, tuple(a.label_query)), refresh=a.refresh)
        for res in run["repositories"]:
            print(f"{res['repo']:40s} {res['status']}" + (f" ({res['n_issues']} issues)" if "n_issues" in res else ""))
        print(f"requests made: {run['requests_made']}; authenticated: {run['authenticated']}; "
              f"stopped by rate limit: {run['stopped_by_rate_limit']}")
        return 0
    with get_sessionmaker()() as db:
        if a.cmd == "ranking-eval":
            from compass_ml_training import ranking

            print(json.dumps(ranking.evaluate_methods(ranking.load_relevance_dataset(db), a.methods, a.k,
                                                     min_users=a.min_users), indent=2))
            return 0
        from compass_ml_training.db_export import export_issues

        recs = export_issues(db)
        ds.write_jsonl(a.out, recs)
        print(f"exported {len(recs)} issues")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
