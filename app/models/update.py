import uuid
from datetime import datetime
from typing import Dict, List, Optional

from sqlmodel import JSON, Field, SQLModel


class User(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(unique=True, index=True)
    gender: Optional[str] = Field(default=None)
    height: Optional[int] = Field(default=None)
    user_age: Optional[int] = Field(default=None)
    weight: Optional[int] = Field(default=None)
    wearable: Optional[str] = Field(default=None)
    residence: Optional[str] = Field(default=None)
    enrolment_date: Optional[str] = Field(default=None)
    informed_consent: Optional[str] = Field(default=None)
    recruitment_center: Optional[str] = Field(default=None)
    level: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)


class UserFeedback(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    events: List[Dict] = Field(sa_type=JSON)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)


class HealthHabitAssessment(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    hhs: Dict = Field(sa_type=JSON)
    assessment_timestamp: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)


class NewMissionContent(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    selection_timestamp: Optional[datetime] = Field(default=None)
    new_missions: List[Dict] = Field(sa_type=JSON)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
