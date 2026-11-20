"""Retain serving scores and individual optimizer updates."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20261120_08"
down_revision = "20261120_07"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("PolicyDecision")}
    for name in ("q_values", "action_probabilities"):
        if name not in columns:
            op.add_column("PolicyDecision", sa.Column(name, JSONB()))
    # With exactly two actions the historical selected probability determines both.
    op.execute("""
        UPDATE "PolicyDecision"
        SET action_probabilities = CASE action
            WHEN 0 THEN jsonb_build_array(propensity, 1.0 - propensity)
            ELSE jsonb_build_array(1.0 - propensity, propensity) END
        WHERE action_probabilities IS NULL AND action IN (0, 1)
            AND propensity > 0 AND propensity <= 1
    """)
    if not inspector.has_table("PolicyTrainingUpdate"):
        op.create_table(
            "PolicyTrainingUpdate",
            sa.Column(
                "learner_id",
                UUID(as_uuid=True),
                sa.ForeignKey("PolicyLearner.id"),
                primary_key=True,
            ),
            sa.Column("update_number", sa.Integer(), primary_key=True),
            sa.Column("fitted_at", sa.DateTime(timezone=True), nullable=False),
            *(
                sa.Column(name, sa.Float(), nullable=False)
                for name in (
                    "learning_rate",
                    "cql_alpha",
                    "td_loss",
                    "cql_loss",
                    "total_loss",
                    "td_error",
                )
            ),
            *(
                sa.Column(name, JSONB(), nullable=False)
                for name in (
                    "offline_indices",
                    "personal_replay_indices",
                    "q_values",
                    "targets",
                    "td_residuals",
                )
            ),
        )


def downgrade():
    op.drop_table("PolicyTrainingUpdate")
    op.drop_column("PolicyDecision", "action_probabilities")
    op.drop_column("PolicyDecision", "q_values")
