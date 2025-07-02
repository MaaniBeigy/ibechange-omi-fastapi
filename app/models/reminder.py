import uuid
from datetime import datetime
from typing import Dict, List, Optional

from sqlmodel import JSON, Field, SQLModel


class Reminder(SQLModel, table=True):
    __tablename__ = "Reminder"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    reminders: List[Dict] = Field(sa_type=JSON)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
