"""Persist per-participant learners and delayed-feedback replay.

Revision ID: 20261004_06
Revises: 20260928_05
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

revision = "20261004_06"
down_revision = "20260928_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    constraints = {
        row["name"]
        for row in inspect(op.get_bind()).get_unique_constraints("PolicyDecision")
    }
    if "uq_policy_user_epoch" not in constraints:
        op.create_unique_constraint(
            "uq_policy_user_epoch", "PolicyDecision", ["user_id", "decided_at"]
        )
    existing = set(inspect(op.get_bind()).get_table_names())
    if "PolicyLearner" not in existing:
        op.create_table(
            "PolicyLearner",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column(
                "user_id", UUID(as_uuid=True), ForeignKey("User.id"), nullable=False
            ),
            Column("device", String, nullable=False),
            Column("base_version", String, nullable=False),
            Column("checkpoint", JSONB, nullable=False),
            Column("started_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint(
                "user_id", "device", "base_version", name="uq_policy_learner"
            ),
        )
        op.create_index("ix_PolicyLearner_user_id", "PolicyLearner", ["user_id"])
    if "PolicyTransition" not in existing:
        op.create_table(
            "PolicyTransition",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column(
                "user_id", UUID(as_uuid=True), ForeignKey("User.id"), nullable=False
            ),
            Column(
                "decision_id",
                UUID(as_uuid=True),
                ForeignKey("PolicyDecision.id"),
                nullable=False,
            ),
            Column(
                "next_decision_id", UUID(as_uuid=True), ForeignKey("PolicyDecision.id")
            ),
            Column("device", String, nullable=False),
            Column("base_version", String, nullable=False),
            Column("state", JSONB, nullable=False),
            Column("next_state", JSONB, nullable=False),
            Column("action", Integer, nullable=False),
            Column("safe_mask", JSONB, nullable=False),
            Column("next_safe_mask", JSONB, nullable=False),
            Column("elapsed_min", Float, nullable=False),
            Column("bootstrap", Integer, nullable=False),
            Column("status", String, nullable=False),
            Column("objectives", JSONB),
            Column("reward", Float),
            Column("outcome_event_id", UUID(as_uuid=True), ForeignKey("Event.id")),
            Column("opened_event_id", UUID(as_uuid=True), ForeignKey("Event.id")),
            Column("released_at", DateTime(timezone=True)),
            Column("consumed_at", DateTime(timezone=True)),
            Column("created_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint("decision_id", name="uq_policy_transition_decision"),
        )
        op.create_index("ix_PolicyTransition_user_id", "PolicyTransition", ["user_id"])
        op.create_index("ix_PolicyTransition_status", "PolicyTransition", ["status"])


def downgrade() -> None:
    op.drop_table("PolicyTransition")
    op.drop_table("PolicyLearner")
    op.drop_constraint("uq_policy_user_epoch", "PolicyDecision", type_="unique")
