import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.artemis import (
    ArtemisFailure,
    ArtemisInbox,
    MissionSelection,
    PillarSelection,
    PsychologicalAssessment,
)
from app.models.escalation_level import EscalationLevel
from app.models.event import Event
from app.models.mission import Mission
from app.models.policy_decision import PolicyDecision
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.user import HealthHabitAssessment, MissionContent, User
from app.schemas.update import parse_artemis_message
from app.services.mission_schedule_service import record_scheduled_snapshot
from app.utils.helpers import _aware_utc, _to_naive_utc


def _user(db: Session, user_id) -> User:
    """Get or create the user named by an update."""
    user = db.get(User, user_id)
    if user is None:
        user = User(id=user_id)
        db.add(user)
        db.flush()
    return user


def _mission(db: Session, mission_code: str) -> Mission:
    """Get or create a mission by its external code."""
    mission = db.exec(select(Mission).where(Mission.mission_id == mission_code)).first()
    if mission is None:
        mission = Mission(mission_id=mission_code)
        db.add(mission)
        db.flush()
    return mission


def _content(
    db: Session, content_id: str, content_type: str, mission_code: str
) -> None:
    """Keep content metadata found in a notification event."""
    content = db.exec(
        select(Recommendation).where(Recommendation.content_id == content_id)
    ).first()
    if content is None:
        db.add(
            Recommendation(
                content_id=content_id,
                content_type=content_type,
                missions=[mission_code],
                objective=[],
                hapa=[],
                comb=[],
                intervention_type=[],
            )
        )
    elif mission_code not in content.missions:
        content.missions = [*content.missions, mission_code]
        db.add(content)


def _link_sent_decision(db: Session, event: Event) -> None:
    """Attach a delivery confirmation to its recorded send decision."""
    properties = event.characteristics or {}
    plans = db.exec(
        select(RecommendationPlan, Recommendation)
        .join(Recommendation, Recommendation.id == RecommendationPlan.recommendation_id)
        .where(
            RecommendationPlan.user_id == event.user_id,
            RecommendationPlan.scheduled_for <= _aware_utc(event.event_start_time),
            RecommendationPlan.scheduled_for
            >= _aware_utc(event.event_start_time) - timedelta(minutes=60),
            RecommendationPlan.is_deleted == False,
        )
        .order_by(RecommendationPlan.scheduled_for.desc())
    ).all()
    for plan, recommendation in plans:
        mission = db.get(Mission, plan.mission_id) if plan.mission_id else None
        if (
            recommendation.content_id != properties.get("content_id")
            or (plan.content_type or recommendation.content_type)
            != properties.get("content_type")
            or (
                mission is not None
                and mission.mission_id != properties.get("mission_id")
            )
            or (
                plan.mission_process_id is not None
                and plan.mission_process_id != properties.get("mission_process_id")
            )
        ):
            continue
        decision = db.exec(
            select(PolicyDecision).where(
                PolicyDecision.user_id == event.user_id,
                PolicyDecision.plan_id == plan.plan_id,
                PolicyDecision.decided_at == _aware_utc(plan.scheduled_for),
                PolicyDecision.occurrence_index == plan.occurrence_index,
                PolicyDecision.action == 1,
            )
        ).first()
        if decision and decision.outcome_event_id is None:
            decision.outcome_event_id = event.id
            db.add(decision)
            return


def _record_event(
    db: Session,
    user: User,
    event_name: str,
    event_time: datetime,
    process_id: int,
    properties: dict,
) -> Event:
    """Keep one source event even when Artemis gives a new broker ID."""
    occurred_at = _to_naive_utc(event_time)
    existing = db.exec(
        select(Event).where(
            Event.user_id == user.id,
            Event.event_name == event_name,
            Event.process_id == process_id,
            Event.event_start_time == occurred_at,
        )
    ).all()
    matching = next(
        (row for row in existing if row.characteristics == properties), None
    )
    if matching is None:
        matching = Event(
            user_id=user.id,
            event_name=event_name,
            event_start_time=occurred_at,
            process_id=process_id,
            characteristics=properties,
        )
        db.add(matching)
        db.flush()
    return matching


