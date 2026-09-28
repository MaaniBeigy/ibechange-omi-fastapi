import logging
from datetime import datetime, timezone
from typing import Dict, Set, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.mission import Mission
from app.models.recommendation import Recommendation
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.schemas.selected_content import SelectedContentCreate, SelectedContentList
from app.utils.helpers import _to_utc

router = APIRouter(prefix="/selected_contents", tags=["Selected Contents"])

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


@router.post("/", response_model=dict)
def create_selected_contents(
    selected_contents: Dict[str, SelectedContentCreate],
    db: Session = Depends(get_db),
):
    with db.no_autoflush:  # Disable autoflush for the entire operation
        for user_id, content in selected_contents.items():
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

            def _naive_utc(dt: datetime | None) -> datetime | None:
                """Strip tzinfo post-conversion to UTC so DB / JSON timestamps match."""
                if dt is None:
                    return None
                return dt.astimezone(timezone.utc).replace(tzinfo=None)

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
            existing_by_id = db.exec(
                select(SelectedContent).where(SelectedContent.id == content.plan_id)
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
                    and _naive_utc(existing_by_id.mission_start_time)
                    == _naive_utc(mst_utc)
                    and _naive_utc(existing_by_id.mission_end_time)
                    == _naive_utc(met_utc)
                    and not existing_by_id.is_deleted
                )
                if same_payload:
                    logger.debug(
                        "SelectedContent already exists for user %s and plan_id=%s, skipping",
                        user_id,
                        content.plan_id,
                    )
                    continue
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
                db_content = SelectedContent(
                    id=content.plan_id,
                    user_id=user_id,
                    contents=incoming_contents,
                    mission_start_time=mst_utc,
                    mission_end_time=met_utc,
                    created_at=datetime.now(timezone.utc),
                    is_deleted=False,
                )
                db.add(db_content)
                logger.debug(
                    "Added SelectedContent for user %s and plan_id=%s",
                    user_id,
                    content.plan_id,
                )

            # Collect unique mission_ids
            seen_ids = set()
            unique_for_meta = []
            for c in raw_contents_models:
                if c.id not in seen_ids:
                    unique_for_meta.append(c)
                    seen_ids.add(c.id)
            mission_ids: Set[str] = {
                c.mission_id for c in unique_for_meta if c.mission_id
            }
            logger.debug(f"Unique mission_ids: {mission_ids}")

            # Process Recommendations and Missions
            for c in unique_for_meta:
                existing_recommendation = db.exec(
                    select(Recommendation).where(
                        Recommendation.content_id == c.id,
                        Recommendation.is_deleted == False,
                    )
                ).first()
                if not existing_recommendation:
                    db_recommendation = Recommendation(
                        content_id=c.id,
                        content_type=c.type.lower(),
                        missions=[c.mission_id] if c.mission_id else [],
                        objective=[],
                        hapa=[],
                        comb=[],
                        intervention_type=[],
                        created_at=datetime.now(timezone.utc),
                        is_deleted=False,
                    )
                    db.add(db_recommendation)
                    logger.debug(f"Created new Recommendation for content_id: {c.id}")
                else:
                    # Update missions field if mission_id not present
                    if c.mission_id and c.mission_id not in (
                        existing_recommendation.missions or []
                    ):
                        existing_recommendation.missions = (
                            existing_recommendation.missions or []
                        ) + [c.mission_id]
                        logger.debug(
                            f"Updated Recommendation missions for content_id: {c.id}"
                        )

            # Check/create Missions for unique mission_ids
            for mission_id in mission_ids:
                existing_mission = db.exec(
                    select(Mission).where(
                        Mission.mission_id == mission_id,
                        Mission.is_deleted == False,
                    )
                ).first()
                if not existing_mission:
                    db_mission = Mission(
                        mission_id=mission_id,
                        created_at=datetime.now(timezone.utc),
                        is_deleted=False,
                    )
                    db.add(db_mission)
                    logger.debug(f"Created new Mission with mission_id: {mission_id}")
                else:
                    logger.debug(f"Mission {mission_id} already exists")

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
                "plan_id": str(sc.id),
            }
        )

    return out
