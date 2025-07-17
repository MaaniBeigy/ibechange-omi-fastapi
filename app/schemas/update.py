from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel


class Event(BaseModel):
    process_id: int
    timestamp: Optional[datetime]
    event_name: str
    properties: Dict


class UserFeedback(BaseModel):
    events: List[Event]


class NewUser(BaseModel):
    user_id: UUID
    characteristics: Dict[str, Any]


class HealthHabitAssessment(BaseModel):
    hhs: Dict
    assessment_timestamp: Optional[datetime]


class NewMission(BaseModel):
    mission: str
    recommendations: List[str]
    resources: List[str]
    prescribed: bool
    selection_timestamp: datetime
    finish_timestamp: datetime

class MissionContent(BaseModel):
    update_timestamp: datetime
    new_missions: List[NewMission]


class EscalationLevelCreate(BaseModel):
    update_timestamp: datetime
    level: int
    pillar_id: str


class EscalationLevelResponse(BaseModel):
    id: UUID
    user_id: UUID
    update_timestamp: datetime
    level: int
    pillar_id: str
    created_at: datetime


class UpdateCreate(BaseModel):
    user_feedback: Dict[str, UserFeedback]
    new_users: Dict[str, NewUser]
    disabled_users: Dict[str, Dict[str, datetime]]
    health_habit_assessments: Dict[str, List[HealthHabitAssessment]]
    new_missions_and_contents: Dict[str, MissionContent]
    escalation_level: Optional[Dict[str, List[EscalationLevelCreate]]] = None
