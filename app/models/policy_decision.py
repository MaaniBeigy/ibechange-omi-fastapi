import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class PolicyDecision(SQLModel, table=True):
    __tablename__ = "PolicyDecision"
    __table_args__ = (
        UniqueConstraint("user_id", "plan_id", "decided_at", name="uq_policy_epoch"),
        UniqueConstraint(
            "user_id",
            "decided_at",
            "superseded_by",
            name="uq_policy_user_epoch",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.id")
    plan_id: uuid.UUID = Field(index=True, foreign_key="SelectedContent.id")
    superseded_by: Optional[uuid.UUID] = Field(
        default=None, foreign_key="SelectedContent.id"
    )
    occurrence_index: int
    decided_at: datetime
    action: int
    propensity: float
    state: List[float] = Field(sa_type=JSONB)
    safe_mask: List[bool] = Field(sa_type=JSONB)
    policy_version: str
    outcome_event_id: Optional[uuid.UUID] = Field(default=None, foreign_key="Event.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
