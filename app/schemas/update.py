from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.schemas.artemis_producer import ArtemisNotificationPlan


class ArtemisPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


class ArtemisNewUser(ArtemisPayload):
    uuid: UUID
    username: str
    email: str
    personalId: Optional[str] = None
    language: Optional[str] = None
    enabled: bool
    attributes: Dict[str, Any]
    tenantName: Optional[str] = None


class ArtemisDisabledUser(ArtemisPayload):
    user_uuid: UUID
    date_disabled: datetime


class ArtemisSubpillar(ArtemisPayload):
    score: float
    colour: str
    name: str


class ArtemisHealthHabitAssessment(ArtemisPayload):
    user_uuid: UUID
    pillar_id: str
    score: float
    colour: str
    trend: float
    subpillars: List[ArtemisSubpillar]
    assessment_timestamp: datetime


class ArtemisPsychologicalAssessment(ArtemisPayload):
    user_uuid: Optional[UUID] = None
    pillar_id: str
    barriers: List[str]
    subpillar: Optional[str] = None
    suppillar: Optional[str] = None
    assessment_timestamp: datetime
    answer_tasklog: Dict[str, Any]


class ArtemisEscalationLevel(ArtemisPayload):
    user_uuid: UUID
    pillar_id: str
    update_timestamp: datetime
    level: int


class ArtemisNewPillar(ArtemisPayload):
    user_uuid: UUID
    pillars: str


class ArtemisMissionSelected(ArtemisPayload):
    user_uuid: UUID
    mission: str = Field(validation_alias=AliasChoices("mission", "mission_id"))
    created_at: datetime
    process_id: int
    mission_process_id: int


class ArtemisTaskWindow(ArtemisPayload):
    startDate: str
    endDate: str

    @model_validator(mode="after")
    def check_order(self):
        """Keep the source timestamps while checking interval order."""
        start = datetime.fromisoformat(self.startDate)
        end = datetime.fromisoformat(self.endDate)
        if (start.tzinfo is None) != (end.tzinfo is None) or end <= start:
            raise ValueError("Scheduled task interval is invalid")
        return self


class ArtemisTaskScheduledProperties(ArtemisPayload):
    mission_id: str
    total_steps: int
    scheduled_for: List[ArtemisTaskWindow]

    @model_validator(mode="after")
    def check_step_count(self):
        """A snapshot can contain a subset of the mission's total steps."""
        if self.total_steps < 1 or not 0 < len(self.scheduled_for) <= self.total_steps:
            raise ValueError("Scheduled task step count is inconsistent")
        return self


class ArtemisTaskStepScheduled(ArtemisPayload):
    user_uuid: UUID
    created_at: datetime
    process_id: int
    mission_process_id: int
    properties: ArtemisTaskScheduledProperties


class ArtemisTaskDoneProperties(ArtemisPayload):
    mission: str = Field(validation_alias=AliasChoices("mission", "mission_id"))
    total_steps: int
    finished_at: datetime


class ArtemisTaskStepDone(ArtemisPayload):
    user_uuid: UUID
    created_at: datetime
    process_id: int
    mission_process_id: int
    properties: ArtemisTaskDoneProperties


class ArtemisMissionAccomplished(ArtemisPayload):
    end_reason: str | None = None
    user_uuid: UUID
    mission_id: str
    created_at: datetime
    process_id: int
    mission_process_id: int


class ArtemisNotificationProperties(ArtemisPayload):
    content_id: str
    mission_id: str
    content_type: str

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, value):
        """Store supported content types in lowercase."""
        return value.lower() if isinstance(value, str) else value


class ArtemisNotification(ArtemisPayload):
    user_uuid: UUID
    created_at: datetime
    process_id: int
    mission_process_id: int
    properties: ArtemisNotificationProperties


ARTEMIS_MESSAGES = {
    "new_user": ArtemisNewUser,
    "disabled_user": ArtemisDisabledUser,
    "health_habit_assessments": ArtemisHealthHabitAssessment,
    "psychological_assessments": ArtemisPsychologicalAssessment,
    "escalation_level": ArtemisEscalationLevel,
    "new_pillar": ArtemisNewPillar,
    "mission_selected": ArtemisMissionSelected,
    "task_step_scheduled": ArtemisTaskStepScheduled,
    "task_step_done": ArtemisTaskStepDone,
    "mission_accomplished": ArtemisMissionAccomplished,
    "mission_changed": ArtemisMissionAccomplished,
    "mission_not_achieved": ArtemisMissionAccomplished,
    "notification_rated": ArtemisNotification,
    "notification_saved": ArtemisNotification,
    "notification_unsaved": ArtemisNotification,
    "resource_link_opened": ArtemisNotification,
    "notification_sent": ArtemisNotification,
    "notification_opened": ArtemisNotification,
    "notification_plan": ArtemisNotificationPlan,
}


def parse_artemis_message(body: Dict[str, Any]) -> tuple[str, ArtemisPayload]:
    """Validate one Artemis message and return its type and payload."""
    if not isinstance(body, dict) or len(body) != 1:
        raise ValueError("Artemis message must contain exactly one event")
    event_name, value = next(iter(body.items()))
    schema = ARTEMIS_MESSAGES.get(event_name)
    if schema is None:
        raise ValueError(f"Unknown Artemis event: {event_name}")
    return event_name, schema.model_validate(value)
