import uuid
from datetime import datetime
from typing import List, Optional

from sqlmodel import ARRAY, Field, SQLModel, String


class Recommendation(SQLModel, table=True):
    __tablename__ = "Recommendation"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    rec_id: str = Field(unique=True, index=True)
    mission: List[str] = Field(sa_type=ARRAY(String))
    objective: List[str] = Field(sa_type=ARRAY(String))
    hapa: List[str] = Field(sa_type=ARRAY(String))
    comb: List[str] = Field(sa_type=ARRAY(String))
    intervention_type: List[str] = Field(sa_type=ARRAY(String))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
