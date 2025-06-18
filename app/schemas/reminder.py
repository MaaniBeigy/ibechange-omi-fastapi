from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel


class ReminderItem(BaseModel):
    Id: str
    Type: str
    Sent_date: str


class ReminderCreate(BaseModel):
    reminders: List[ReminderItem]
    Created_at: Optional[datetime]


class ReminderList(BaseModel):
    reminders: Dict[str, ReminderCreate]
