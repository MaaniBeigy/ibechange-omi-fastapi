import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ArtemisInbox(SQLModel, table=True):
    __tablename__ = "ArtemisInbox"

    message_id: str = Field(primary_key=True)
    body_sha256: str
    event_name: str = Field(index=True)
    processed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ArtemisFailure(SQLModel, table=True):
    __tablename__ = "ArtemisFailure"

    message_id: str = Field(primary_key=True)
    body_sha256: str
    reason: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PsychologicalAssessment(SQLModel, table=True):
    __tablename__ = "PsychologicalAssessment"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(
        default=None, index=True, foreign_key="User.id"
    )
    pillar_id: str
    subpillar: Optional[str] = None
    barriers: List[str] = Field(sa_type=JSONB)
    answer_tasklog: Dict[str, Any] = Field(sa_type=JSONB)
    assessment_timestamp: datetime
    raw_payload: Optional[Dict[str, Any]] = Field(default=None, sa_type=JSONB)
    source_message_id: Optional[str] = None


class PillarSelection(SQLModel, table=True):
    __tablename__ = "PillarSelection"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.id")
    pillar_id: str
    selected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MissionSelection(SQLModel, table=True):
    __tablename__ = "MissionSelection"
    __table_args__ = (
        UniqueConstraint("user_id", "mission_process_id", name="uq_mission_selection"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.id")
    mission_id: uuid.UUID = Field(index=True, foreign_key="Mission.id")
    process_id: int
    mission_process_id: int = Field(index=True)
    selected_at: datetime


class MissionScheduledStep(SQLModel, table=True):
    __tablename__ = "MissionScheduledStep"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "mission_process_id",
            "step_id",
            name="uq_mission_scheduled_step",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.id")
    mission_process_id: int = Field(index=True)
    mission_code: str = Field(index=True)
    step_id: str
    start_date_raw: Optional[str] = None
    end_date_raw: Optional[str] = None
    scheduled_for: Optional[datetime] = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    source_message_id: Optional[str] = None
    is_active: bool = True
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
