from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from typing import cast, Dict

from app.dependencies import get_db
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.schemas.selected_content import SelectedContentList, SelectedContentCreate
from app.models.recommendation import Recommendation
from app.models.mission import Mission

router = APIRouter(prefix="/selected_contents", tags=["Selected Contents"])


@router.post("/", response_model=dict)
def create_selected_contents(
    selected_contents: Dict[str, SelectedContentCreate], db: Session = Depends(get_db)
):
    for user_id, content in selected_contents.items():
        # Check if user exists, create if not
        existing_user = db.exec(select(User).where(User.user_id == user_id)).first()
        if not existing_user:
            db_user = User(user_id=user_id)
            db.add(db_user)
        # Fetch all SelectedContent for this user
        user_contents = db.exec(
            select(SelectedContent).where(SelectedContent.user_id == user_id)
        ).all()
        # Check if the same contents, mission_start_time, mission_end_time, exists and not deleted 
        already_exists = any(
            sc.contents == [c.model_dump() for c in content.contents] and
            sc.mission_start_time == content.mission_start_time and
            sc.mission_end_time == content.mission_end_time and
            not sc.is_deleted
            for sc in user_contents
        )
        if already_exists:
            continue  # Do nothing if already exists
        # Insert new SelectedContent
        db_content = SelectedContent(
            user_id=user_id,
            contents=[c.model_dump() for c in content.contents],
            mission_start_time=content.mission_start_time,
            mission_end_time=content.mission_end_time
        )
        db.add(db_content)
        # For each content, check/create Recommendation and Mission
        for c in content.contents:
            # Check/create Recommendation (non-deleted)
            existing_recommendation = db.exec(
                select(Recommendation).where(
                    Recommendation.content_id == c.id,
                    Recommendation.is_deleted == False
                )
            ).first()
            if not existing_recommendation:
                db_recommendation = Recommendation(
                    content_id=c.id,
                    content_type=c.type,
                    missions=[c.mission_id],
                    objective=[],
                    hapa=[],
                    comb=[],
                    intervention_type=[]
                )
                db.add(db_recommendation)
            else:
                # Update missions field if mission_id not present
                if c.mission_id not in existing_recommendation.missions:
                    existing_recommendation.missions.append(c.mission_id)
                    db.add(existing_recommendation)
            # Check/create Mission (non-deleted)
            existing_mission = db.exec(
                select(Mission).where(
                    Mission.mission_id == c.mission_id,
                    Mission.is_deleted == False
                )
            ).first()
            if not existing_mission:
                db_mission = Mission(mission_id=c.mission_id)
                db.add(db_mission)
    db.commit()
    return selected_contents
