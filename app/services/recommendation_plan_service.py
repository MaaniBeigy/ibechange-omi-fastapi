"""Make one constrained policy decision for the current selected-content queue."""

import hashlib
import json
import os
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.models.artemis import MissionScheduledStep
from app.models.event import Event
from app.models.mission import Mission
from app.models.policy_decision import PolicyDecision
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.services.policy_learning_service import prepare_policy, record_next_state
from app.services.rl_observations import observed_history
from app.services.rl_policy import (
    MAX_RECOMMENDATION_CLASS_PER_DAY,
    MAX_REMINDERS_PER_DAY,
    MIN_RECOMMENDATION_CLASS_PER_DAY,
    MIN_REMINDERS_PER_DAY,
    InfeasibleQueueError,
    QueueItem,
    decide_epoch,
    load_policy_artifacts,
    policy_kind,
)
from app.utils.helpers import _aware_utc, _to_naive_utc

DeadlineResolver = Callable[[Session, SelectedContent, dict, datetime], datetime | None]


def _source_time(value: str) -> datetime | None:
    """Interpret an Artemis interval endpoint in the configured mission zone."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    zone = ZoneInfo(
        os.getenv(
            "ARTEMIS_SCHEDULE_TIMEZONE", os.getenv("POLICY_TIMEZONE", "Europe/Berlin")
        )
    )
    first = parsed.replace(tzinfo=zone, fold=0)
    second = parsed.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        return None
    result = first.astimezone(timezone.utc)
    if result.astimezone(zone).replace(tzinfo=None) != parsed:
        return None
    return result


def resolve_reminder_deadline(
    db: Session, selected: SelectedContent, content: dict, epoch: datetime
) -> datetime | None:
    """Use today's last Artemis step, or the selected mission's global end."""
    zone = ZoneInfo(os.getenv("POLICY_TIMEZONE", "Europe/Berlin"))
    day = _aware_utc(epoch).astimezone(zone).date()
    steps = db.exec(
        select(MissionScheduledStep).where(
            MissionScheduledStep.user_id == selected.user_id,
            MissionScheduledStep.mission_code == content["mission_id"],
            MissionScheduledStep.is_active == True,
        )
    ).all()
    deadlines = []
    for step in steps:
        if step.end_date_raw is not None:
            raw_end = datetime.fromisoformat(step.end_date_raw)
            deadline = _source_time(step.end_date_raw)
            if deadline is None:
                if raw_end.date() == day:
                    return None
                continue
        elif step.scheduled_for is not None:
            deadline = _aware_utc(step.scheduled_for)
        else:
            continue
        if deadline.astimezone(zone).date() == day:
            deadlines.append(deadline)
    if deadlines:
        return max(deadlines)
    if selected.mission_end_time is not None:
        return _aware_utc(selected.mission_end_time)
    if selected.mission_start_time is not None:
        return _aware_utc(selected.mission_start_time) + timedelta(days=7)
    return None


def _day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """Return UTC bounds for one local calendar day."""
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(
        timezone.utc
    )
    return start, end


def _occurrences(contents: list[dict], day: date) -> list[tuple[int, str, dict]]:
    """Give unchanged queue entries a stable identity across repeated updates."""
    repetitions: Counter[tuple] = Counter()
    result = []
    for index, content in enumerate(contents):
        if content.get("delivery_date") not in (None, day.isoformat()):
            continue
        identity = (
            content.get("id"),
            content.get("type"),
            content.get("mission_id"),
            content.get("mission_process_id"),
        )
        repetitions[identity] += 1
        encoded = json.dumps((identity, repetitions[identity]), separators=(",", ":"))
        key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        result.append((index, key, content))
    return result


def _waking_deadline(quiet_start: int, quiet_end: int) -> int:
    """Return the last local minute before overnight quiet hours."""
    return quiet_start * 60 - 1 if quiet_start > quiet_end else 1439


def _delivery_counts(
    db: Session,
    user_id,
    prior: list[tuple[RecommendationPlan, Recommendation]],
    week_begin: datetime,
    day_end: datetime,
    zone: ZoneInfo,
    epoch: datetime,
) -> tuple[Counter, Counter]:
    """Match planned sends to broker confirmations and count unmatched sends."""
    planned: Counter[tuple] = Counter()
    for plan, recommendation in prior:
        mission = db.get(Mission, plan.mission_id) if plan.mission_id else None
        planned[
            (
                _aware_utc(plan.scheduled_for).astimezone(zone).date(),
                recommendation.content_id,
                plan.content_type or recommendation.content_type,
                mission.mission_id if mission else None,
                plan.mission_process_id,
            )
        ] += 1
    rows = db.exec(
        select(Event).where(
            Event.user_id == user_id,
            Event.event_name == "notification_sent",
            Event.event_start_time >= _to_naive_utc(week_begin),
            Event.event_start_time < _to_naive_utc(day_end),
            Event.event_start_time <= _to_naive_utc(epoch),
            Event.created_at <= epoch,
            Event.is_deleted == False,
            Event.is_virtual == False,
        )
    ).all()
    confirmed: Counter[tuple] = Counter()
    for event in rows:
        properties = event.characteristics or {}
        confirmed[
            (
                _aware_utc(event.event_start_time).astimezone(zone).date(),
                properties.get("content_id"),
                properties.get("content_type"),
                properties.get("mission_id"),
                properties.get("mission_process_id"),
            )
        ] += 1
    return planned, confirmed


def generate_recommendation_plan(
    selected: SelectedContent,
    epoch: datetime,
    db: Session,
    deadline_resolver: DeadlineResolver | None = None,
) -> RecommendationPlan | None:
    """Persist one deterministic policy decision for the latest queue epoch."""
    if selected.is_deleted:
        return None
    deadline_resolver = deadline_resolver or resolve_reminder_deadline
    user = db.get(User, selected.user_id)
    if user is None or user.is_deleted:
        return None
    epoch = _aware_utc(epoch)
    observed_at = min(epoch, datetime.now(timezone.utc))
    if db.exec(
        select(PolicyDecision).where(
            PolicyDecision.user_id == selected.user_id,
            PolicyDecision.decided_at == epoch,
        )
    ).first():
        return None

    zone = ZoneInfo(os.getenv("POLICY_TIMEZONE", "Europe/Berlin"))
    local_epoch = epoch.astimezone(zone)
    day = local_epoch.date()
    _, day_end = _day_bounds(day, zone)
    week_start = day - timedelta(days=day.weekday())
    week_begin, _ = _day_bounds(week_start, zone)
    prior = db.exec(
        select(RecommendationPlan, Recommendation)
        .join(Recommendation, Recommendation.id == RecommendationPlan.recommendation_id)
        .where(
            RecommendationPlan.user_id == selected.user_id,
            RecommendationPlan.scheduled_for >= week_begin,
            RecommendationPlan.scheduled_for < day_end,
            RecommendationPlan.is_deleted == False,
        )
    ).all()
    planned_counts, confirmed_counts = _delivery_counts(
        db, user.id, prior, week_begin, day_end, zone, observed_at
    )
    combined_counts = planned_counts | confirmed_counts
    unmatched_confirmed = confirmed_counts - planned_counts
    sent_keys = {
        key
        for key in db.exec(
            select(RecommendationPlan.occurrence_key).where(
                RecommendationPlan.user_id == selected.user_id,
                RecommendationPlan.plan_id == selected.id,
                RecommendationPlan.is_deleted == False,
            )
        ).all()
        if key
    }
    occurrences = []
    for row in _occurrences(selected.contents or [], day):
        index, key, content = row
        if key in sent_keys:
            continue
        identity = (
            day,
            content["id"],
            content["type"],
            content["mission_id"],
            content.get("mission_process_id"),
        )
        if unmatched_confirmed[identity]:
            unmatched_confirmed[identity] -= 1
            continue
        occurrences.append(row)
    if not occurrences:
        return None

    attributes = (user.characteristics or {}).get("attributes", {})
    wearer = attributes.get("wearable") == "yes"
    policy = load_policy_artifacts()["wearer" if wearer else "nonwearer"]
    policy = prepare_policy(db, user.id, policy, observed_at)
    history = observed_history(db, user.id, observed_at, policy.metadata, zone)
    sent_recommendation_class = sum(
        count
        for identity, count in combined_counts.items()
        if identity[0] == day and identity[2] in {"recommendation", "resource"}
    )
    sent_reminders = sum(
        count
        for identity, count in combined_counts.items()
        if identity[0] == day and identity[2] == "reminder"
    )
    remaining_by_class = {
        "recommendation": max(
            0, MAX_RECOMMENDATION_CLASS_PER_DAY - sent_recommendation_class
        ),
        "reminder": max(0, MAX_REMINDERS_PER_DAY - sent_reminders),
    }
    today_occurrences = []
    for row in occurrences:
        kind = policy_kind(row[2]["type"])
        if remaining_by_class[kind] == 0:
            if kind == "reminder":
                deadline = deadline_resolver(db, selected, row[2], epoch)
                if deadline is not None and _aware_utc(deadline) <= day_end:
                    raise InfeasibleQueueError(
                        "Daily content class limit would be exceeded"
                    )
            break
        today_occurrences.append(row)
        remaining_by_class[kind] -= 1
    available_kinds = {policy_kind(row[2]["type"]) for row in occurrences}
    selected_counts = Counter(policy_kind(row[2]["type"]) for row in today_occurrences)
    if (
        "recommendation" in available_kinds
        and sent_recommendation_class + selected_counts["recommendation"]
        < MIN_RECOMMENDATION_CLASS_PER_DAY
    ) or (
        "reminder" in available_kinds
        and sent_reminders + selected_counts["reminder"] < MIN_REMINDERS_PER_DAY
    ):
        raise InfeasibleQueueError(
            "Ordered daily queue cannot meet the content class minimum"
        )
    if not today_occurrences:
        return None
    occurrences = today_occurrences

    minute = local_epoch.hour * 60 + local_epoch.minute
    quiet_start = int(policy.metadata["quiet_hour_start"])
    quiet_end = int(policy.metadata["quiet_hour_end"])
    waking_deadline = _waking_deadline(quiet_start, quiet_end)
    items = []
    deadlines = []
    for _, _, content in occurrences:
        if content["type"] == "reminder":
            deadline = deadline_resolver(db, selected, content, epoch)
            if deadline is None:
                raise InfeasibleQueueError(
                    "Reminder has no Artemis deadline or mission end time"
                )
            deadline = _aware_utc(deadline)
            local_deadline = deadline.astimezone(zone)
            if deadline < epoch:
                deadline_minute = -1
            elif local_deadline.date() == day:
                deadline_minute = local_deadline.hour * 60 + local_deadline.minute
            else:
                deadline_minute = waking_deadline
        else:
            deadline = None
            deadline_minute = waking_deadline
        items.append(QueueItem(content["type"], deadline_minute))
        deadlines.append(deadline)

    sent_day = sum(
        count for identity, count in combined_counts.items() if identity[0] == day
    )
    sent_week = sum(combined_counts.values())
    last_planned = max(
        (
            _aware_utc(plan.scheduled_for)
            for plan, _ in prior
            if _aware_utc(plan.scheduled_for) <= epoch
        ),
        default=None,
    )
    since_last = history.since_last
    if last_planned:
        since_last = min(since_last, (epoch - last_planned).total_seconds() / 60)
    mission_minute = max(
        (item.deadline_minute for item in items if item.kind == "reminder"),
        default=None,
    )
    decision = decide_epoch(
        policy,
        tuple(items),
        minute,
        local_epoch.weekday(),
        sent_day,
        sent_week,
        since_last,
        history.engagement_ema,
        history.recent_opens,
        history.recent_delays,
        mission_minute,
        {} if wearer else None,
        floor_day=(
            (
                MIN_RECOMMENDATION_CLASS_PER_DAY
                if "recommendation" in available_kinds
                else 0
            )
            + (MIN_REMINDERS_PER_DAY if "reminder" in available_kinds else 0)
        ),
        floor_week=0,
    )
    index, key, content = occurrences[0]
    policy_version = f"{decision.policy_version}-omi2x2-v1"
    recorded = PolicyDecision(
        user_id=user.id,
        plan_id=selected.id,
        occurrence_index=index,
        decided_at=epoch,
        action=decision.action,
        propensity=1.0,
        state=decision.state.tolist(),
        safe_mask=list(decision.safe_mask),
        policy_version=policy_version,
    )
    db.add(recorded)
    db.flush()
    record_next_state(db, recorded)
    if decision.action == 0:
        db.flush()
        return None
    recommendation = db.exec(
        select(Recommendation).where(Recommendation.content_id == content["id"])
    ).first()
    if recommendation is None or recommendation.is_deleted:
        raise ValueError("Selected content has no catalog record")
    mission = db.exec(
        select(Mission).where(Mission.mission_id == content["mission_id"])
    ).first()
    if mission is None or mission.is_deleted:
        raise ValueError("Selected content has no mission record")
    plan = RecommendationPlan(
        user_id=user.id,
        plan_id=selected.id,
        recommendation_id=recommendation.id,
        mission_id=mission.id,
        occurrence_index=index,
        occurrence_key=key,
        content_type=content["type"],
        mission_process_id=content.get("mission_process_id"),
        deadline=deadlines[0],
        policy_version=policy_version,
        scheduled_for=epoch,
    )
    db.add(plan)
    db.flush()
    return plan
