import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    __tablename__ = "User"
    
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
