from typing import Any, List, Optional

from pydantic import BaseModel


class IndicatorVar(BaseModel):
    name: str
    type: Optional[str] = None
    unit: Optional[str] = None


class SingleIndicator(BaseModel):
    vars: List[IndicatorVar]
    rows: List[List[Any]]


class MultipleIndicator(BaseModel):
    vars: List[IndicatorVar]
    rows: List[List[Any]]


class PhysicalActivityData(BaseModel):
    singleindicator: Optional[SingleIndicator] = None
    multipleindicator: Optional[MultipleIndicator] = None


class PhysicalActivityRecord(BaseModel):
    user_uuid: str
    pillar_id: str
    start_date: str
    end_date: str
    data: PhysicalActivityData
