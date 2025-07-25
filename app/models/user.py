import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import ForeignKey
from sqlmodel import JSON, Field, SQLModel


class User(SQLModel, table=True):
    __tablename__ = "User"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(unique=True, index=True)
    characteristics: Dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)

    def deleteUser(self, date_disabled: datetime):
        self.is_deleted = True
        self.deleted_at = date_disabled


class UserFeedback(SQLModel, table=True):
    __tablename__ = "UserFeedback"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.user_id")
    events: List[Dict] = Field(sa_type=JSON)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)


class HealthHabitAssessment(SQLModel, table=True):
    __tablename__ = "HealthHabitAssessment"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.user_id")
    hhs: Dict = Field(sa_type=JSON)
    assessment_timestamp: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)


class MissionContent(SQLModel, table=True):
    __tablename__ = "MissionContent"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.user_id")
    selection_timestamp: Optional[datetime] = Field(default=None)
    mission_id: str = Field(index=True, foreign_key="Mission.mission_id")
    mission_start_time: Optional[datetime] = Field(default=None)
    mission_end_time: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
