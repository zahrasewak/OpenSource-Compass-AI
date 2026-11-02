"""Metrics. Requires scikit-learn + numpy (training environment only)."""

import numpy as np
from sklearn.metrics import confusion_matrix, f1_score, precision_recall_fscore_support


def classification_metrics(y_true: list[str], y_pred: list[str], labels: list[str]) -> dict:
    p, r, f, s = precision_recall_fscore_support(y_true, y_pred, labels=labels, zero_division=0)
    return {
        "n": len(y_true),
        "macro_f1": round(float(f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0)), 4),
        "accuracy": round(float(np.mean(np.array(y_true) == np.array(y_pred))), 4) if y_true else None,
        "per_class": {c: {"precision": round(float(p[i]), 4), "recall": round(float(r[i]), 4),
                          "f1": round(float(f[i]), 4), "support": int(s[i])} for i, c in enumerate(labels)},
        "confusion_matrix": {"labels": labels, "rows_are": "true", "columns_are": "predicted",
                             "matrix": confusion_matrix(y_true, y_pred, labels=labels).tolist()},
    }


def calibration_metrics(y_true: list[str], proba: np.ndarray, classes: list[str], bins: int = 10) -> dict:
    """Top-label expected calibration error and multiclass Brier score. Reported so uncalibrated scores are
    visible as such; a low value on one small test set would still not make the scores probabilities."""
    idx = {c: i for i, c in enumerate(classes)}
    truth = np.array([idx[c] for c in y_true])
    conf, pred = proba.max(axis=1), proba.argmax(axis=1)
    correct = (pred == truth).astype(float)
    edges = np.linspace(0, 1, bins + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (conf > lo) & (conf <= hi)
        if mask.any():
            ece += mask.mean() * abs(correct[mask].mean() - conf[mask].mean())
    onehot = np.eye(len(classes))[truth]
    return {"expected_calibration_error": round(float(ece), 4),
            "brier_score": round(float(np.mean(np.sum((proba - onehot) ** 2, axis=1))), 4),
            "calibrated": False, "note": "No calibration step was applied; scores are not probabilities."}


def fast_macro_f1(t: np.ndarray, p: np.ndarray, n_classes: int) -> float:
    """Macro-F1 on integer class ids (same value as sklearn's with zero_division=0; ~100x faster per call, which
    matters for thousands of bootstrap resamples; a test checks the two agree)."""
    tp = np.bincount(t[t == p], minlength=n_classes)
    denom = np.bincount(t, minlength=n_classes) + np.bincount(p, minlength=n_classes)
    return float(np.mean(np.where(denom > 0, 2 * tp / np.maximum(denom, 1), 0.0)))


def bootstrap_macro_f1_difference(y_true: list[str], pred_a: list[str], pred_b: list[str], labels: list[str],
                                  seed: int, n_boot: int = 1000) -> dict:
    """95% bootstrap interval for macro-F1(a) - macro-F1(b) on the same test set. If the interval includes 0 the
    data does not show that a beats b."""
    rng = np.random.RandomState(seed)
    index = {c: i for i, c in enumerate(labels)}
    t, a, b = (np.array([index[x] for x in v]) for v in (y_true, pred_a, pred_b))
    diffs = []
    for _ in range(n_boot):
        i = rng.randint(0, len(t), len(t))
        diffs.append(fast_macro_f1(t[i], a[i], len(labels)) - fast_macro_f1(t[i], b[i], len(labels)))
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"mean_difference": round(float(np.mean(diffs)), 4), "ci95": [round(float(lo), 4), round(float(hi), 4)],
            "n_bootstrap": n_boot, "a_better_with_95pct_confidence": bool(lo > 0)}
