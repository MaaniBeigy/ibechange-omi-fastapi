"""Durable per-user training checkpoints and delayed-feedback transitions."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class PolicyLearner(SQLModel, table=True):
    __tablename__ = "PolicyLearner"
    __table_args__ = (
        UniqueConstraint("user_id", "device", "base_version", name="uq_policy_learner"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.id")
    device: str
    base_version: str
    checkpoint: dict = Field(sa_type=JSONB)
    policy_timezone: str
    started_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class PolicyCheckpoint(SQLModel, table=True):
    """Optimizer state at initialization and each refit, with a replay cursor."""

    __tablename__ = "PolicyCheckpoint"
    learner_id: uuid.UUID = Field(primary_key=True, foreign_key="PolicyLearner.id")
    saved_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), primary_key=True)
    )
    checkpoint: dict = Field(sa_type=JSONB)


class PolicyTrainingUpdate(SQLModel, table=True):
    """One committed optimizer step, with all arrays in minibatch order."""

    __tablename__ = "PolicyTrainingUpdate"
    learner_id: uuid.UUID = Field(primary_key=True, foreign_key="PolicyLearner.id")
    update_number: int = Field(primary_key=True)
    fitted_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    learning_rate: float
    cql_alpha: float
    td_loss: float
    cql_loss: float
    total_loss: float
    td_error: float
    offline_indices: list[int] = Field(sa_type=JSONB)
    personal_replay_indices: list[int] = Field(sa_type=JSONB)
    q_values: list[list[float]] = Field(sa_type=JSONB)
    targets: list[float] = Field(sa_type=JSONB)
    td_residuals: list[float] = Field(sa_type=JSONB)


class PolicyTransition(SQLModel, table=True):
    __tablename__ = "PolicyTransition"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_policy_transition_decision"),
        UniqueConstraint(
            "user_id",
            "device",
            "base_version",
            "replay_index",
            name="uq_policy_replay_index",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.id")
    decision_id: uuid.UUID = Field(foreign_key="PolicyDecision.id")
    next_decision_id: uuid.UUID | None = Field(
        default=None, foreign_key="PolicyDecision.id"
    )
    device: str
    base_version: str
    state: list[float] = Field(sa_type=JSONB)
    next_state: list[float] = Field(sa_type=JSONB)
    action: int
    safe_mask: list[bool] = Field(sa_type=JSONB)
    next_safe_mask: list[bool] = Field(sa_type=JSONB)
    elapsed_min: float
    bootstrap: int
    status: str = Field(default="pending", index=True)
    objectives: dict | None = Field(default=None, sa_type=JSONB)
    reward: float | None = None
    outcome_event_id: uuid.UUID | None = Field(default=None, foreign_key="Event.id")
    opened_event_id: uuid.UUID | None = Field(default=None, foreign_key="Event.id")
    released_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    replay_index: int | None = None
    consumed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
