"""Versioned provider-neutral records. All unknown fields fail validation."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Kind(str, Enum):
    task = "task"
    decision = "decision"
    question = "question"
    debugging = "debugging"
    debt = "debt"
    project = "project"
    review = "review"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = 1
    id: UUID = Field(default_factory=uuid4)
    kind: Kind
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    project_id: str | None = None
    repository: str = ""
    technologies: list[str] = Field(default_factory=list)
    affected_components: list[str] = Field(default_factory=list)
    related_task_ids: list[UUID] = Field(default_factory=list)
    status: str = "unknown"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    source_agent: Literal["claude", "codex", "cursor", "manual", "import"] = "manual"
    source_session: str = ""
    verified_facts: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    content_hash: str = ""
    # Tasks
    branch: str = ""
    task_id: str = ""
    pull_request_id: str = ""
    commit_ids: list[str] = Field(default_factory=list)
    problem_statement: str = ""
    requirements: list[str] = Field(default_factory=list)
    implementation_summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests_performed: list[str] = Field(default_factory=list)
    technical_decisions: list[UUID] = Field(default_factory=list)
    debugging_references: list[UUID] = Field(default_factory=list)
    lessons_learned: list[str] = Field(default_factory=list)
    mistakes: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    follow_up_actions: list[str] = Field(default_factory=list)
    # Decisions
    context: str = ""
    problem: str = ""
    chosen_approach: str = ""
    alternatives: list[str] = Field(default_factory=list)
    rejected_alternatives: list[str] = Field(default_factory=list)
    rationale: str = ""
    trade_offs: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    reversibility: str = "unknown"
    review_date: datetime | None = None
    # Questions and review history
    question: str = ""
    expected_answer: str = ""
    explanation: str = ""
    difficulty: Literal["beginner", "intermediate", "advanced"] = "intermediate"
    technology: str = ""
    topic: str = ""
    question_source: Literal["quiz", "I asked", "should have asked"] = "quiz"
    review_schedule: list[int] = Field(default_factory=lambda: [1, 7, 14, 30])
    next_review_at: datetime | None = None
    review_count: int = Field(default=0, ge=0)
    correct_count: int = Field(default=0, ge=0)
    confidence_score: float = Field(default=0, ge=0, le=1)
    question_id: UUID | None = None
    user_answer: str = ""
    score: float = Field(default=0, ge=0, le=1)
    feedback: str = ""
    evaluator: Literal["human", "agent"] = "human"
    # Debugging
    symptoms: list[str] = Field(default_factory=list)
    environment: str = ""
    error_messages: list[str] = Field(default_factory=list)
    investigation_timeline: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    incorrect_assumptions: list[str] = Field(default_factory=list)
    failed_approaches: list[str] = Field(default_factory=list)
    root_cause: str = ""
    resolution: str = ""
    prevention_strategy: str = ""
    monitoring_recommendations: list[str] = Field(default_factory=list)
    # Technical debt
    category: str = ""
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    severity: str = ""
    impact: str = ""
    workaround: str = ""
    proposed_solution: str = ""
    owner: str = ""
    target_date: date | None = None
    # Projects
    name: str = ""
    services: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    architecture_summary: str = ""

    @field_validator("title")
    @classmethod
    def meaningful_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must contain text")
        return value.strip()

    @field_validator("review_schedule")
    @classmethod
    def positive_intervals(cls, value: list[int]) -> list[int]:
        if not value or any(x < 1 for x in value) or value != sorted(set(value)):
            raise ValueError("review_schedule must be increasing positive day intervals")
        return value

    @field_validator("created_at", "updated_at", "completed_at", "review_date", "next_review_at")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("timestamps must include a UTC offset")
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def validate_kind(self) -> Record:
        if self.kind == Kind.question and (not self.question or not self.expected_answer):
            raise ValueError("questions require question and expected_answer")
        if self.kind == Kind.review and (not self.question_id or not self.user_answer):
            raise ValueError("reviews require question_id and user_answer")
        if self.correct_count > self.review_count:
            raise ValueError("correct_count cannot exceed review_count")
        return self

    def sealed(self) -> Record:
        data = self.model_dump(mode="json")
        for key in ("id", "created_at", "updated_at", "content_hash"):
            data.pop(key)
        digest = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self.model_copy(update={"content_hash": digest})


def migrate_record(data: dict) -> Record:
    """Only published schema v1 is accepted; future versions must not be guessed."""
    if data.get("schema_version", 1) != 1:
        raise ValueError("unsupported schema version; upgrade EKM before importing")
    return Record.model_validate(data)


def verify_integrity(record: Record) -> Record:
    """Detect accidental corruption, not authenticate a remote editor's identity."""
    if record.content_hash and record.content_hash != record.sealed().content_hash:
        raise ValueError("record content hash does not match its payload")
    return record
