from typing import Any, List, Optional

from pydantic import BaseModel


class IndicatorVar(BaseModel):
    name: str
    type: Optional[str] = None
    unit: Optional[str] = None


class SingleIndicator(BaseModel):
    vars: List[IndicatorVar]
    rows: List[List[Any]]


class MultipleIndicators(BaseModel):
    vars: List[IndicatorVar]
    rows: List[List[Any]]


class EmotionalWellbeingData(BaseModel):
    singleindicator: Optional[SingleIndicator] = None
    multipleindicators: Optional[MultipleIndicators] = None


class EmotionalWellbeingRecord(BaseModel):
    user_uuid: str
    pillar_id: str
    start_date: str
    end_date: str
    data: EmotionalWellbeingData
