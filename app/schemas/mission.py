from typing import List

from pydantic import BaseModel, model_validator


class MissionCreate(BaseModel):
    mission_id: str
    weekly_frequency: int


class MissionList(BaseModel):
    missions: List[MissionCreate]

    @model_validator(mode="before")
    @classmethod
    def from_vu(cls, data):
        return {"missions": data} if isinstance(data, list) else data
