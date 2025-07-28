import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy.sql import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.dependencies import get_db
from app.main import app
from app.models.escalation_level import EscalationLevel
from app.models.user import User

load_dotenv()

# Use 'db' as the host since tests run inside the Docker network
DATABASE_URL = f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@db:5432/{os.getenv('POSTGRES_DB')}_test"
engine = create_engine(DATABASE_URL)


def override_get_db():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="function")
def client():
    # Create test database if it doesn't exist
    base_db_url = f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@db:5432/postgres"
    base_engine = create_engine(base_db_url)
    with base_engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        result = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :db_name"),
            {"db_name": f"{os.getenv('POSTGRES_DB')}_test"},
        ).fetchone()
        if not result:
            conn.execute(text(f"CREATE DATABASE {os.getenv('POSTGRES_DB')}_test"))

    SQLModel.metadata.create_all(engine)
    with TestClient(app) as c:
        yield c
    SQLModel.metadata.drop_all(engine)


def test_post_updates_inserts_user_and_escalation_and_disables_user(client):
    # prepare ids
    new_user_id = str(uuid4())
    disabled_user_id = str(uuid4())
    escalation_user_id = str(uuid4())

    # pre-insert a user to be disabled
    with Session(engine) as session:
        u = User(
            id=disabled_user_id,
            characteristics={"gender": "male"},
            is_deleted=False,
        )
        session.add(u)
        session.commit()

    payload = {
        "user_feedback": {
            new_user_id: {
                "events": [
                    {
                        "process_id": 123,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event_name": "notification_sent",
                        "properties": {
                            "content_id": "ERc1",
                            "content_type": "recommendation",
                            "mission_id": "PM1",
                        },
                    }
                ]
            }
        },
        "new_users": {
            new_user_id: {
                "gender": "female",
                "height": 165,
                "userAge": 30,
                "weight": 60,
                "wearable": "yes",
                "residence": "TestCity",
                "enrolmentDate": "2025-07-16",
                "informedConsent": "signed",
                "recruitmentCenter": "TEST",
                "level": 0,
            }
        },
        "disabled_users": {
            disabled_user_id: {"date_disabled": datetime.now(timezone.utc).isoformat()}
        },
        "health_habit_assessments": {
            new_user_id: [
                {
                    "hhs": {"smoking": 20.0},
                    "assessment_timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        },
        "new_missions_and_contents": {
            new_user_id: {
                "update_timestamp": datetime.now(timezone.utc).isoformat(),
                "new_missions": [
                    {
                        "mission": "PM1",
                        "recommendations": ["PRc1", "PRc2"],
                        "resources": ["PRs1"],
                        "prescribed": False,
                        "selection_timestamp": datetime.now(timezone.utc).isoformat(),
                        "finish_timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ],
            }
        },
        "escalation_level": {
            escalation_user_id: [
                {
                    "update_timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": 1,
                    "pillar_id": "EO",
                }
            ]
        },
    }

    response = client.post("/updates", json=payload)
    print(response.json())
    assert response.status_code == 200

    with Session(engine) as session:
        from uuid import UUID

        new_user_obj = session.exec(
            select(User).where(User.id == UUID(new_user_id))
        ).first()
        assert new_user_obj is not None
        assert new_user_obj.characteristics.get("gender") == "female"

        disabled_user_obj = session.exec(
            select(User).where(User.id == UUID(disabled_user_id))
        ).first()
        assert disabled_user_obj.is_deleted is True
        assert disabled_user_obj.deleted_at is not None

        levels = session.exec(
            select(EscalationLevel).where(EscalationLevel.user_id == escalation_user_id)
        ).all()
        assert len(levels) == 1
        assert levels[0].pillar_id == "EO"
        assert levels[0].level == 1
