import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlmodel import JSON, Field, SQLModel


class UserFeedback(SQLModel, table=True):
    __tablename__ = "UserFeedback"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    events: List[Dict] = Field(sa_type=JSON)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)


class HealthHabitAssessment(SQLModel, table=True):
    __tablename__ = "HealthHabitAssessment"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    hhs: Dict = Field(sa_type=JSON)
    assessment_timestamp: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)


class NewMissionContent(SQLModel, table=True):
    __tablename__ = "NewMissionContent"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    selection_timestamp: Optional[datetime] = Field(default=None)
    mission_id: str = Field(sa_type=JSON)
    mission_start_time: Optional[datetime] = Field(default=None)
    mission_end_time: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
