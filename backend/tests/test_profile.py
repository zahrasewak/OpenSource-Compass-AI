import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.security import issue_token
from app.db.models import User, UserPreference, UserSkill

URL = "/api/v1/profile"
SETTINGS = Settings(auth_secret="s" * 48, _env_file=None)

PROFILE = {
    "skills": [
        {"name": "Python", "category": "language", "proficiency": 0.8},
        {"name": "FastAPI", "category": "framework", "proficiency": 0.5},
    ],
    "interests": ["Machine Learning", "web apis"],
    "contribution_goals": ["Learn testing"],
    "preferred_task_types": ["bug_fixing", "documentation"],
    "preferred_difficulty": 2,
    "experience_level": "beginner",
}


@pytest.fixture
def app_client(client):
    client.app.dependency_overrides[get_settings] = lambda: SETTINGS
    return client


@pytest.fixture
def alice(db_session):
    u = User(github_user_id=1, username="alice")
    db_session.add(u)
    db_session.commit()
    return {"Authorization": f"Bearer {issue_token(u.id, SETTINGS)}"}, u


@pytest.fixture
def bob(db_session):
    u = User(github_user_id=2, username="bob")
    db_session.add(u)
    db_session.commit()
    return {"Authorization": f"Bearer {issue_token(u.id, SETTINGS)}"}, u


def test_create_and_get(app_client, alice):
    h, _ = alice
    r = app_client.post(URL, json=PROFILE, headers=h)
    assert r.status_code == 201
    body = r.json()
    assert [s["name"] for s in body["skills"]] == ["fastapi", "python"]  # normalised + sorted
    assert body["interests"] == ["machine learning", "web apis"]
    assert body["preferred_difficulty"] == 2 and body["experience_level"] == "beginner"
    assert body["skills"][0]["source"] == "manual"
    assert app_client.get(URL, headers=h).json() == body


def test_get_before_create_is_404(app_client, alice):
    r = app_client.get(URL, headers=alice[0])
    assert r.status_code == 404 and r.json()["error"]["code"] == "profile_not_found"


def test_duplicate_create_is_409(app_client, alice):
    app_client.post(URL, json=PROFILE, headers=alice[0])
    r = app_client.post(URL, json=PROFILE, headers=alice[0])
    assert r.status_code == 409 and r.json()["error"]["code"] == "profile_exists"


def test_minimal_profile_allowed(app_client, alice):
    r = app_client.post(URL, json={}, headers=alice[0])
    assert r.status_code == 201
    assert r.json()["skills"] == [] and r.json()["preferred_difficulty"] is None


