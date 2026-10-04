"""Schemas for Artemis message production (outbound messages)."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ArtemisPayload(BaseModel):
    """Base class for Artemis payloads with extra fields allowed."""

    model_config = ConfigDict(extra="allow")


class ArtemisNotificationPlan(ArtemisPayload):
    """Notification plan message for Artemis (to be consumed by EUT)."""

    user_id: UUID = Field(..., description="Target user identifier")
    notifications: List[Dict[str, Any]] = Field(
        ..., description="List of notification items"
    )
    start_time: datetime = Field(..., description="Start of notification window")
    end_time: datetime = Field(..., description="End of notification window")


def create_notification_plan_message(
    user_id: UUID,
    notifications: List[Dict[str, Any]],
    start_time: datetime,
    end_time: datetime,
) -> Dict[str, Any]:
    """Create a properly formatted Artemis message for notification plans."""
    notification_plan = ArtemisNotificationPlan(
        user_id=user_id,
        notifications=notifications,
        start_time=start_time,
        end_time=end_time,
    )
    return {"notification_plan": notification_plan.model_dump(mode="json")}
