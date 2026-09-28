"""Store scheduled mission task steps for reminder deadlines.

Revision ID: 20260928_04
Revises: 20260928_03
"""

from alembic import op
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    inspect,
)
from sqlalchemy.dialects.postgresql import UUID

revision = "20260928_04"
down_revision = "20260928_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create scheduled task step records."""
    if inspect(op.get_bind()).has_table("MissionScheduledStep"):
        return
    op.create_table(
        "MissionScheduledStep",
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("user_id", UUID(as_uuid=True), ForeignKey("User.id"), nullable=False),
        Column("mission_process_id", Integer, nullable=False),
        Column("mission_code", String, nullable=False),
        Column("step_id", String, nullable=False),
        Column("scheduled_for", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(
            "user_id",
            "mission_process_id",
            "step_id",
            name="uq_mission_scheduled_step",
        ),
    )
    for name in ("user_id", "mission_process_id", "mission_code"):
        op.create_index(
            f"ix_MissionScheduledStep_{name}", "MissionScheduledStep", [name]
        )


def downgrade() -> None:
    """Remove scheduled task step records."""
    op.drop_table("MissionScheduledStep")
