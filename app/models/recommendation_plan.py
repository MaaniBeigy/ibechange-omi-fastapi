import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class RecommendationPlan(SQLModel, table=True):
    __tablename__ = "RecommendationPlan"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    content_id: str
    mission_id: Optional[str] = Field(default=None)
    scheduled_for: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
