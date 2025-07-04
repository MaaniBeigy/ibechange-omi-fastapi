from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.schemas.selected_content import SelectedContentList

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
        # Check if SelectedContent exists, create if not
        existing_content = db.exec(
            select(SelectedContent).where(SelectedContent.user_id == user_id)
        ).first()
        if existing_content:
            continue
        db_content = SelectedContent(user_id=user_id, **content.model_dump())
        db.add(db_content)
    db.commit()
    return selected_contents
