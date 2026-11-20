"""Retain time checkpoints, ordered replay and daily CS plan revisions."""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20261120_07"
down_revision = "20261004_06"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "superseded_by" not in {
        row["name"] for row in inspector.get_columns("PolicyDecision")
    }:
        op.add_column(
            "PolicyDecision",
            sa.Column(
                "superseded_by", UUID(as_uuid=True), sa.ForeignKey("SelectedContent.id")
            ),
        )
        op.drop_constraint("uq_policy_user_epoch", "PolicyDecision", type_="unique")
        op.create_unique_constraint(
            "uq_policy_user_epoch",
            "PolicyDecision",
            ["user_id", "decided_at", "superseded_by"],
            postgresql_nulls_not_distinct=True,
        )
    columns = {row["name"] for row in inspector.get_columns("SelectedContent")}
    for name, kind in (
        ("source_plan_id", sa.String),
        ("source_created_at", sa.DateTime),
        ("delivery_date", sa.Date),
        ("task_scheduled_for", sa.DateTime),
        ("mission_id", sa.String),
        ("mission_process_id", sa.Integer),
    ):
        if name not in columns:
            op.add_column(
                "SelectedContent",
                sa.Column(name, kind(timezone=True) if kind is sa.DateTime else kind()),
            )
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_SelectedContent_delivery_date" ON "SelectedContent" (delivery_date)'
    )
    if "policy_timezone" not in {
        row["name"] for row in inspector.get_columns("PolicyLearner")
    }:
        op.add_column("PolicyLearner", sa.Column("policy_timezone", sa.String()))
        bind.execute(
            sa.text('UPDATE "PolicyLearner" SET policy_timezone = :zone'),
            {"zone": os.getenv("POLICY_TIMEZONE", "Europe/Berlin")},
        )
        op.alter_column("PolicyLearner", "policy_timezone", nullable=False)
    if "replay_index" not in {
        row["name"] for row in inspector.get_columns("PolicyTransition")
    }:
        op.add_column("PolicyTransition", sa.Column("replay_index", sa.Integer))
        op.create_unique_constraint(
            "uq_policy_replay_index",
            "PolicyTransition",
            ["user_id", "device", "base_version", "replay_index"],
        )
    op.execute("""
        WITH ordered AS (
            SELECT id, row_number() OVER (PARTITION BY user_id, device, base_version
                ORDER BY consumed_at, released_at, id) AS n
            FROM "PolicyTransition" WHERE consumed_at IS NOT NULL
        )
        UPDATE "PolicyTransition" t SET replay_index = ordered.n
        FROM ordered WHERE t.id = ordered.id AND t.replay_index IS NULL
    """)
    op.execute("""
        UPDATE "PolicyLearner" l SET checkpoint = checkpoint || jsonb_build_object(
            'format_version', 2, 'replay_count', (SELECT count(*) FROM "PolicyTransition" t
            WHERE t.user_id = l.user_id AND t.device = l.device AND t.base_version = l.base_version
            AND t.consumed_at IS NOT NULL)) WHERE NOT (checkpoint ? 'replay_count') AND checkpoint->>'format_version' = '1'
    """)
    if not inspector.has_table("PolicyCheckpoint"):
        op.create_table(
            "PolicyCheckpoint",
            sa.Column(
                "learner_id",
                UUID(as_uuid=True),
                sa.ForeignKey("PolicyLearner.id"),
                primary_key=True,
            ),
            sa.Column("saved_at", sa.DateTime(timezone=True), primary_key=True),
            sa.Column("checkpoint", JSONB, nullable=False),
        )
    op.execute("""
        INSERT INTO "PolicyCheckpoint" (learner_id, saved_at, checkpoint)
        SELECT id, updated_at, checkpoint FROM "PolicyLearner" l
        WHERE NOT EXISTS (SELECT 1 FROM "PolicyCheckpoint" c WHERE c.learner_id = l.id)
    """)


def downgrade():
    op.drop_constraint("uq_policy_user_epoch", "PolicyDecision", type_="unique")
    op.create_unique_constraint(
        "uq_policy_user_epoch", "PolicyDecision", ["user_id", "decided_at"]
    )
    op.drop_column("PolicyDecision", "superseded_by")
    op.drop_table("PolicyCheckpoint")
    op.drop_column("PolicyLearner", "policy_timezone")
    op.drop_constraint("uq_policy_replay_index", "PolicyTransition", type_="unique")
    op.drop_column("PolicyTransition", "replay_index")
    op.execute(
        """UPDATE "PolicyLearner" SET checkpoint = (checkpoint - 'replay_count') || jsonb_build_object('format_version', 1)"""
    )
    for name in (
        "delivery_date",
        "source_plan_id",
        "source_created_at",
        "task_scheduled_for",
        "mission_id",
        "mission_process_id",
    ):
        op.drop_column("SelectedContent", name)
