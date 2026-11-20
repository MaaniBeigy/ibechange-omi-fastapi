from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.recommendation import Recommendation
from app.schemas.recommendation import (
    CatalogItem,
    RecommendationList,
    normalize_catalog,
)

router = APIRouter(prefix="/recommendations", tags=["Recommendations"])


resources_router = APIRouter(prefix="/resources", tags=["Resources"])


@router.post("/", response_model=RecommendationList)
def create_recommendations(
    recommendations: RecommendationList | dict[str, CatalogItem],
    db: Session = Depends(get_db),
):
    if isinstance(recommendations, dict):
        recommendations = normalize_catalog(recommendations, "recommendation")
    for rec in recommendations.recommendations:
        existing_rec = db.exec(
            select(Recommendation).where(Recommendation.content_id == rec.content_id)
        ).first()
        if existing_rec:
            continue
        db_rec = Recommendation(**rec.model_dump())
        db.add(db_rec)
    db.commit()
    return recommendations


@resources_router.post("/", response_model=RecommendationList)
def create_resources(resources: dict[str, CatalogItem], db: Session = Depends(get_db)):
    return create_recommendations(normalize_catalog(resources, "resource"), db)
