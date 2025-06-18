from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel


class SelectedContentCreate(BaseModel):
    contents: List[str]
    mission_start_time: Optional[datetime]
    mission_end_time: Optional[datetime]


class SelectedContentList(BaseModel):
    selected_contents: Dict[str, SelectedContentCreate]
