import uuid
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlmodel import JSON, Field, SQLModel


class SelectedContent(SQLModel, table=True):
    __tablename__ = "SelectedContent"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True, foreign_key="User.id")
    source_plan_id: Optional[str] = None
    source_created_at: Optional[datetime] = None
    delivery_date: Optional[date] = Field(default=None, index=True)
    task_scheduled_for: Optional[datetime] = None
    mission_id: Optional[str] = None
    mission_process_id: Optional[int] = None
    contents: List[dict] = Field(default_factory=list, sa_type=JSON)
    mission_start_time: Optional[datetime] = Field(default=None)
    mission_end_time: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
