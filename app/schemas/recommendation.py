from typing import List

from pydantic import BaseModel


class RecommendationCreate(BaseModel):
    content_id: str
    content_type: str
    missions: List[str]
    objective: List[str]
    hapa: List[str]
    comb: List[str]
    intervention_type: List[str]


class RecommendationList(BaseModel):
    recommendations: List[RecommendationCreate]


class CatalogItem(BaseModel):
    missions: list[str]
    com_b: str | list[str] = []
    intervention_types: list[str] = []
    objective: list[str] = []
    hapa: list[str] = []


def normalize_catalog(
    items: dict[str, CatalogItem], content_type: str
) -> RecommendationList:
    return RecommendationList(
        recommendations=[
            RecommendationCreate(
                content_id=key,
                content_type=content_type,
                missions=item.missions,
                objective=item.objective,
                hapa=item.hapa,
                comb=[item.com_b] if isinstance(item.com_b, str) else item.com_b,
                intervention_type=item.intervention_types,
            )
            for key, item in items.items()
        ]
    )
