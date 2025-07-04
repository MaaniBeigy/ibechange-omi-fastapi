from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from typing import cast

from app.dependencies import get_db
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.schemas.selected_content import SelectedContentList
from app.models.recommendation import Recommendation

router = APIRouter(prefix="/selected_contents", tags=["Selected Contents"])


@router.post("/", response_model=SelectedContentList)
def create_selected_contents(
    selected_contents: SelectedContentList, db: Session = Depends(get_db)
):
    for user_id, content in selected_contents.selected_contents.items():
        # Check if user exists, create if not
        existing_user = db.exec(select(User).where(User.user_id == user_id)).first()
        if not existing_user:
            db_user = User(user_id=user_id)
            db.add(db_user)
        # Fetch all SelectedContent for this user
        user_contents = db.exec(
            select(SelectedContent).where(SelectedContent.user_id == user_id)
        ).all()
        # For each content, check in Python if it exists
        for content_id in content.contents:
            already_exists = any(
                content_id in (sc.contents or []) for sc in user_contents
            )
            if not already_exists:
                db_content = SelectedContent(
                    user_id=user_id,
                    contents=[content_id],
                    mission_start_time=content.mission_start_time,
                    mission_end_time=content.mission_end_time
                )
                db.add(db_content)
            # For each new content, check if Recommendation exists, if not, add it
            existing_recommendation = db.exec(
                select(Recommendation).where(Recommendation.rec_id == content_id)
            ).first()
            if not existing_recommendation:
                db_recommendation = Recommendation(
                    rec_id=content_id,
                    missions=[],
                    objective=[],
                    hapa=[],
                    comb=[],
                    intervention_type=[]
                )
                db.add(db_recommendation)
    db.commit()
    return selected_contents
