"""SYNTHETIC data generator, used ONLY by tests to exercise the pipeline.

Nothing trained on this data may be described as meaningful: the classes are separable by construction, so
good scores here prove the code works, not that the approach works on real issues. Every record carries
source="synthetic_test", which makes training mark the artifact as synthetic and makes the loader refuse it.
"""

import random

from app.ml.schema import IssueRecord
from compass_ml_training.dataset import make_record_id

LEVEL_LABEL = {1: "good first issue", 2: "easy", 3: "medium", 4: "hard", 5: "expert"}
LEVEL_WORDS = {
    1: "typo readme spelling docs link wording comment",
    2: "rename message tooltip padding color label",
    3: "refactor endpoint pagination validation cache config",
    4: "concurrency deadlock scheduler migration protocol streaming",
    5: "consensus kernel allocator compiler lockfree distributed",
}
NOISE = ("the a is when we should this issue to for with and not on in it of can be fix add update use when "
         "after before always sometimes users project module function value result error").split()


def synthetic_records(n_repos=12, per_repo=60, seed=0, probs=(0.25, 0.25, 0.2, 0.2, 0.1), leak_labels_in_text=True,
                      informative=True, languages=("Python", "Go", "Rust")) -> list[IssueRecord]:
    rng = random.Random(seed)
    records = []
    for r in range(n_repos):
        repo = f"org{r}/proj{r}"
        for i in range(1, per_repo + 1):
            level = rng.choices([1, 2, 3, 4, 5], weights=probs)[0]
            words = rng.choices(NOISE, k=rng.randint(8, 25))
            if informative:
                words += rng.choices(LEVEL_WORDS[level].split(), k=rng.randint(2, 5))
            if leak_labels_in_text:
                words.append(f"(maintainers say: {LEVEL_LABEL[level]})")  # must be scrubbed, never learned
            rng.shuffle(words)
            records.append(IssueRecord(
                record_id=make_record_id(repo, i), repo=repo, issue_number=i,
                html_url=f"https://github.com/{repo}/issues/{i}", title=f"Problem {r}-{i} " + " ".join(words[:4]),
                body=f"id{r}x{i} " + " ".join(words), labels=[LEVEL_LABEL[level], "bug"],
                author_association=rng.choice(["NONE", "MEMBER", "CONTRIBUTOR"]),
                repo_language=languages[r % len(languages)], source="synthetic_test"))
    return records
