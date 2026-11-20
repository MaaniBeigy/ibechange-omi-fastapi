import os
from datetime import date, datetime, timezone
from typing import Dict, List, Literal, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator


def cs_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        zone = ZoneInfo(os.getenv("POLICY_TIMEZONE", "Europe/Berlin"))
        value = value.replace(tzinfo=zone)
        if value.utcoffset() != value.replace(fold=1).utcoffset():
            raise ValueError("Offset required for ambiguous or missing CS clock time")
    return value.astimezone(timezone.utc)


class ContentItem(BaseModel):
    id: str
    type: Literal[
        "recommendation", "resource", "reminder", "hour_reminder", "day_reminder"
    ]
    mission_id: Optional[str] = None
    latest_time: Optional[datetime] = None
    push_text_id: str | int | None = None
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
    plan_id: str = Field(min_length=1)
    created_at: Optional[datetime] = None
    delivery_date: Optional[date] = None
    task_scheduled_for: Optional[datetime] = None
    mission_id: Optional[str] = None
    mission_process_id: Optional[int] = None
    contents: List[ContentItem]
    mission_start_time: Optional[datetime] = None
    mission_end_time: Optional[datetime] = None

    @field_validator("plan_id", mode="before")
    @classmethod
    def normalize_plan_id(cls, value):
        return str(value) if isinstance(value, UUID) else value

    @model_validator(mode="after")
    def normalize_plan(self):
        if self.delivery_date is not None:
            if (
                self.created_at is None
                or not self.mission_id
                or self.mission_process_id is None
            ):
                raise ValueError(
                    "Daily plans require created_at, mission_id and mission_process_id"
                )
            for field in (
                "created_at",
                "task_scheduled_for",
                "mission_start_time",
                "mission_end_time",
            ):
                value = getattr(self, field)
                if value is not None:
                    setattr(self, field, cs_time(value))
        else:
            UUID(self.plan_id)
            if self.task_scheduled_for is not None or self.created_at is not None:
                raise ValueError("Daily plans require delivery_date")
        for item in self.contents:
            if self.delivery_date is not None:
                if (
                    item.mission_id not in (None, self.mission_id)
                    or item.mission_process_id not in (None, self.mission_process_id)
                    or item.delivery_date not in (None, self.delivery_date)
                ):
                    raise ValueError("Content conflicts with its daily plan")
                item.mission_id = self.mission_id
                item.mission_process_id = self.mission_process_id
            if not item.mission_id:
                raise ValueError("Missing mission_id")
            if item.latest_time is not None:
                item.latest_time = cs_time(item.latest_time)
            if item.type == "hour_reminder" and self.task_scheduled_for is None:
                raise ValueError("hour_reminder requires task_scheduled_for")
            if item.type == "day_reminder" and self.delivery_date is None:
                raise ValueError("day_reminder requires a daily plan")
        return self


class SelectedContentList(BaseModel):
    selected_contents: Dict[str, SelectedContentCreate]
