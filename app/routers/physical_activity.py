import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.event import Event
from app.models.user import User
from app.schemas.physical_activity import IndicatorVar, PhysicalActivityRecord
from app.utils.helpers import (
    _ensure_user,
    _insert_event,
    _iso_utc,
    _multiple_row_times,
    _parse_iso_z,
    _parse_uuid_or_none,
)

INTENSITY_TO_EVENT = {
    "low": "low_intensity_physical_activity",
    "medium": "medium_intensity_physical_activity",
    "high": "high_intensity_physical_activity",
}


def _combine_vars_row(vars_: List[IndicatorVar], row: List[Any]) -> Dict[str, Any]:
    return {vars_[i].name: row[i] for i in range(min(len(vars_), len(row)))}


router = APIRouter(prefix="/physical_activity", tags=["physical_activity"])


@router.post("/", status_code=200)
def ingest_physical_activity(
    payload: List[PhysicalActivityRecord], db: Session = Depends(get_db)
):
    """
    Ingests 'physical_activity' pillar records and inserts into Event, idempotent-ly.

    Rules:
    - If user_uuid is not a valid UUID, skip that record.
    - Create user if not existing.
    - singleindicator: take only FIRST row (if any), map `vars` to that row.
      Event name: 'physical_activity_summary', times from top-level start_date / end_date.
    - multipleindicator: for each row, map `vars` to row dict. Event name is derived from 'type activity':
        'low'|'medium'|'high' will be put into '{intensity}_intensity_physical_activity'
      Times: derive using 'id' (YYYY-MM-DD_*) + 'start time'/'end time'.
    - Idempotent on (user_id, name, start_time, end_time, characteristics).
    """
    inserted: List[Dict[str, Any]] = []

    for rec in payload:
        if rec.pillar_id != "physical_activity":
            continue

        user_uuid = _parse_uuid_or_none(rec.user_uuid)
        if not user_uuid:  # Skip invalid user IDs
            continue

        _ensure_user(db, user_uuid)  # make sure user exists

        # ------------------------------ singleindicator ------------------------------
        if rec.data.singleindicator and rec.data.singleindicator.rows:
            si = rec.data.singleindicator
            first_row = si.rows[0]  # only the first row
            characteristics = _combine_vars_row(si.vars, first_row)

            start_dt = _parse_iso_z(rec.start_date)
            end_dt = _parse_iso_z(rec.end_date)
            ev = _insert_event(
                db,
                user_uuid,
                name="physical_activity_summary",
                start=start_dt,
                end=end_dt,
                characteristics=characteristics,
                process_id=None,
            )
            if ev:
                inserted.append(
                    {
                        "user_id": str(user_uuid),
                        "event_name": ev.event_name,
                        "start_time": _iso_utc(ev.event_start_time),
                        "end_time": _iso_utc(ev.event_end_time),
                    }
                )

        # ----------------------------- multipleindicator -----------------------------
        if rec.data.multipleindicator and rec.data.multipleindicator.rows:
            mi = rec.data.multipleindicator
            for row in mi.rows:
                row_dict = _combine_vars_row(mi.vars, row)

                # event name from 'type activity'
                intensity = str(row_dict.get("type activity", "")).strip().lower()
                event_name = INTENSITY_TO_EVENT.get(intensity, "physical_activity")

                # event times from 'id' date + 'start time'/'end time'
                start_dt, end_dt = _multiple_row_times(row_dict)

                characteristics = {
                    "average MET": row_dict.get("average MET"),
                    "duration": row_dict.get("duration"),
                    "mean heart rate": row_dict.get("mean heart rate"),
                }

                ev = _insert_event(
                    db,
                    user_uuid,
                    name=event_name,
                    start=start_dt,
                    end=end_dt,
                    characteristics=characteristics,
                    process_id=None,
                )
                if ev:
                    inserted.append(
                        {
                            "user_id": str(user_uuid),
                            "event_name": ev.event_name,
                            "start_time": _iso_utc(ev.event_start_time),
                            "end_time": _iso_utc(ev.event_end_time),
                        }
                    )

    db.commit()
    return {"inserted": inserted}
