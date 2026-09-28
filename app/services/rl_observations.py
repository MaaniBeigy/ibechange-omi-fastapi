import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.models.event import Event
from app.utils.helpers import _aware_utc, _to_naive_utc


@dataclass(frozen=True)
class ObservedHistory:
    """Summarize deliveries and completed response windows known now."""

    since_last: float
    engagement_ema: float
    recent_opens: list[bool]
    recent_delays: list[float]
    sent_day: int
    sent_week: int
    sent_recommendation_class: int
    sent_reminders: int


def _identity(event: Event) -> tuple:
    """Identify a notification across sent and opened events."""
    properties = event.characteristics or {}
    return (
        event.process_id,
        properties.get("mission_process_id"),
        properties.get("content_id"),
        properties.get("mission_id"),
        properties.get("content_type"),
    )


def observed_history(
    db: Session, user_id, now: datetime, metadata: dict, zone: ZoneInfo
) -> ObservedHistory:
    """Read only deliveries with response windows closed by the decision time."""
    now_utc = _aware_utc(now)
    rows = db.exec(
        select(Event)
        .where(
            Event.user_id == user_id,
            Event.event_name.in_(("notification_sent", "notification_opened")),
            Event.event_start_time <= _to_naive_utc(now_utc),
            Event.is_deleted == False,
            Event.is_virtual == False,
        )
        .order_by(Event.event_start_time)
    ).all()
    sends = [row for row in rows if row.event_name == "notification_sent"]
    opens = [row for row in rows if row.event_name == "notification_opened"]
    response_window = float(metadata["response_window_min"])
    history_window = int(metadata["history_window"])
    decay = 2 / (history_window + 1)
    ema = 0.0
    recent_opens: list[bool] = []
    recent_delays: list[float] = []
    unused = set(range(len(opens)))
    for sent in sends:
        sent_time = _aware_utc(sent.event_start_time)
        if sent_time + timedelta(minutes=response_window) > now_utc:
            continue
        matched = None
        for index in sorted(unused):
            opened = opens[index]
            opened_time = _aware_utc(opened.event_start_time)
            if _identity(opened) == _identity(
                sent
            ) and sent_time <= opened_time < sent_time + timedelta(
                minutes=response_window
            ):
                matched = index
                break
        did_open = matched is not None
        delay = response_window
        engagement = 0.0
        if did_open:
            unused.remove(matched)
            delay = (
                _aware_utc(opens[matched].event_start_time) - sent_time
            ).total_seconds() / 60
            engagement = (1 - float(metadata["rating_weight"])) * math.exp(
                -float(metadata["delay_decay"]) * delay
            )
        ema += decay * (engagement - ema)
        recent_opens.append(did_open)
        recent_delays.append(delay)
        recent_opens = recent_opens[-history_window:]
        recent_delays = recent_delays[-history_window:]
    local_now = now_utc.astimezone(zone)
    day = local_now.date()
    week_start = day - timedelta(days=day.weekday())
    sent_day = sum(
        _aware_utc(row.event_start_time).astimezone(zone).date() == day for row in sends
    )
    today_sends = [
        row
        for row in sends
        if _aware_utc(row.event_start_time).astimezone(zone).date() == day
    ]
    sent_recommendation_class = sum(
        (row.characteristics or {}).get("content_type")
        in {"recommendation", "resource"}
        for row in today_sends
    )
    sent_reminders = sum(
        (row.characteristics or {}).get("content_type") == "reminder"
        for row in today_sends
    )
    sent_week = sum(
        week_start <= _aware_utc(row.event_start_time).astimezone(zone).date() <= day
        for row in sends
    )
    since_last = (
        max((now_utc - _aware_utc(sends[-1].event_start_time)).total_seconds() / 60, 0)
        if sends
        else 10080.0
    )
    return ObservedHistory(
        since_last,
        ema,
        recent_opens,
        recent_delays,
        sent_day,
        sent_week,
        sent_recommendation_class,
        sent_reminders,
    )
