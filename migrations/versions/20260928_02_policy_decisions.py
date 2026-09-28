"""Record scheduled occurrences and policy decisions.

Revision ID: 20260928_02
Revises: 20260928_01
"""

from alembic import op
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    inspect,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260928_02"
down_revision = "20260928_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add occurrence identity and decision records."""
    connection = op.get_bind()
    columns = {
        column["name"]
        for column in inspect(connection).get_columns("RecommendationPlan")
    }
    if "occurrence_index" not in columns:
        op.add_column("RecommendationPlan", Column("occurrence_index", Integer))
    if "deadline" not in columns:
        op.add_column("RecommendationPlan", Column("deadline", DateTime(timezone=True)))
    if "policy_version" not in columns:
        op.add_column("RecommendationPlan", Column("policy_version", String))
    constraints = {
        constraint["name"]
        for constraint in inspect(connection).get_unique_constraints(
            "RecommendationPlan"
        )
    }
    if "uq_plan_occurrence" not in constraints:
        op.create_unique_constraint(
            "uq_plan_occurrence", "RecommendationPlan", ["plan_id", "occurrence_index"]
        )
    if not inspect(connection).has_table("PolicyDecision"):
        op.create_table(
            "PolicyDecision",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column(
                "user_id", UUID(as_uuid=True), ForeignKey("User.id"), nullable=False
            ),
            Column(
                "plan_id",
                UUID(as_uuid=True),
                ForeignKey("SelectedContent.id"),
                nullable=False,
            ),
            Column("occurrence_index", Integer, nullable=False),
            Column("decided_at", DateTime(timezone=True), nullable=False),
            Column("action", Integer, nullable=False),
            Column("propensity", Float, nullable=False),
            Column("state", JSONB, nullable=False),
            Column("safe_mask", JSONB, nullable=False),
            Column("policy_version", String, nullable=False),
            Column("outcome_event_id", UUID(as_uuid=True), ForeignKey("Event.id")),
            Column("created_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint(
                "user_id", "plan_id", "decided_at", name="uq_policy_epoch"
            ),
        )
        op.create_index("ix_PolicyDecision_user_id", "PolicyDecision", ["user_id"])
        op.create_index("ix_PolicyDecision_plan_id", "PolicyDecision", ["plan_id"])


def downgrade() -> None:
    """Remove policy decision records and occurrence fields."""
    op.drop_table("PolicyDecision")
    op.drop_constraint("uq_plan_occurrence", "RecommendationPlan", type_="unique")
    op.drop_column("RecommendationPlan", "policy_version")
    op.drop_column("RecommendationPlan", "deadline")
    op.drop_column("RecommendationPlan", "occurrence_index")
