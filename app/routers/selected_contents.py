import logging
import os
from datetime import datetime, timezone
from typing import Dict
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.mission import Mission
from app.models.policy_decision import PolicyDecision
from app.models.policy_learning import PolicyTransition
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.schemas.selected_content import SelectedContentCreate, SelectedContentList
from app.services.policy_learning_service import lock_user
from app.services.recommendation_plan_service import _day_bounds
from app.utils.helpers import _to_utc

router = APIRouter(prefix="/selected_contents", tags=["Selected Contents"])

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def _ensure_catalog(db, contents):
    # Shared catalog rows can arrive concurrently in different participants' plans.
    for code in sorted({item.mission_id for item in contents}):
        db.execute(
            insert(Mission)
            .values(mission_id=code)
            .on_conflict_do_nothing(index_elements=["mission_id"])
        )
    by_content = {}
    for item in contents:
        by_content.setdefault(item.id, []).append(item)
    for content_id, occurrences in sorted(by_content.items()):
        codes = list(dict.fromkeys(item.mission_id for item in occurrences))
        db.execute(
            insert(Recommendation)
            .values(
                content_id=content_id,
                content_type=occurrences[0].type,
                missions=codes,
                objective=[],
                hapa=[],
                comb=[],
                intervention_type=[],
            )
            .on_conflict_do_nothing(index_elements=["content_id"])
        )
        existing = db.exec(
            select(Recommendation)
            .where(Recommendation.content_id == content_id)
            .with_for_update()
        ).one()
        existing.missions = list(dict.fromkeys([*(existing.missions or []), *codes]))


def _replace_day(db, selected):
    """Retire unconfirmed sends and revoke decisions for grid instants still ahead."""
    now = datetime.now(timezone.utc)
    start, end = _day_bounds(
        selected.delivery_date, ZoneInfo(os.getenv("POLICY_TIMEZONE", "Europe/Berlin"))
    )
    decisions = db.exec(
        select(PolicyDecision).where(
            PolicyDecision.user_id == selected.user_id,
            PolicyDecision.decided_at >= start,
            PolicyDecision.decided_at < end,
            PolicyDecision.outcome_event_id == None,
            PolicyDecision.superseded_by == None,
        )
    ).all()
    future = []
    for decision in decisions:
        if decision.decided_at > now:
            decision.superseded_by = selected.id
            future.append(decision.id)
        if decision.action:
            plan = db.exec(
                select(RecommendationPlan).where(
                    RecommendationPlan.plan_id == decision.plan_id,
                    RecommendationPlan.scheduled_for == decision.decided_at,
                    RecommendationPlan.is_deleted == False,
                )
            ).first()
            if plan:
                plan.is_deleted = True
                plan.deleted_at = now
    if future:
        # A speculative successor is replaced before its elapsed-time reward can be released.
        db.execute(
            delete(PolicyTransition).where(
                PolicyTransition.next_decision_id.in_(future),
                PolicyTransition.status == "pending",
            )
        )


