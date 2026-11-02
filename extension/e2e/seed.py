"""Seeds a throwaway backend database for the browser end-to-end run. Prints a JSON line describing the seeded user.

Run from backend/ with the backend virtualenv (the e2e runner does this):
    COMPASS_DATABASE_URL=sqlite:///... COMPASS_AUTH_SECRET=... python ../extension/e2e/seed.py

No credentials are created here: the runner signs in the way a real user does, with a one-time pairing code issued by
`python -m app.cli issue-pairing-code`. Everything is fictional test data (repository acme/ml-api); no real GitHub data is used.
"""

import json
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")  # backend/ (also makes the `tests` helpers importable)

from app.db.models import Issue, Repository, User
from app.db.session import get_sessionmaker
from app.schemas.profile import ProfileIn
from app.services.profile import create_profile
from tests import intel_data as D


def main() -> None:
    now = datetime.now(timezone.utc)
    with get_sessionmaker()() as db:
        user = User(github_user_id=1, username="e2e-user")
        db.add(user)
        db.commit()
        create_profile(db, user.id, ProfileIn.model_validate({
            "skills": [{"name": "python", "category": "language", "proficiency": 0.9},
                       {"name": "fastapi", "category": "framework", "proficiency": 0.6}],
            "interests": ["machine learning"], "preferred_difficulty": 2, "experience_level": "beginner",
        }))
        repo = Repository(owner="acme", name="ml-api", default_branch="main", primary_language="Python", topics=["machine-learning", "fastapi"],
                          archived=False, pushed_at=now - timedelta(days=4), last_fetched_at=now, issues_synced_at=now, license_spdx="MIT")
        db.add(repo)
        db.commit()
        for number, title, body, labels in [
            (7, "Parser crashes on load", "Machine learning pipeline crashes when loading data with FastAPI. " + "Steps to reproduce: run the loader. " * 4, ["easy", "bug"]),
            (8, "Add pagination to the issues endpoint", "We should add limit and offset to the endpoint. " * 4, ["help wanted"]),
            (9, "Document the loader module", "The loader module has no documentation. Please describe its options. " * 4, ["documentation"]),
        ]:
            db.add(Issue(repository_id=repo.id, issue_number=number, title=title, body=body, state="open", labels=[{"name": l} for l in labels],
                         assignees=[], created_at=now - timedelta(days=10), updated_at=now - timedelta(days=1), fetched_at=now))
        db.commit()
        D.store_snapshot(db, repo, "healthy")  # a fresh stored analysis, so the health report needs no GitHub call
        print(json.dumps({"user_id": user.id, "github_id": 1, "username": "e2e-user"}))


if __name__ == "__main__":
    main()
