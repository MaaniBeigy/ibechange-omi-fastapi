"""Keep broker task intervals and unattributed psychological assessments.

Revision ID: 20260928_05
Revises: 20260928_04
"""

from alembic import op
from sqlalchemy import Boolean, Column, String, inspect, text
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260928_05"
down_revision = "20260928_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep source fields that cannot yet be assigned or converted safely."""
    connection = op.get_bind()
    psychological = {
        column["name"]: column
        for column in inspect(connection).get_columns("PsychologicalAssessment")
    }
    for name in ("user_id", "subpillar"):
        if not psychological[name]["nullable"]:
            op.alter_column("PsychologicalAssessment", name, nullable=True)
    if "raw_payload" not in psychological:
        op.add_column("PsychologicalAssessment", Column("raw_payload", JSONB))
    if "source_message_id" not in psychological:
        op.add_column("PsychologicalAssessment", Column("source_message_id", String))

    steps = {
        column["name"]: column
        for column in inspect(connection).get_columns("MissionScheduledStep")
    }
    if not steps["scheduled_for"]["nullable"]:
        op.alter_column("MissionScheduledStep", "scheduled_for", nullable=True)
    for name in ("start_date_raw", "end_date_raw", "source_message_id"):
        if name not in steps:
            op.add_column("MissionScheduledStep", Column(name, String))
    if "is_active" not in steps:
        op.add_column(
            "MissionScheduledStep",
            Column("is_active", Boolean, nullable=False, server_default=text("true")),
        )


def downgrade() -> None:
    """Remove captured source fields when all rows meet the old constraints."""
    op.drop_column("MissionScheduledStep", "is_active")
    op.drop_column("MissionScheduledStep", "source_message_id")
    op.drop_column("MissionScheduledStep", "end_date_raw")
    op.drop_column("MissionScheduledStep", "start_date_raw")
    op.alter_column("MissionScheduledStep", "scheduled_for", nullable=False)
    op.drop_column("PsychologicalAssessment", "source_message_id")
    op.drop_column("PsychologicalAssessment", "raw_payload")
    op.alter_column("PsychologicalAssessment", "subpillar", nullable=False)
    op.alter_column("PsychologicalAssessment", "user_id", nullable=False)
