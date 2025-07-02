import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import ARRAY, Field, SQLModel, String


class Resource(SQLModel, table=True):
    __tablename__ = "Resource"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    res_id: str = Field(unique=True, index=True)
    mission: List[str] = Field(sa_type=ARRAY(String))
    objective: List[str] = Field(sa_type=ARRAY(String))
    hapa: List[str] = Field(sa_type=ARRAY(String))
    comb: List[str] = Field(sa_type=ARRAY(String))
    intervention_type: List[str] = Field(sa_type=ARRAY(String))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
