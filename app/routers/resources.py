from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.resource import Resource
from app.schemas.resource import ResourceList

router = APIRouter(prefix="/resources", tags=["Resources"])


@router.post("/", response_model=ResourceList)
def create_resources(resources: ResourceList, db: Session = Depends(get_db)):
    for res in resources.resources:
        existing_res = db.exec(
            select(Resource).where(Resource.res_id == res.res_id)
        ).first()
        if existing_res:
            continue
        db_res = Resource(**res.model_dump())
        db.add(db_res)
    db.commit()
    return resources
