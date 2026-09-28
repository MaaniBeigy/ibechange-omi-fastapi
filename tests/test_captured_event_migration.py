"""Check the migration from stored mission deadlines to source intervals."""

import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

import app.models
from app.models.artemis import MissionScheduledStep, PsychologicalAssessment
from app.models.user import User


def test_captured_event_migration_keeps_existing_rows():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL tests")
    schema = f"test_migration_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            user = User(id=uuid4())
            session.add(user)
            session.flush()
            session.add(
                PsychologicalAssessment(
                    user_id=user.id,
                    pillar_id="NO",
                    subpillar="stress",
                    barriers=[],
                    answer_tasklog={},
                    assessment_timestamp=datetime(2026, 9, 28, tzinfo=timezone.utc),
                )
            )
            session.add(
                MissionScheduledStep(
                    user_id=user.id,
                    mission_process_id=25,
                    mission_code="NM1",
                    step_id="1",
                    scheduled_for=datetime(2026, 9, 29, tzinfo=timezone.utc),
                )
            )
            session.commit()
        with engine.begin() as connection:
            connection.execute(
                text(
                    'ALTER TABLE "PsychologicalAssessment" '
                    "DROP COLUMN raw_payload, DROP COLUMN source_message_id, "
                    "ALTER COLUMN user_id SET NOT NULL, "
                    "ALTER COLUMN subpillar SET NOT NULL"
                )
            )
            connection.execute(
                text(
                    'ALTER TABLE "MissionScheduledStep" '
                    "DROP COLUMN start_date_raw, DROP COLUMN end_date_raw, "
                    "DROP COLUMN source_message_id, DROP COLUMN is_active, "
                    "ALTER COLUMN scheduled_for SET NOT NULL"
                )
            )
            migration_path = (
                Path(__file__).resolve().parents[1]
                / "migrations/versions/20260928_05_captured_events.py"
            )
            specification = importlib.util.spec_from_file_location(
                "captured_event_migration", migration_path
            )
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            psychology = {
                column["name"]: column
                for column in inspect(connection).get_columns("PsychologicalAssessment")
            }
            steps = {
                column["name"]: column
                for column in inspect(connection).get_columns("MissionScheduledStep")
            }
            assert psychology["user_id"]["nullable"]
            assert psychology["subpillar"]["nullable"]
            assert {"raw_payload", "source_message_id"} <= psychology.keys()
            assert steps["scheduled_for"]["nullable"]
            assert {
                "start_date_raw",
                "end_date_raw",
                "source_message_id",
                "is_active",
            } <= steps.keys()
            assert (
                connection.execute(text('SELECT count(*) FROM "User"')).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text('SELECT count(*) FROM "PsychologicalAssessment"')
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text('SELECT count(*) FROM "MissionScheduledStep"')
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
