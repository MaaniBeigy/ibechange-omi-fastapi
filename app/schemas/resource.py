from typing import List

from pydantic import BaseModel


class ResourceCreate(BaseModel):
    res_id: str
    mission: List[str]
    objective: List[str]
    hapa: List[str]
    comb: List[str]
    intervention_type: List[str]


class ResourceList(BaseModel):
    resources: List[ResourceCreate]
