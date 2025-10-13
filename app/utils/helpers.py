import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from app.models.event import Event
from app.models.user import User

NA_SENTINELS = {"N/A", "NA", "NaN", "", None}


def _coerce_by_type(value, typ: str):
    if value in NA_SENTINELS:
        return None
    if typ is None:
        return value
    t = typ.lower()
    try:
        if t in ("int", "integer"):
            return int(value)
        if t in ("float", "double", "number"):
            return float(value)
        # leave strings, booleans, etc. as-is
        return value
    except (ValueError, TypeError):
        # if it can't be coerced, store as NULL to avoid bad casts downstream
        return None


def _combine_vars_row(vars_, row):
    out = {}
    for i in range(min(len(vars_), len(row))):
        name = vars_[i].name
        typ = getattr(vars_[i], "type", None)
        out[name] = _coerce_by_type(row[i], typ)
    return out


def _to_naive_utc(dt: datetime) -> datetime:
    """Return a UTC-naive datetime (tzinfo=None) for stable storage/comparison."""
    if dt.tzinfo is None:
        # treat naive input as UTC already
        return dt.replace(tzinfo=None)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_uuid_or_none(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(value)
    except Exception:
        return None


def _ensure_user(db: Session, user_id: uuid.UUID) -> None:
    user = db.get(User, user_id)
    if user:
        return user
    user = db.exec(select(User).where(User.id == user_id)).first()
    if user:
        return user

    user = User(id=user_id, created_at=datetime.now(timezone.utc), is_deleted=False)
    db.add(user)
    db.flush()
    return user


def _parse_iso_z(dt_str: str) -> datetime:
    """
    Accepts strings that may end with 'Z' or offset; returns timezone-aware UTC datetime.
    """
    # Normalize trailing Z → +00:00 for fromisoformat
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    # if naive, treat it as UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _multiple_row_times(row_dict: Dict[str, Any]) -> Tuple[datetime, datetime]:
    """
    Build start/end UTC datetimes from row fields:
    - date is taken from the 'id' field prefix (YYYY-MM-DD_...)
    - 'start time' and 'end time' are HH:MM:SS
    """
    rid = str(row_dict.get("id", ""))
    date_part = rid.split("_")[0]  # keeps '2025-08-03' from '2025-08-03_activity_1'
    start_t = str(row_dict.get("start time", "00:00:00"))
    end_t = str(row_dict.get("end time", "00:00:00"))

    start_iso = f"{date_part}T{start_t}.000000Z"
    end_iso = f"{date_part}T{end_t}.000000Z"
    return _parse_iso_z(start_iso), _parse_iso_z(end_iso)


def _canon_characteristics(obj: Dict[str, Any]) -> str:
    """
    Canonical JSON for characteristics:
    - sorted keys
    - no whitespace differences (compact separators)
    - stable unicode handling
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _event_exists(
    db: Session,
    user_id: uuid.UUID,
    name: str,
    start: datetime,
    end: Optional[datetime],
    characteristics: Dict[str, Any],
) -> bool:
    start_naive = _to_naive_utc(start)
    end_naive = _to_naive_utc(end) if end is not None else None
    with db.no_autoflush:
        q = (
            select(Event)
            .where(Event.user_id == user_id)
            .where(Event.event_name == name)
            .where(Event.event_start_time == start_naive)
            .where(Event.event_end_time == end_naive)
        )
        rows = db.exec(q).all()
    canon_new = _canon_characteristics(characteristics)

    for ev in rows:
        existing_dict = (
            ev.characteristics if isinstance(ev.characteristics, dict) else {}
        )
        if _canon_characteristics(existing_dict) == canon_new:
            return True
    return False


def _insert_event(
    db: Session,
    user_id: uuid.UUID,
    name: str,
    start: datetime,
    end: datetime,
    characteristics: Dict[str, Any],
    process_id: Optional[uuid.UUID] = None,
) -> Optional[Event]:
    start_naive = _to_naive_utc(start)
    end_naive = _to_naive_utc(end) if end is not None else None
    if _event_exists(db, user_id, name, start, end, characteristics):
        return None

    ev = Event(
        user_id=user_id,
        event_name=name,
        event_start_time=start_naive,
        event_end_time=end_naive,
        characteristics=characteristics,
        process_id=process_id,
    )
    db.add(ev)
    db.flush()
    return ev
