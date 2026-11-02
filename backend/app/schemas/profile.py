"""Request/response models for the profile API."""

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class SkillCategory(str, Enum):
    language = "language"
    framework = "framework"
    library = "library"
    skill = "skill"


class ExperienceLevel(str, Enum):
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"


class TaskType(str, Enum):
    bug_fixing = "bug_fixing"
    documentation = "documentation"
    testing = "testing"
    refactoring = "refactoring"
    feature_development = "feature_development"
    performance_optimization = "performance_optimization"
    dependency_maintenance = "dependency_maintenance"
    other = "other"


def _reject_duplicates(values: list[str], label: str) -> None:
    seen: set[str] = set()
    for v in values:
        if v in seen:
            raise ValueError(f"duplicate {label}: {v!r}")
        seen.add(v)


class SkillIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    category: SkillCategory = SkillCategory.skill
    proficiency: float = Field(ge=0, le=1, description="Self-reported, 0 (none) to 1 (expert)")

    @field_validator("name")
    @classmethod
    def normalise(cls, v: str) -> str:
        return v.lower()  # "Python" and "python" are the same skill


class ProfileIn(BaseModel):
    """Full profile body used by both POST and PUT. Unknown fields (e.g. user_id) are rejected."""

    model_config = ConfigDict(extra="forbid")

    skills: list[SkillIn] = Field(default_factory=list, max_length=100)
    interests: list[Name] = Field(default_factory=list, max_length=50)
    contribution_goals: list[Name] = Field(default_factory=list, max_length=20)
    preferred_task_types: list[TaskType] = Field(default_factory=list)
    preferred_difficulty: int | None = Field(default=None, ge=1, le=5)
    experience_level: ExperienceLevel | None = None

    @field_validator("interests", "contribution_goals")
    @classmethod
    def normalise_and_reject_duplicates(cls, v: list[str]) -> list[str]:
        lowered = [x.lower() for x in v]
        _reject_duplicates(lowered, "entry")
        return lowered

    @field_validator("preferred_task_types")
    @classmethod
    def unique_task_types(cls, v: list[TaskType]) -> list[TaskType]:
        _reject_duplicates([t.value for t in v], "task type")
        return v

    @model_validator(mode="after")
    def unique_skills(self) -> "ProfileIn":
        _reject_duplicates([s.name for s in self.skills], "skill")
        return self


class SkillOut(BaseModel):
    name: str
    category: str
    proficiency: float
    source: str


class ProfileOut(BaseModel):
    skills: list[SkillOut]
    interests: list[str]
    contribution_goals: list[str]
    preferred_task_types: list[str]
    preferred_difficulty: int | None
    experience_level: str | None
    updated_at: datetime
