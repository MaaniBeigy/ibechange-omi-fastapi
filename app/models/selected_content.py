import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import ARRAY, Field, SQLModel, String


class SelectedContent(SQLModel, table=True):
    __tablename__ = "SelectedContent"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True)
    contents: List[str] = Field(sa_type=ARRAY(String))
    mission_start_time: Optional[datetime] = Field(default=None)
    mission_end_time: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
