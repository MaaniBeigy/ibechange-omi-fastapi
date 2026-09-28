"""Keep queue occurrence identity when POLIMI changes item order.

Revision ID: 20260928_03
Revises: 20260928_02
"""

from alembic import op
from sqlalchemy import Column, Integer, String, inspect

revision = "20260928_03"
down_revision = "20260928_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add stable occurrence identity and original content metadata."""
    connection = op.get_bind()
    columns = {
        column["name"]
        for column in inspect(connection).get_columns("RecommendationPlan")
    }
    for name, column_type in (
        ("occurrence_key", String),
        ("content_type", String),
        ("mission_process_id", Integer),
    ):
        if name not in columns:
            op.add_column("RecommendationPlan", Column(name, column_type))
    constraints = {
        constraint["name"]
        for constraint in inspect(connection).get_unique_constraints(
            "RecommendationPlan"
        )
    }
    if "uq_plan_occurrence" in constraints:
        op.drop_constraint("uq_plan_occurrence", "RecommendationPlan", type_="unique")
    if "uq_plan_occurrence_key" not in constraints:
        op.create_unique_constraint(
            "uq_plan_occurrence_key",
            "RecommendationPlan",
            ["plan_id", "occurrence_key"],
        )


def downgrade() -> None:
    """Restore the prior occurrence index constraint."""
    op.drop_constraint("uq_plan_occurrence_key", "RecommendationPlan", type_="unique")
    op.create_unique_constraint(
        "uq_plan_occurrence", "RecommendationPlan", ["plan_id", "occurrence_index"]
    )
    op.drop_column("RecommendationPlan", "mission_process_id")
    op.drop_column("RecommendationPlan", "content_type")
    op.drop_column("RecommendationPlan", "occurrence_key")