def test_put_replaces_and_keeps_unchanged_rows(app_client, alice, db_session):
    h, u = alice
    app_client.post(URL, json=PROFILE, headers=h)
    python_id = db_session.scalar(select(UserSkill.id).where(UserSkill.skill_name == "python"))
    update = {
        "skills": [{"name": "python", "category": "language", "proficiency": 0.9},
                   {"name": "rust", "category": "language", "proficiency": 0.2}],
        "interests": ["compilers"],
        "preferred_difficulty": 4,
    }
    r = app_client.put(URL, json=update, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert [(s["name"], s["proficiency"]) for s in body["skills"]] == [("python", 0.9), ("rust", 0.2)]
    assert body["interests"] == ["compilers"] and body["contribution_goals"] == []
    assert body["preferred_difficulty"] == 4 and body["experience_level"] is None
    assert db_session.scalar(select(UserSkill.id).where(UserSkill.skill_name == "python")) == python_id
    assert db_session.scalar(select(UserSkill).where(UserSkill.skill_name == "fastapi")) is None


def test_put_without_profile_is_404(app_client, alice):
    assert app_client.put(URL, json=PROFILE, headers=alice[0]).status_code == 404


@pytest.mark.parametrize("method", ["post", "get", "put"])
def test_auth_required(app_client, method):
    kwargs = {} if method == "get" else {"json": PROFILE}
    r = getattr(app_client, method)(URL, **kwargs)
    assert r.status_code == 401
    r = getattr(app_client, method)(URL, headers={"Authorization": "Bearer nope"}, **kwargs)
    assert r.status_code == 401


@pytest.mark.parametrize(
    "patch",
    [
        {"preferred_difficulty": 0},
        {"preferred_difficulty": 6},
        {"experience_level": "wizard"},
        {"preferred_task_types": ["bogus"]},
        {"skills": [{"name": "python", "proficiency": 1.5}]},
        {"skills": [{"name": "python", "proficiency": -0.1}]},
        {"skills": [{"name": "  ", "proficiency": 0.5}]},
        {"skills": [{"name": "python", "category": "magic", "proficiency": 0.5}]},
        {"skills": [{"name": "python"}]},
        {"skills": [{"name": "Python", "proficiency": 0.5}, {"name": "python", "proficiency": 0.6}]},
        {"interests": ["ML", "ml"]},
        {"contribution_goals": ["a", "A"]},
        {"preferred_task_types": ["testing", "testing"]},
        {"interests": ["x" * 101]},
        {"skills": [{"name": f"s{i}", "proficiency": 0.1} for i in range(101)]},
        {"unknown_field": 1},
    ],
)
def test_invalid_input_422(app_client, alice, patch):
    r = app_client.post(URL, json={**PROFILE, **patch}, headers=alice[0])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["details"]
    assert app_client.get(URL, headers=alice[0]).status_code == 404  # nothing was stored


def test_invalid_update_leaves_profile_unchanged(app_client, alice):
    h, _ = alice
    original = app_client.post(URL, json=PROFILE, headers=h).json()
    assert app_client.put(URL, json={"preferred_difficulty": 9}, headers=h).status_code == 422
    assert app_client.get(URL, headers=h).json() == original


@pytest.mark.parametrize("field", ["user_id", "id", "github_user_id"])
def test_client_supplied_identity_rejected(app_client, alice, bob, field):
    _, b = bob
    for method in (app_client.post, app_client.put):
        r = method(URL, json={**PROFILE, field: b.id}, headers=alice[0])
        assert r.status_code == 422
    assert app_client.post(f"{URL}?user_id={b.id}", json=PROFILE, headers=alice[0]).status_code == 201


def test_cross_user_isolation(app_client, alice, bob, db_session):
    ha, ua = alice
    hb, ub = bob
    app_client.post(URL, json=PROFILE, headers=ha)

    # Bob cannot see Alice's profile through GET, with or without a spoofed id.
    assert app_client.get(URL, headers=hb).status_code == 404
    assert app_client.get(f"{URL}?user_id={ua.id}", headers=hb).status_code == 404
    assert app_client.get(URL, headers={**hb, "X-User-Id": str(ua.id)}).status_code == 404

    # Bob's PUT cannot modify Alice's profile (he has none, so 404).
    assert app_client.put(f"{URL}?user_id={ua.id}", json={"interests": ["hacked"]}, headers=hb).status_code == 404

    # Bob creates his own; Alice's data is untouched, and vice versa.
    rb = app_client.post(URL, json={"skills": [{"name": "go", "proficiency": 0.3}]}, headers=hb)
    assert rb.status_code == 201
    app_client.put(URL, json={"skills": [{"name": "go", "proficiency": 0.3}], "interests": ["bob-only"]}, headers=hb)

    alice_view = app_client.get(URL, headers=ha).json()
    assert alice_view["interests"] == ["machine learning", "web apis"]
    assert {s["name"] for s in alice_view["skills"]} == {"python", "fastapi"}
    bob_view = app_client.get(URL, headers=hb).json()
    assert bob_view["interests"] == ["bob-only"] and [s["name"] for s in bob_view["skills"]] == ["go"]

    # Same skill name for two users is allowed (uniqueness is per user).
    app_client.put(URL, json={"skills": [{"name": "python", "proficiency": 0.1}]}, headers=hb)
    assert db_session.query(UserSkill).filter_by(skill_name="python").count() == 2
    assert db_session.query(UserPreference).count() == 2
