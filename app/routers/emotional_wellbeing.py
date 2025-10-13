import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.dependencies import get_db
from app.schemas.emotional_wellbeing import EmotionalWellbeingRecord, IndicatorVar
from app.utils.helpers import (
    _canon_characteristics,
    _combine_vars_row,
    _ensure_user,
    _insert_event,
    _iso_utc,
    _parse_iso_z,
    _parse_uuid_or_none,
    _to_naive_utc,
)

router = APIRouter(prefix="/emotional_wellbeing", tags=["emotional_wellbeing"])


def _extract_sleep_event(row_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Build event payload from a mapped row:
      - name: from 'type_sleep' (string)
      - start/end: from 'bedtime_start'/'bedtime_end' (ISO with Z/offset)
      - characteristics: all other keys except those three
    Returns None if required fields are missing/empty.
    """
    event_name = str(row_dict.get("type_sleep") or "").strip()
    start_raw = row_dict.get("bedtime_start")
    end_raw = row_dict.get("bedtime_end")
    if not event_name or not start_raw or not end_raw:
        return None

    start_dt = _parse_iso_z(str(start_raw))
    end_dt = _parse_iso_z(str(end_raw))

    characteristics = {
        k: v
        for k, v in row_dict.items()
        if k not in ("type_sleep", "bedtime_start", "bedtime_end")
    }
    return {
        "event_name": event_name,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "characteristics": characteristics,
    }


@router.post("/", status_code=200)
def ingest_emotional_wellbeing(
    payload: List[EmotionalWellbeingRecord], db: Session = Depends(get_db)
):
    """
    Ingests 'emotional_wellbeing' pillar records and inserts into Event.

    Rules:
    - If user_uuid is not a valid UUID, skip that record.
    - Create user if not existing.
    - singleindicator: take only FIRST row (if any).
      Derive event_name from 'type_sleep', times from 'bedtime_start'/'bedtime_end'.
    - multipleindicators: for each row, derive same as above.
    - Idempotent on (user_id, event_name, start_time, end_time, characteristics).
    - Also de-duplicate.
    """
    inserted: List[Dict[str, Any]] = []

    for rec in payload:
        if rec.pillar_id != "emotional_wellbeing":
            continue

        user_uuid = _parse_uuid_or_none(rec.user_uuid)
        if not user_uuid:
            continue

        _ensure_user(db, user_uuid)

        # Keep a set of tuples to deduplicate within this single request payload for this user
        # Key uses naive UTC times + canonical characteristics
        seen_keys = set()

        # ------------------------------ singleindicator ------------------------------
        if rec.data.singleindicator and rec.data.singleindicator.rows:
            si = rec.data.singleindicator
            first_row = si.rows[0]
            row_dict = _combine_vars_row(si.vars, first_row)
            evt = _extract_sleep_event(row_dict)
            if evt:
                start_naive = _to_naive_utc(evt["start_dt"])
                end_naive = _to_naive_utc(evt["end_dt"])
                canon = _canon_characteristics(evt["characteristics"])
                local_key = (
                    user_uuid,
                    evt["event_name"],
                    start_naive,
                    end_naive,
                    canon,
                )
                if local_key not in seen_keys:
                    seen_keys.add(local_key)
                    ev = _insert_event(
                        db,
                        user_uuid,
                        name=evt["event_name"],
                        start=evt["start_dt"],
                        end=evt["end_dt"],
                        characteristics=evt["characteristics"],
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

        # ----------------------------- multipleindicators ----------------------------
        if rec.data.multipleindicators and rec.data.multipleindicators.rows:
            mi = rec.data.multipleindicators
            for row in mi.rows:
                row_dict = _combine_vars_row(mi.vars, row)
                evt = _extract_sleep_event(row_dict)
                if not evt:
                    continue
                start_naive = _to_naive_utc(evt["start_dt"])
                end_naive = _to_naive_utc(evt["end_dt"])
                canon = _canon_characteristics(evt["characteristics"])
                local_key = (
                    user_uuid,
                    evt["event_name"],
                    start_naive,
                    end_naive,
                    canon,
                )
                if local_key in seen_keys:
                    continue
                seen_keys.add(local_key)

                ev = _insert_event(
                    db,
                    user_uuid,
                    name=evt["event_name"],
                    start=evt["start_dt"],
                    end=evt["end_dt"],
                    characteristics=evt["characteristics"],
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