def _apply(db: Session, event_name: str, payload, message_id: str, raw: dict) -> None:
    """Apply one validated Artemis event to the current transaction."""
    
    if event_name == "notification_plan":
        # Handle incoming notification plans from Artemis
        _handle_notification_plan(db, payload, message_id)
        return

    if event_name == "new_user":
        user = _user(db, payload.uuid)
        user.characteristics = payload.model_dump(mode="json", exclude={"uuid"})
        if not payload.enabled:
            user.deleteUser(datetime.now(timezone.utc))
        db.add(user)
        return

    user = _user(db, payload.user_uuid) if payload.user_uuid is not None else None
    if event_name == "disabled_user":
        user.deleteUser(_aware_utc(payload.date_disabled))
        db.add(user)
    elif event_name == "health_habit_assessments":
        assessment_time = _aware_utc(payload.assessment_timestamp)
        existing = db.exec(
            select(HealthHabitAssessment).where(
                HealthHabitAssessment.user_id == user.id,
                HealthHabitAssessment.assessment_timestamp == assessment_time,
            )
        ).all()
        content = payload.model_dump(mode="json", exclude={"user_uuid"})
        if not any(row.hhs.get("pillar_id") == payload.pillar_id for row in existing):
            db.add(
                HealthHabitAssessment(
                    user_id=user.id,
                    hhs=content,
                    assessment_timestamp=assessment_time,
                )
            )
    elif event_name == "psychological_assessments":
        assessment_time = _aware_utc(payload.assessment_timestamp)
        subpillar = payload.subpillar or payload.suppillar
        existing = db.exec(
            select(PsychologicalAssessment).where(
                PsychologicalAssessment.user_id == (user.id if user else None),
                PsychologicalAssessment.pillar_id == payload.pillar_id,
                PsychologicalAssessment.subpillar == subpillar,
                PsychologicalAssessment.assessment_timestamp == assessment_time,
            )
        ).all()
        if not any(row.raw_payload == raw for row in existing):
            db.add(
                PsychologicalAssessment(
                    user_id=user.id if user else None,
                    pillar_id=payload.pillar_id,
                    subpillar=subpillar,
                    barriers=payload.barriers,
                    answer_tasklog=payload.answer_tasklog,
                    assessment_timestamp=assessment_time,
                    raw_payload=raw,
                    source_message_id=message_id,
                )
            )
    elif event_name == "escalation_level":
        update_time = _aware_utc(payload.update_timestamp)
        existing = db.exec(
            select(EscalationLevel).where(
                EscalationLevel.user_id == user.id,
                EscalationLevel.pillar_id == payload.pillar_id,
                EscalationLevel.level == payload.level,
                EscalationLevel.update_timestamp == update_time,
            )
        ).first()
        if existing is None:
            db.add(
                EscalationLevel(
                    user_id=user.id,
                    pillar_id=payload.pillar_id,
                    level=payload.level,
                    update_timestamp=update_time,
                )
            )
    elif event_name == "new_pillar":
        db.add(PillarSelection(user_id=user.id, pillar_id=payload.pillars))
    elif event_name == "mission_selected":
        mission = _mission(db, payload.mission)
        selected_at = _aware_utc(payload.created_at)
        selection = db.exec(
            select(MissionSelection).where(
                MissionSelection.user_id == user.id,
                MissionSelection.mission_process_id == payload.mission_process_id,
            )
        ).first()
        if selection is None:
            db.add(
                MissionSelection(
                    user_id=user.id,
                    mission_id=mission.id,
                    process_id=payload.process_id,
                    mission_process_id=payload.mission_process_id,
                    selected_at=selected_at,
                )
            )
        elif (
            selection.mission_id != mission.id
            or selection.process_id != payload.process_id
        ):
            raise ValueError("Mission process ID was reused for another mission")
        current = db.exec(
            select(MissionContent).where(
                MissionContent.user_id == user.id,
                MissionContent.mission_id == mission.id,
            )
        ).first()
        if current is None:
            db.add(
                MissionContent(
                    user_id=user.id,
                    mission_id=mission.id,
                    selection_timestamp=selected_at,
                    mission_start_time=selected_at,
                )
            )
        elif (
            current.selection_timestamp is None
            or _aware_utc(current.selection_timestamp) < selected_at
        ):
            current.selection_timestamp = selected_at
            current.mission_start_time = selected_at
            current.mission_end_time = None
            current.modified_at = datetime.now(timezone.utc)
            db.add(current)
    elif event_name == "task_step_scheduled":
        mission_code = payload.properties.mission_id
        _mission(db, mission_code)
        properties = payload.properties.model_dump(mode="json")
        properties["mission_process_id"] = payload.mission_process_id
        previous = db.exec(
            select(Event).where(
                Event.user_id == user.id,
                Event.event_name == event_name,
                Event.process_id == payload.process_id,
            )
        ).all()
        previous = [
            row
            for row in previous
            if (row.characteristics or {}).get("mission_process_id")
            == payload.mission_process_id
        ]
        latest = max(previous, key=lambda row: row.event_start_time, default=None)
        source_time = _to_naive_utc(payload.created_at)
        if (
            latest is not None
            and latest.event_start_time == source_time
            and latest.characteristics != properties
        ):
            raise ValueError("Conflicting mission schedules have the same source time")
        if latest is None or latest.event_start_time <= source_time:
            record_scheduled_snapshot(
                db,
                user.id,
                payload.mission_process_id,
                mission_code,
                [
                    (window.startDate, window.endDate)
                    for window in payload.properties.scheduled_for
                ],
                message_id,
            )
        _record_event(
            db, user, event_name, payload.created_at, payload.process_id, properties
        )
    elif event_name == "task_step_done":
        _mission(db, payload.properties.mission)
        properties = payload.properties.model_dump(mode="json")
        properties["mission_process_id"] = payload.mission_process_id
        properties["created_at"] = payload.created_at.isoformat()
        _record_event(
            db,
            user,
            event_name,
            payload.properties.finished_at,
            payload.process_id,
            properties,
        )
    elif event_name == "mission_accomplished":
        mission = _mission(db, payload.mission_id)
        _record_event(
            db,
            user,
            event_name,
            payload.created_at,
            payload.process_id,
            {
                "mission_id": payload.mission_id,
                "mission_process_id": payload.mission_process_id,
            },
        )
        current = db.exec(
            select(MissionContent).where(
                MissionContent.user_id == user.id,
                MissionContent.mission_id == mission.id,
            )
        ).first()
        latest_selection = db.exec(
            select(MissionSelection)
            .where(
                MissionSelection.user_id == user.id,
                MissionSelection.mission_id == mission.id,
            )
            .order_by(MissionSelection.selected_at.desc())
        ).first()
        if (
            current is not None
            and latest_selection is not None
            and latest_selection.mission_process_id == payload.mission_process_id
        ):
            accomplished_at = _aware_utc(payload.created_at)
            if (
                current.mission_end_time is None
                or _aware_utc(current.mission_end_time) < accomplished_at
            ):
                current.mission_end_time = accomplished_at
                current.modified_at = datetime.now(timezone.utc)
                db.add(current)
    elif event_name in {"notification_sent", "notification_opened"}:
        props = payload.properties.model_dump(mode="json")
        _mission(db, props["mission_id"])
        _content(db, props["content_id"], props["content_type"], props["mission_id"])
        props["mission_process_id"] = payload.mission_process_id
        matching = _record_event(
            db, user, event_name, payload.created_at, payload.process_id, props
        )
        if event_name == "notification_sent":
            _link_sent_decision(db, matching)