@router.post("/", response_model=dict)
def create_selected_contents(
    selected_contents: Dict[UUID, SelectedContentCreate],
    db: Session = Depends(get_db),
):
    with db.no_autoflush:  # Disable autoflush for the entire operation
        for user_id, content in sorted(selected_contents.items()):
            lock_user(db, user_id)
            logger.debug(f"Processing user_id: {user_id}")

            # Check if user exists, create if not
            existing_user = db.exec(select(User).where(User.id == user_id)).first()
            if not existing_user:
                logger.debug(f"Creating new user with user_id: {user_id}")
                db_user = User(
                    id=user_id,
                    created_at=datetime.now(timezone.utc),
                    is_deleted=False,
                )
                db.add(db_user)
                db.flush()  # Ensure user is persisted before referencing
                logger.debug(f"User {user_id} created and flushed")
            else:
                logger.debug(f"User {user_id} already exists")

            raw_contents_models = list(content.contents)  # keep repeats, keep order
            incoming_contents = [
                c.model_dump(mode="json", exclude_none=True)
                for c in raw_contents_models
            ]

            # Normalize times once
            mst_utc = (
                _to_utc(content.mission_start_time)
                if content.mission_start_time
                else None
            )
            met_utc = (
                _to_utc(content.mission_end_time) if content.mission_end_time else None
            )
            # Upsert-by-plan_id (same user): update if exists, else insert
            daily = content.delivery_date is not None
            internal_id = (
                uuid5(user_id, content.plan_id) if daily else UUID(content.plan_id)
            )
            header = dict(
                source_plan_id=content.plan_id if daily else None,
                source_created_at=content.created_at,
                delivery_date=content.delivery_date,
                mission_id=content.mission_id,
                mission_process_id=content.mission_process_id,
                task_scheduled_for=content.task_scheduled_for,
            )
            existing_by_id = db.exec(
                select(SelectedContent).where(SelectedContent.id == internal_id)
            ).first()
            if existing_by_id:
                if str(existing_by_id.user_id) != str(user_id):
                    logger.error(
                        "Incoming plan_id=%s belongs to another user (existing=%s, incoming=%s)",
                        content.plan_id,
                        existing_by_id.user_id,
                        user_id,
                    )
                    raise HTTPException(
                        status_code=409,
                        detail=f"plan_id {content.plan_id} already exists for another user ({existing_by_id.user_id})",
                    )
                same_payload = (
                    (existing_by_id.contents or []) == incoming_contents
                    and _to_utc(existing_by_id.mission_start_time) == mst_utc
                    and _to_utc(existing_by_id.mission_end_time) == met_utc
                    and all(
                        getattr(existing_by_id, key) == value
                        for key, value in header.items()
                    )
                    and not existing_by_id.is_deleted
                )
                if same_payload:
                    logger.debug(
                        "SelectedContent already exists for user %s and plan_id=%s, skipping",
                        user_id,
                        content.plan_id,
                    )
                    continue
                if daily or existing_by_id.source_plan_id is not None:
                    raise HTTPException(
                        409, "CS reused a plan ID with a different payload"
                    )
                # UPDATE in place
                existing_by_id.contents = incoming_contents
                existing_by_id.mission_start_time = mst_utc
                existing_by_id.mission_end_time = met_utc
                existing_by_id.modified_at = datetime.now(timezone.utc)
                logger.debug(
                    "Updated SelectedContent for user %s and plan_id=%s",
                    user_id,
                    content.plan_id,
                )
            else:
                if daily:
                    previous = db.exec(
                        select(SelectedContent)
                        .where(
                            SelectedContent.user_id == user_id,
                            SelectedContent.delivery_date == content.delivery_date,
                        )
                        .order_by(SelectedContent.source_created_at.desc())
                    ).first()
                    if previous:
                        if previous.source_created_at == header["source_created_at"]:
                            raise HTTPException(
                                409, "Different CS revisions have the same created_at"
                            )
                        if previous.source_created_at > header["source_created_at"]:
                            continue
                db_content = SelectedContent(
                    **header,
                    id=internal_id,
                    user_id=user_id,
                    contents=incoming_contents,
                    mission_start_time=mst_utc,
                    mission_end_time=met_utc,
                    created_at=datetime.now(timezone.utc),
                    is_deleted=False,
                )
                db.add(db_content)
                if daily:
                    db.flush()
                    _replace_day(db, db_content)
                logger.debug(
                    "Added SelectedContent for user %s and plan_id=%s",
                    user_id,
                    content.plan_id,
                )

            _ensure_catalog(db, raw_contents_models)

            try:
                db.commit()
            except IntegrityError as e:
                db.rollback()
                logger.exception("Commit failed due to integrity error")
                raise HTTPException(
                    status_code=409, detail="Duplicate SelectedContent id"
                ) from e

        db.commit()
        logger.debug("Committed all changes")
    return selected_contents


@router.get("/", response_model=dict)
def list_selected_contents(
    db: Session = Depends(get_db),
    user_id: UUID | None = Query(None),
    include_deleted: bool = Query(False),
):
    def _isoz(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")

    stmt = select(SelectedContent)
    if user_id:
        stmt = stmt.where(SelectedContent.user_id == user_id)
    if not include_deleted:
        stmt = stmt.where(SelectedContent.is_deleted == False)
    rows = db.exec(stmt.order_by(SelectedContent.created_at.desc())).all()

    # Return ALL SelectedContent per user (most recent first due to ORDER BY)
    out: Dict[str, list[dict]] = {}
    for sc in rows:
        uid = str(sc.user_id)
        out.setdefault(uid, []).append(
            {
                "contents": sc.contents or [],
                "mission_start_time": _isoz(sc.mission_start_time),
                "mission_end_time": _isoz(sc.mission_end_time),
                "plan_id": sc.source_plan_id or str(sc.id),
                **(
                    dict(
                        created_at=_isoz(sc.source_created_at),
                        mission_id=sc.mission_id,
                        mission_process_id=sc.mission_process_id,
                        delivery_date=sc.delivery_date.isoformat(),
                        task_scheduled_for=_isoz(sc.task_scheduled_for),
                    )
                    if sc.delivery_date
                    else {}
                ),
            }
        )

    return out
