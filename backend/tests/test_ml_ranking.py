"""Offline ranking evaluation of stored scorers against explicit relevance feedback (trains nothing)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Feedback, Issue, Recommendation, Repository, User
from compass_ml_training import ranking as rk


def test_precision_at_k_hand_computed():
    assert rk.precision_at_k([1, 0, 1, 0, 0], 3) == pytest.approx(2 / 3)
    assert rk.precision_at_k([1, 1], 5) == 1.0  # fewer items than k: judged on those available
    assert rk.precision_at_k([], 3) == 0.0


def test_ndcg_at_k_hand_computed():
    # DCG@3 = 1/log2(2) + 0 + 1/log2(4) = 1.5 ; ideal [1,1,0] = 1 + 1/log2(3) = 1.6309
    assert rk.ndcg_at_k([1, 0, 1, 0, 0], 3) == pytest.approx(1.5 / 1.6309297, abs=1e-4)
    assert rk.ndcg_at_k([1, 1, 0], 3) == pytest.approx(1.0)
    assert rk.ndcg_at_k([0, 0, 1], 3) == pytest.approx(0.5)  # relevant item at rank 3: (1/2) / 1
    assert rk.ndcg_at_k([0, 0, 0], 3) is None  # undefined without a relevant item


def test_ranking_order_and_deterministic_ties():
    items = [(1, 0.5, 0), (2, 0.9, 1), (3, 0.5, 1)]
    assert rk.rank_labels(items) == [1, 0, 1]  # 0.9 first; tie between 1 and 3 broken by item id
    assert rk.rank_labels(list(reversed(items))) == [1, 0, 1]


def users(n, good_scores=True):
    """n users, 6 items each with 2 relevant; method 'perfect' ranks relevant first, 'reversed' ranks them last."""
    data = {}
    for u in range(n):
        labels = {i: (1 if i < 2 else 0) for i in range(6)}
        data[u] = {"labels": labels, "scores": {"perfect": {i: 10 - i for i in labels}, "reversed": {i: i for i in labels}}}
    return data


def test_methods_are_compared_on_identical_items():
    report = rk.evaluate_methods(users(6), ["perfect", "reversed"], k=2, min_users=5)
    assert report["status"] == "ok" and report["eligible_users"] == 6
    assert report["results"]["perfect"] == {"p_at_k": 1.0, "ndcg_at_k": 1.0}
    assert report["results"]["reversed"]["p_at_k"] == 0.0 and report["results"]["reversed"]["ndcg_at_k"] < 0.5
    assert report["baseline_random_p_at_k"] == pytest.approx(2 / 6, abs=1e-3)  # prevalence = what a random order achieves
    assert len(report["bias_notes"]) >= 4 and any("selection" in n for n in report["bias_notes"])


def test_insufficient_data_is_reported_not_papered_over():
    report = rk.evaluate_methods(users(2), ["perfect", "reversed"], min_users=5)
    assert report["status"] == "insufficient" and "results" not in report and "need >= 5" in report["reason"]
    assert rk.evaluate_methods({}, ["a"])["status"] == "insufficient"


def test_users_without_both_classes_or_enough_items_are_not_eligible():
    data = users(6)
    data[0]["labels"] = {i: 1 for i in range(6)}  # all relevant
    data[1]["labels"] = {i: 0 for i in range(6)}  # none relevant
    data[2]["labels"] = {0: 1, 1: 0}  # too few items
    report = rk.evaluate_methods(data, ["perfect", "reversed"], min_users=1)
    assert report["eligible_users"] == 3 and report["total_rated_users"] == 6


def test_items_not_scored_by_every_method_are_excluded_from_the_comparison():
    data = users(6)
    for d in data.values():
        del d["scores"]["reversed"][5]
    assert rk.evaluate_methods(data, ["perfect", "reversed"], min_items=6, min_users=1)["eligible_users"] == 0
    assert rk.evaluate_methods(data, ["perfect", "reversed"], min_items=5, min_users=1)["eligible_users"] == 6


# ---------------------------------------------------------------- loading real stored data


def seed(db):
    now = datetime.now(timezone.utc)
    repo = Repository(owner="o", name="r")
    db.add(repo)
    db.flush()
    issues = [Issue(repository_id=repo.id, issue_number=n, title="t", state="open", labels=[], created_at=now, updated_at=now,
                    fetched_at=now) for n in range(1, 7)]
    users_ = [User(github_user_id=i, username=f"u{i}") for i in (1, 2)]
    db.add_all(issues + users_)
    db.commit()
    return issues, users_


def fb(db, user, issue, value, ftype="relevance", category="explicit_feedback", minutes=0):
    db.add(Feedback(user_id=user.id, issue_id=issue.id, feedback_type=ftype, feedback_value=value, category=category,
                    source="user", created_at=datetime.now(timezone.utc) + timedelta(minutes=minutes)))


def rec(db, user, issue, model, score, minutes=0):
    db.add(Recommendation(user_id=user.id, issue_id=issue.id, final_score=score, ranking_model_version=model, input_hash=f"{model}{score}{minutes}",
                          components={}, created_at=datetime.now(timezone.utc) + timedelta(minutes=minutes)))


def test_load_relevance_dataset_uses_latest_explicit_relevance_and_latest_scores(db_session):
    issues, (alice, bob) = seed(db_session)
    fb(db_session, alice, issues[0], 1, minutes=0)
    fb(db_session, alice, issues[0], -1, minutes=5)  # alice changed her mind: latest wins
    fb(db_session, alice, issues[1], 1)
    fb(db_session, alice, issues[2], 1, ftype="interest")  # not relevance feedback
    fb(db_session, alice, issues[3], 1, ftype="saved", category="event")  # an observed event is not a rating
    fb(db_session, bob, issues[0], 1)
    rec(db_session, alice, issues[0], "heuristic_v1", 40, minutes=-10)
    rec(db_session, alice, issues[0], "heuristic_v1", 55, minutes=-1)  # newer score replaces the older one
    rec(db_session, alice, issues[0], "heuristic_v2", 60)
    rec(db_session, alice, issues[2], "heuristic_v1", 70)
    rec(db_session, bob, issues[0], "heuristic_v1", 10)
    db_session.commit()
    data = rk.load_relevance_dataset(db_session)
    a = data[alice.id]
    assert a["labels"] == {issues[0].id: 0, issues[1].id: 1}
    assert a["scores"]["heuristic_v1"] == {issues[0].id: 55}
    assert a["scores"]["heuristic_v2"] == {issues[0].id: 60}
    assert data[bob.id]["labels"] == {issues[0].id: 1} and data[bob.id]["scores"]["heuristic_v1"] == {issues[0].id: 10}  # isolated


def test_with_realistic_small_data_the_report_is_insufficient(db_session):
    issues, (alice, _) = seed(db_session)
    for i, issue in enumerate(issues):
        fb(db_session, alice, issue, 1 if i < 2 else -1)
        rec(db_session, alice, issue, "heuristic_v1", 50 - i)
        rec(db_session, alice, issue, "heuristic_v2", 40 + i)
    db_session.commit()
    data = rk.load_relevance_dataset(db_session)
    assert rk.evaluate_methods(data, ["heuristic_v1", "heuristic_v2"])["status"] == "insufficient"  # one user is not evidence
    one = rk.evaluate_methods(data, ["heuristic_v1", "heuristic_v2"], k=2, min_users=1)
    assert one["status"] == "ok" and one["results"]["heuristic_v1"]["p_at_k"] == 1.0 and one["results"]["heuristic_v2"]["p_at_k"] == 0.0
