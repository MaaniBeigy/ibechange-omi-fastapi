import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.sql import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.main import app
from app.dependencies import get_db
from app.models.user import User
from app.models.mission import Mission
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent
from fastapi.testclient import TestClient
from dotenv import load_dotenv

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


@pytest.fixture
def setup_selected_content(client):
    # Matches your existing fixture pattern (creates the user & selected content)
    payload = {
        "10b58b39-9a4c-46ad-b98d-03dc28735e6b": {
            "contents": [
                {"id": "NRc30", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc54", "type": "recommendation", "mission_id": "NM31"},
            ],
            "mission_start_time": datetime.now(timezone.utc).isoformat(),
            "mission_end_time": (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
            "plan_id": "1b1a1f51-701a-1ab0-afe0-05f100441796",
        }
    }
    resp = client.post("/selected_contents/", json=payload)
    assert resp.status_code == 200


def _iso(dt: datetime) -> str:
    # your endpoints expect naive ISO strings
    return dt.replace(tzinfo=None).isoformat()


def test_get_notification_plans_basic(client, setup_selected_content):
    start_time = _iso(datetime.now(timezone.utc))
    end_time = _iso(datetime.now(timezone.utc) + timedelta(days=1))

    resp = client.get(
        f"/notification_plans/?start_time={start_time}&end_time={end_time}"
    )
    assert resp.status_code == 200

    data = resp.json()
    # No wrapper key
    assert "notification_plans" not in data

    # Dict keyed by user UUID (string in JSON)
    user_id = "10b58b39-9a4c-46ad-b98d-03dc28735e6b"
    assert user_id in data
    assert "contents" in data[user_id]
    contents = data[user_id]["contents"]
    assert isinstance(contents, list)
    assert len(contents) > 0

    # Check item fields exist and have expected types/values
    item = contents[0]
    for k in ("id", "send_timestamp", "type", "mission"):
        assert k in item
    item = data[user_id]["contents"][0]
    # mission must be a str
    assert isinstance(item["mission"], str)
    # id must be one of selected contents for this user
    assert item["id"] in {"NRc30", "NRc54"}
    assert item["mission"] == "NM31"


def test_notification_plans_invalid_datetime_format(client):
    resp = client.get(
        "/notification_plans/?start_time=not-a-date&end_time=2025-07-07T09:00:00"
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "Invalid datetime format"}


def test_notification_plans_empty_when_no_generation(client, monkeypatch):
    # Force quotas to zero so generation returns nothing
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_WEEK", "0")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_WEEK", "0")
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_DAY", "0")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_DAY", "0")

    now = datetime.now(timezone.utc)

    # Minimal DB setup so the router attempts generation (and returns empty)
    with Session(engine) as session:
        user_id = uuid.uuid4()
        session.add(User(id=user_id, created_at=now, is_deleted=False))
        rec = Recommendation(
            content_id="RC_ZERO",
            content_type="recommendation",
            missions=[],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        mission = Mission(mission_id="MC_ZERO")
        session.add_all([rec, mission])
        session.commit()
        session.add(
            SelectedContent(
                id="6b5a6f36-701a-1ab0-afe0-05f100441733",
                user_id=user_id,
                contents=[
                    {
                        "id": "RC_ZERO",
                        "type": "recommendation",
                        "mission_id": "MC_ZERO",
                    }
                ],
                created_at=now,
            )
        )
        session.commit()

    start_iso = _iso(now + timedelta(minutes=1))
    end_iso = _iso(now + timedelta(hours=1))
    resp = client.get(f"/notification_plans/?start_time={start_iso}&end_time={end_iso}")
    assert resp.status_code == 200
    # With no plans generated or existing, top-level dict is empty
    assert resp.json() == {}


def test_notification_plans_uses_existing_records(client, setup_selected_content):
    """
    Insert an existing RecommendationPlan with a valid mission_id
    and verify it appears in /notification_plans with the correct fields.
    """
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        user_id = uuid.UUID("10b58b39-9a4c-46ad-b98d-03dc28735e6b")

        # Ensure a Recommendation exists for this content
        rec = Recommendation(
            content_id="ARc5",
            content_type="recommendation",
            missions=[],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        session.add(rec)
        session.commit()

        # Create a mission and capture its UUID before session closes
        mission = Mission(mission_id="NM_TEST_NP")
        session.add(mission)
        session.commit()
        mission_id_str = str(mission.id)  # <-- capture now

        # (setup_selected_content already created SelectedContent with this plan_id)
        plan_time = now + timedelta(hours=1)
        session.add(
            RecommendationPlan(
                user_id=user_id,
                plan_id=uuid.UUID("1b1a1f51-701a-1ab0-afe0-05f100441796"),
                recommendation_id=rec.id,
                mission_id=mission.id,  # REQUIRED
                scheduled_for=plan_time,
            )
        )
        session.commit()

    start_iso = _iso(now)
    end_iso = _iso(now + timedelta(hours=2))
    resp = client.get(f"/notification_plans/?start_time={start_iso}&end_time={end_iso}")
    assert resp.status_code == 200
    data = resp.json()

    user_key = "10b58b39-9a4c-46ad-b98d-03dc28735e6b"
    assert user_key in data
    contents = data[user_key]["contents"]

    # Find our ARc5 item
    arc5_items = [it for it in contents if it["id"] == "ARc5"]
    assert len(arc5_items) == 1
    itm = arc5_items[0]

    assert itm["type"] == "recommendation"
    assert itm["mission"] == "NM_TEST_NP"
    assert itm["send_timestamp"].startswith(
        plan_time.replace(tzinfo=None).isoformat()[:16]
    )


def test_notification_plans_requires_mission(client):
    """
    If an existing plan lacks mission_id, /notification_plans should 500 (by design).
    """
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        user_id = uuid.uuid4()
        session.add(User(id=user_id, created_at=now, is_deleted=False))

        rec = Recommendation(
            content_id="RC_NO_MISSION",
            content_type="recommendation",
            missions=[],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        session.add(rec)
        session.commit()

        # Create SelectedContent FIRST (to satisfy FK on plan_id)
        plan_id = uuid.uuid4()
        selected = SelectedContent(
            id=plan_id,
            user_id=user_id,
            contents=[
                {
                    "id": "RC_NO_MISSION",
                    "type": "recommendation",
                    "mission_id": "NM_FAKE",
                }
            ],
            created_at=now,
        )
        session.add(selected)
        session.commit()

        # Insert a plan WITHOUT mission_id (NULL) referencing the existing SelectedContent
        session.add(
            RecommendationPlan(
                user_id=user_id,
                plan_id=plan_id,  # FK satisfied
                recommendation_id=rec.id,
                mission_id=None,  # <- missing on purpose
                scheduled_for=now + timedelta(minutes=5),
            )
        )
        session.commit()

    start_iso = _iso(now)
    end_iso = _iso(now + timedelta(hours=1))
    resp = client.get(f"/notification_plans/?start_time={start_iso}&end_time={end_iso}")
    assert resp.status_code == 500
    assert "Missing mission_id" in resp.text


def test_notification_plans_invalid_start_datetime_format(client):
    # Triggers: except ValueError -> HTTP 422
    resp = client.get(
        "/notification_plans/?start_time=not-a-date&end_time=2025-07-07T09:00:00"
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "Invalid datetime format"}


def test_notification_plans_invalid_end_datetime_format(client):
    # Also hits the same ValueError branch (bad end_time)
    resp = client.get(
        "/notification_plans/?start_time=2025-07-07T08:00:00&end_time=not-a-date"
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "Invalid datetime format"}


def test_notification_plans_missing_content_type_raises_500(
    client, setup_selected_content, monkeypatch
):
    """
    Force content_type_map to miss a content_id -> triggers:
        raise HTTPException(500, f"Missing content_type for content_id=...")
    """
    from sqlmodel import Session as SQLModelSession  # class to monkeypatch

    # Keep original exec so other queries behave normally
    orig_exec = SQLModelSession.exec

    class _FakeResult:
        def all(self):
            # Return no rows for the specific content_type query
            return []

    def fake_exec(self, statement, *args, **kwargs):
        # Detect the exact query: select(Recommendation.content_id, Recommendation.content_type) ...
        raw_cols = getattr(statement, "_raw_columns", [])
        col_strs = {str(c) for c in raw_cols}
        if (
            "Recommendation.content_id" in col_strs
            and "Recommendation.content_type" in col_strs
        ):
            return _FakeResult()
        return orig_exec(self, statement, *args, **kwargs)

    monkeypatch.setattr(SQLModelSession, "exec", fake_exec, raising=True)

    # Call the endpoint with a valid window; our monkeypatch makes the map empty
    start_time = _iso(datetime.now(timezone.utc))
    end_time = _iso(datetime.now(timezone.utc) + timedelta(days=1))
    resp = client.get(
        f"/notification_plans/?start_time={start_time}&end_time={end_time}"
    )

    assert resp.status_code == 500
    assert "Missing content_type for content_id=" in resp.text
