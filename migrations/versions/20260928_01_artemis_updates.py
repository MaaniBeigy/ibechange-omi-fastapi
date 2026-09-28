"""Add durable Artemis update records.

Revision ID: 20260928_01
Revises:
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
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260928_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create tables used by the Artemis consumer."""
    existing = set(inspect(op.get_bind()).get_table_names())
    if "ArtemisInbox" not in existing:
        op.create_table(
            "ArtemisInbox",
            Column("message_id", String, primary_key=True),
            Column("body_sha256", String, nullable=False),
            Column("event_name", String, nullable=False),
            Column("processed_at", DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ArtemisInbox_event_name", "ArtemisInbox", ["event_name"])
    if "ArtemisFailure" not in existing:
        op.create_table(
            "ArtemisFailure",
            Column("message_id", String, primary_key=True),
            Column("body_sha256", String, nullable=False),
            Column("reason", String, nullable=False),
            Column("received_at", DateTime(timezone=True), nullable=False),
        )
    if "PsychologicalAssessment" not in existing:
        op.create_table(
            "PsychologicalAssessment",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column(
                "user_id", UUID(as_uuid=True), ForeignKey("User.id"), nullable=False
            ),
            Column("pillar_id", String, nullable=False),
            Column("subpillar", String, nullable=False),
            Column("barriers", JSONB, nullable=False),
            Column("answer_tasklog", JSONB, nullable=False),
            Column("assessment_timestamp", DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_PsychologicalAssessment_user_id", "PsychologicalAssessment", ["user_id"]
        )
    if "PillarSelection" not in existing:
        op.create_table(
            "PillarSelection",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column(
                "user_id", UUID(as_uuid=True), ForeignKey("User.id"), nullable=False
            ),
            Column("pillar_id", String, nullable=False),
            Column("selected_at", DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_PillarSelection_user_id", "PillarSelection", ["user_id"])
    if "MissionSelection" not in existing:
        op.create_table(
            "MissionSelection",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column(
                "user_id", UUID(as_uuid=True), ForeignKey("User.id"), nullable=False
            ),
            Column(
                "mission_id",
                UUID(as_uuid=True),
                ForeignKey("Mission.id"),
                nullable=False,
            ),
            Column("process_id", Integer, nullable=False),
            Column("mission_process_id", Integer, nullable=False),
            Column("selected_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint(
                "user_id", "mission_process_id", name="uq_mission_selection"
            ),
        )
        op.create_index("ix_MissionSelection_user_id", "MissionSelection", ["user_id"])
        op.create_index(
            "ix_MissionSelection_mission_id", "MissionSelection", ["mission_id"]
        )
        op.create_index(
            "ix_MissionSelection_mission_process_id",
            "MissionSelection",
            ["mission_process_id"],
        )


def downgrade() -> None:
    """Drop Artemis update tables in reverse dependency order."""
    op.drop_table("MissionSelection")
    op.drop_table("PillarSelection")
    op.drop_table("PsychologicalAssessment")
    op.drop_table("ArtemisFailure")
    op.drop_table("ArtemisInbox")
