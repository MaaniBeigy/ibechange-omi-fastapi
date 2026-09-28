from datetime import date, datetime
from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, model_validator


class ContentItem(BaseModel):
    id: str
    type: Literal["recommendation", "resource", "reminder"]
    mission_id: str
    delivery_date: Optional[date] = None
    deadline: Optional[datetime] = None
    mission_process_id: Optional[int] = None
    task_step_id: Optional[str] = None

    @model_validator(mode="after")
    def check_reminder_deadline(self):
        """Keep per-item deadlines separate from mission schedule times."""
        if self.deadline is not None:
            raise ValueError("Deadlines use Artemis steps or mission end times")
        return self


class SelectedContentCreate(BaseModel):
    plan_id: UUID
    contents: List[ContentItem]
    mission_start_time: Optional[datetime] = None
    mission_end_time: Optional[datetime] = None


class SelectedContentList(BaseModel):
    selected_contents: Dict[str, SelectedContentCreate]
