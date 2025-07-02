import os
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy.sql import text
from sqlmodel import Session, SQLModel, create_engine

from app.dependencies import get_db
from app.main import app
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent

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
    # Use autocommit to avoid transaction issues with CREATE DATABASE
    with base_engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        # Check if the test database exists
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


@pytest.fixture
def setup_selected_content():
    with Session(engine) as session:
        content = SelectedContent(
            user_id="user_01",
            contents=["ARc5", "ARc13"],
            mission_start_time=datetime.now(timezone.utc),
            mission_end_time=datetime.now(timezone.utc) + timedelta(days=1),
        )
        session.add(content)
        session.commit()


def test_get_recommendation_plans(client, setup_selected_content):
    start_time = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    end_time = (
        (datetime.now(timezone.utc) + timedelta(days=1))
        .replace(tzinfo=None)
        .isoformat()
    )
    response = client.get(
        f"/recommendation_plans/?start_time={start_time}&end_time={end_time}"
    )
    assert response.status_code == 200
    data = response.json()
    assert "recommendation_plans" in data
    assert len(data["recommendation_plans"]) > 0
    assert data["recommendation_plans"][0]["user_id"] == "user_01"


def test_get_existing_recommendation_plans(client, setup_selected_content):
    start_time = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    end_time = (
        (datetime.now(timezone.utc) + timedelta(days=1))
        .replace(tzinfo=None)
        .isoformat()
    )
    with Session(engine) as session:
        plan = RecommendationPlan(
            user_id="user_01",
            content_id="ARc5",
            scheduled_for=(
                (datetime.now(timezone.utc) + timedelta(hours=1))
                .replace(tzinfo=None)
                .isoformat()
            ),
        )
        session.add(plan)
        session.commit()
    response = client.get(
        f"/recommendation_plans/?start_time={start_time}&end_time={end_time}"
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["recommendation_plans"]) == 1
    assert data["recommendation_plans"][0]["plans"][0]["content_id"] == "ARc5"
