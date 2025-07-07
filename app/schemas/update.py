from datetime import datetime
from typing import Dict, List, Optional, Any
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


class MissionContent(BaseModel):
    selection_timestamp: Optional[datetime]
    new_missions: List[NewMission]


class UpdateCreate(BaseModel):
    user_feedback: Dict[str, UserFeedback]
    new_users: Dict[str, NewUser]
    disabled_users: List[str]
    health_habit_assessments: Dict[str, HealthHabitAssessment]
    new_missions_and_contents: Dict[str, MissionContent]
