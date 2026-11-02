"""Label consistency measurements (pure Python, no scikit-learn).

Two questions:
  * Do two annotators apply the rubric the same way?          -> agreement()
  * How far can maintainer labels stand in for annotation?    -> weak_vs_annotation()
Small samples give wide uncertainty: results report n and should be read with that in mind.
"""

from app.ml.labels import derive_weak_label
from app.ml.schema import IssueRecord

LEVELS = 5
# Common (Landis & Koch) reading of kappa; a convention, not a law, and unreliable for tiny samples.
KAPPA_BANDS = ((0.0, "no better than chance"), (0.2, "slight"), (0.4, "fair"), (0.6, "moderate"), (0.8, "substantial"), (1.01, "almost perfect"))


def _kappa(x: list[int], y: list[int], quadratic: bool) -> float | None:
    n = len(x)
    if n == 0:
        return None
    obs = [[0.0] * LEVELS for _ in range(LEVELS)]
    for a, b in zip(x, y):
        obs[a - 1][b - 1] += 1
    row = [sum(r) for r in obs]
    col = [sum(obs[i][j] for i in range(LEVELS)) for j in range(LEVELS)]
    num = den = 0.0
    for i in range(LEVELS):
        for j in range(LEVELS):
            w = ((i - j) ** 2 / (LEVELS - 1) ** 2) if quadratic else (0.0 if i == j else 1.0)
            num += w * obs[i][j]
            den += w * row[i] * col[j] / n
    return None if den == 0 else round(1 - num / den, 4)


def interpret(kappa: float | None) -> str:
    if kappa is None:
        return "undefined (one annotator used a single level, or no overlap)"
    return next(label for limit, label in KAPPA_BANDS if kappa < limit) if kappa >= 0 else KAPPA_BANDS[0][1]


def agreement(a: dict[str, int], b: dict[str, int], disagreement_gap: int = 2) -> dict:
    """Agreement between two annotators on the issues both labelled."""
    ids = sorted(set(a) & set(b))
    x, y = [a[i] for i in ids], [b[i] for i in ids]
    n = len(ids)
    out = {"n_common": n, "n_a": len(a), "n_b": len(b)}
    if n == 0:
        return {**out, "status": "no_overlap"}
    kappa, qwk = _kappa(x, y, False), _kappa(x, y, True)
    matrix = [[0] * LEVELS for _ in range(LEVELS)]
    for p, q in zip(x, y):
        matrix[p - 1][q - 1] += 1
    return {
        **out, "status": "ok" if n >= 30 else "small_sample",
        "exact_agreement": round(sum(p == q for p, q in zip(x, y)) / n, 4),
        "within_one_level": round(sum(abs(p - q) <= 1 for p, q in zip(x, y)) / n, 4),
        "cohen_kappa": kappa, "quadratic_weighted_kappa": qwk, "interpretation": interpret(qwk),
        "confusion_matrix": {"rows": "annotator_a", "columns": "annotator_b", "levels": [1, 2, 3, 4, 5], "matrix": matrix},
        "large_disagreements": [{"record_id": i, "a": a[i], "b": b[i]} for i in ids if abs(a[i] - b[i]) >= disagreement_gap],
        "caveat": "Few items give wide uncertainty; interpret kappa bands loosely." if n < 100 else None,
    }


def weak_vs_annotation(records: list[IssueRecord]) -> dict:
    """Compare maintainer-label hints with annotator labels on issues that have both.

    Shows whether maintainer labels are a trustworthy stand-in; if agreement is poor they must stay 'weak'.
    """
    pairs = []
    for r in records:
        if r.label.source in {"human_review", "llm_annotation"} and r.label.difficulty and not r.label.unrateable:
            weak = derive_weak_label(r.labels).difficulty
            if weak:
                pairs.append((r.record_id, weak, r.label.difficulty))
    if not pairs:
        return {"status": "no_overlap", "n": 0}
    weak_map = {i: w for i, w, _ in pairs}
    ann_map = {i: a for i, _, a in pairs}
    result = agreement(weak_map, ann_map)
    result["n_annotated_total"] = sum(1 for r in records if r.label.source in {"human_review", "llm_annotation"} and r.label.difficulty)
    result["note"] = ("Maintainer-label hints (rows) vs annotator labels (columns). Only issues carrying a difficulty label "
                      "can be compared, so this says nothing about the many issues without one.")
    return result
