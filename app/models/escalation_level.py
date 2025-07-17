import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class EscalationLevel(SQLModel, table=True):
    __tablename__ = "EscalationLevel"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(index=True)
    update_timestamp: datetime
    level: int
    pillar_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    modified_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
