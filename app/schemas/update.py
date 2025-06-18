from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel


class Event(BaseModel):
    process_id: int
    timestamp: Optional[datetime]
    event_name: str
    properties: Dict


class UserFeedback(BaseModel):
    events: List[Event]


class NewUser(BaseModel):
    gender: Optional[str]
    height: Optional[int]
    userAge: Optional[int]
    weight: Optional[int]
    wearable: Optional[str]
    residence: Optional[str]
    enrolmentDate: Optional[str]
    informedConsent: Optional[str]
    recruitmentCenter: Optional[str]
    level: Optional[int]


class HealthHabitAssessment(BaseModel):
    hhs: Dict
    assessment_timestamp: Optional[datetime]


class NewMission(BaseModel):
    mission: str
    recommendations: List[str]
    resources: List[str]


class NewMissionContent(BaseModel):
    selection_timestamp: Optional[datetime]
    new_missions: List[NewMission]


class UpdateCreate(BaseModel):
    user_feedback: Dict[str, UserFeedback]
    new_users: Dict[str, NewUser]
    disabled_users: List[str]
    health_habit_assessments: Dict[str, HealthHabitAssessment]
    new_missions_and_contents: Dict[str, NewMissionContent]
