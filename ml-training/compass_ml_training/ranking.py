"""Offline ranking evaluation of the recommendation scorers against explicit relevance feedback.

This TRAINS NOTHING. It measures how well each stored scoring method (heuristic_v1, heuristic_v2, any future
model) orders the issues a user rated, so methods can be compared on identical items once enough feedback exists.

Cautions baked into every report: the rated items are only ones the user was shown (selection bias, mostly by
the scorer under test), people rate sparsely and mostly positively, "relevant" is not "successful contribution",
and a handful of users cannot support a conclusion. With too little data the report says "insufficient".
"""

import math
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Feedback, Recommendation

BIAS_NOTES = [
    "Rated items were chosen by the recommender being evaluated (selection/position bias).",
    "Few users and few ratings per user; confidence intervals are not computed and differences may be noise.",
    "Negative feedback is rare; missing feedback is not negative feedback.",
    "'Relevant' is a stated opinion, not a successful contribution.",
]


def precision_at_k(labels_in_rank_order: list[int], k: int) -> float:
    top = labels_in_rank_order[:k]
    return sum(top) / len(top) if top else 0.0


def ndcg_at_k(labels_in_rank_order: list[int], k: int) -> float | None:
    """Binary-gain NDCG@k; None when the list has no relevant item (undefined)."""
    def dcg(rels):
        return sum(r / math.log2(i + 2) for i, r in enumerate(rels[:k]))

    ideal = dcg(sorted(labels_in_rank_order, reverse=True))
    return None if ideal == 0 else dcg(labels_in_rank_order) / ideal


def rank_labels(items: list[tuple[int, float, int]]) -> list[int]:
    """items: (item_id, score, label). Highest score first; ties broken by item_id for determinism."""
    return [lab for _, _, lab in sorted(items, key=lambda t: (-t[1], t[0]))]


def load_relevance_dataset(db: Session) -> dict[int, dict]:
    """{user_id: {"labels": {issue_id: 0/1}, "scores": {method: {issue_id: score}}}} from stored data.

    A label is the user's LATEST explicit `relevance` feedback for the issue; a score is the latest stored
    recommendation of that method for that user and issue.
    """
    data: dict[int, dict] = defaultdict(lambda: {"labels": {}, "scores": defaultdict(dict)})
    for fb in db.scalars(select(Feedback).where(Feedback.feedback_type == "relevance", Feedback.category == "explicit_feedback")
                         .order_by(Feedback.created_at, Feedback.id)):
        data[fb.user_id]["labels"][fb.issue_id] = 1 if fb.feedback_value > 0 else 0
    for rec in db.scalars(select(Recommendation).order_by(Recommendation.created_at, Recommendation.id)):
        if rec.user_id in data and rec.issue_id in data[rec.user_id]["labels"]:
            data[rec.user_id]["scores"][rec.ranking_model_version][rec.issue_id] = rec.final_score
    return data


def evaluate_methods(data: dict[int, dict], methods: list[str], k: int = 5, min_items: int = 5, min_users: int = 5) -> dict:
    """Compare methods on the items BOTH were scored for, per eligible user, then macro-average over users.

    A user is eligible with >= min_items commonly scored rated items including both a relevant and a non-relevant one.
    """
    per_user = []
    for uid, d in sorted(data.items()):
        common = set(d["labels"])
        for m in methods:
            common &= set(d["scores"].get(m, {}))
        labels = [d["labels"][i] for i in common]
        if len(common) < min_items or sum(labels) == 0 or sum(labels) == len(labels):
            continue
        row = {"user": uid, "items": len(common), "positive_rate": sum(labels) / len(labels)}
        for m in methods:
            ranked = rank_labels([(i, d["scores"][m][i], d["labels"][i]) for i in common])
            row[m] = {"p_at_k": precision_at_k(ranked, k), "ndcg_at_k": ndcg_at_k(ranked, k)}
        per_user.append(row)
    report = {"k": k, "methods": methods, "eligible_users": len(per_user), "min_users_required": min_users,
              "total_rated_users": len(data), "bias_notes": BIAS_NOTES}
    if len(per_user) < min_users:
        report["status"] = "insufficient"
        report["reason"] = f"{len(per_user)} eligible user(s); need >= {min_users} with >= {min_items} rated, commonly scored items each"
        return report
    mean = lambda xs: round(sum(xs) / len(xs), 4)  # noqa: E731
    report["status"] = "ok"
    report["baseline_random_p_at_k"] = mean([u["positive_rate"] for u in per_user])  # expected P@K of a random order
    report["results"] = {m: {"p_at_k": mean([u[m]["p_at_k"] for u in per_user]),
                             "ndcg_at_k": mean([u[m]["ndcg_at_k"] for u in per_user if u[m]["ndcg_at_k"] is not None])}
                         for m in methods}
    return report
