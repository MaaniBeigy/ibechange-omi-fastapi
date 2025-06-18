import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Mission(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    mission_id: str = Field(unique=True, index=True)
    weekly_frequency: int
    mission_start_time: Optional[datetime] = Field(default=None)
    mission_end_time: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