def process_artemis_message(
    body: str | dict[str, Any], message_id: str, db: Session
) -> bool:
    """Commit one broker message and report whether it was new."""
    if not message_id:
        raise ValueError("Artemis message ID is required")
    document = json.loads(body) if isinstance(body, str) else body
    event_name, payload = parse_artemis_message(document)
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    existing = db.get(ArtemisInbox, message_id)
    if existing is not None:
        if existing.body_sha256 != digest:
            raise ValueError("Artemis message ID was reused with a different body")
        return False
    try:
        db.add(
            ArtemisInbox(
                message_id=message_id, body_sha256=digest, event_name=event_name
            )
        )
        _apply(db, event_name, payload, message_id, document[event_name])
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(ArtemisInbox, message_id)
        if existing is None or existing.body_sha256 != digest:
            raise
        return False
    except Exception:
        db.rollback()
        raise
    return True


def record_invalid_message(
    body: str, message_id: str, reason: str, db: Session
) -> None:
    """Record a rejected message before the broker acknowledges it."""
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    existing = db.get(ArtemisFailure, message_id)
    if existing is not None:
        if existing.body_sha256 != digest:
            raise ValueError("Artemis message ID was reused with a different body")
        return
    try:
        db.add(
            ArtemisFailure(
                message_id=message_id,
                body_sha256=digest,
                reason=reason[:1000],
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(ArtemisFailure, message_id)
        if existing is None or existing.body_sha256 != digest:
            raise
    except Exception:
        db.rollback()
        raise



def _handle_notification_plan(db: Session, payload, message_id: str) -> None:
    """Handle incoming notification plan message from Artemis."""
    from app.models.event import Event
    from app.utils.helpers import _aware_utc, _to_naive_utc
    
    user = _user(db, payload.user_id)
    
    for notification in payload.notifications:
        event = Event(
            user_id=user.id,
            event_name="notification_sent",
            event_start_time=_to_naive_utc(payload.start_time),
            process_id=0,
            characteristics={
                "content_id": notification.get("id", ""),
                "mission_id": notification.get("mission", ""),
                "content_type": notification.get("type", ""),
                "mission_process_id": notification.get("mission_process_id"),
                "source_message_id": message_id,
            },
        )
        db.add(event)
