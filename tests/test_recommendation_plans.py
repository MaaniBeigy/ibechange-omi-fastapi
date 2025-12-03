import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy.sql import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.dependencies import get_db
from app.main import app
from app.models.mission import Mission
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.services.recommendation_plan_service import generate_recommendation_plan

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


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



@pytest.fixture
def setup_selected_content(client):
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
    response = client.post("/selected_contents/", json=payload)
    assert response.status_code == 200


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
    assert (
        data["recommendation_plans"][0]["user_id"]
        == "10b58b39-9a4c-46ad-b98d-03dc28735e6b"
    )


def test_get_existing_recommendation_plans(client, setup_selected_content):
    start_time = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    end_time = (
        (datetime.now(timezone.utc) + timedelta(days=1))
        .replace(tzinfo=None)
        .isoformat()
    )
    with Session(engine) as session:
        # Insert recommendation first
        recommendation = Recommendation(
            content_id="ARc5",
            content_type="recommendation",
            missions=[],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        session.add(recommendation)
        session.commit()

        # Now insert the plan using the recommendation_id
        plan = RecommendationPlan(
            user_id="10b58b39-9a4c-46ad-b98d-03dc28735e6b",
            plan_id="1b1a1f51-701a-1ab0-afe0-05f100441796",
            recommendation_id=recommendation.id,
            scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
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


def test_recent_plan_in_cooldown_skips_generation(client):
    now = datetime.now(timezone.utc)
    user_id = uuid.uuid4()

    with Session(engine) as session:
        # prerequisite recommendation + mission
        rec = Recommendation(
            content_id="RC_SKIP",
            content_type="recommendation",
            missions=[],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        mission = Mission(mission_id="MC_SKIP")
        session.add(User(id=user_id, created_at=now, is_deleted=False))
        session.add_all([rec, mission])
        session.commit()

        # selected content for the user
        session.add(
            SelectedContent(
                id="6b5a6f66-701a-1ab0-afe0-05f100441766",
                user_id=user_id,
                contents=[
                    {"id": "RC_SKIP", "type": "recommendation", "mission_id": "MC_SKIP"}
                ],
                created_at=now,
            )
        )
        # recent plan (10 min ago) triggers the cooldown logic
        session.add(
            RecommendationPlan(
                user_id=user_id,
                plan_id="6b5a6f66-701a-1ab0-afe0-05f100441766",
                recommendation_id=rec.id,
                scheduled_for=now - timedelta(minutes=10),
            )
        )
        session.commit()

    # query a *future* window (no existing plans there)
    start_iso = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    end_iso = (now + timedelta(hours=2)).replace(tzinfo=None).isoformat()
    resp = client.get(
        f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}"
    )
    assert resp.status_code == 200
    assert resp.json() == {"recommendation_plans": []}


def test_generation_returns_empty_when_remaining_zero(client, monkeypatch):
    # weekly / daily quotas -> 0
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_WEEK", "0")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_WEEK", "0")
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_DAY", "0")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_DAY", "0")

    now = datetime.now(timezone.utc)
    user_id = uuid.uuid4()

    with Session(engine) as session:
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
        session.add(User(id=user_id, created_at=now, is_deleted=False))
        session.add_all([rec, mission])
        session.commit()

        session.add(
            SelectedContent(
                id="6b5a6f36-701a-1ab0-afe0-05f100441733",
                user_id=user_id,
                contents=[
                    {"id": "RC_ZERO", "type": "recommendation", "mission_id": "MC_ZERO"}
                ],
                created_at=now,
            )
        )
        session.commit()

    start_iso = (now + timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    end_iso = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    resp = client.get(
        f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}"
    )
    assert resp.status_code == 200
    assert resp.json() == {"recommendation_plans": []}


def test_generation_respects_cooldown_and_break(client, monkeypatch):
    # set quotas so *exactly* 2 notifications are needed in the window
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_WEEK", "2")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_WEEK", "2")
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_DAY", "2")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_DAY", "5")
    monkeypatch.setenv("BETWEEN_NOTIFICATION_COOLDOWN", "30")  # minutes

    now = datetime.now(timezone.utc)
    user_id = uuid.uuid4()

    # deterministic schedule: 10 min, 20 min, 45 min
    def fake_schedule(s, e):
        return [
            s + timedelta(minutes=10),
            s + timedelta(minutes=20),  # will be skipped (too close)
            s + timedelta(minutes=45),
        ]

    monkeypatch.setattr(
        "app.services.recommendation_plan_service.generate_random_schedule",
        fake_schedule,
    )

    with Session(engine) as session:
        rec_a = Recommendation(
            content_id="RC_A",
            content_type="recommendation",
            missions=[],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        rec_b = Recommendation(
            content_id="RC_B",
            content_type="recommendation",
            missions=[],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        mission = Mission(mission_id="MC_CDLN")
        session.add(User(id=user_id, created_at=now, is_deleted=False))
        session.add_all([rec_a, rec_b, mission])
        session.commit()

        session.add(
            SelectedContent(
                id="8b5a6f88-701a-1ab0-afe0-05f100441788",
                user_id=user_id,
                contents=[
                    {"id": "RC_A", "type": "recommendation", "mission_id": "MC_CDLN"},
                    {"id": "RC_B", "type": "recommendation", "mission_id": "MC_CDLN"},
                ],
                created_at=now,
                mission_start_time=now,
                mission_end_time=now + timedelta(hours=1),
            )
        )
        session.commit()

    start_iso = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_iso = (
        (now + timedelta(hours=1))
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    resp = client.get(
        f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}"
    )
    data = resp.json()

    # exactly one user block with **2** scheduled notifications
    assert resp.status_code == 200
    assert len(data["recommendation_plans"]) == 1
    assert len(data["recommendation_plans"][0]["plans"]) == 2


def test_existing_schedule_counting_and_generation(client, monkeypatch):
    """
    One plan already in the window → loop fills existing_by_day.
    We need 3 per week, so 2 more are generated.
    """
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now
    end = now + timedelta(days=5)

    # env: need exactly three notifications
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_WEEK", "7")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_WEEK", "21")
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_DAY", "3")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_DAY", "5")
    monkeypatch.setenv("BETWEEN_NOTIFICATION_COOLDOWN", "30")  # minutes

    # deterministic schedule inside [start, end]
    def fake_schedule(s, e):
        base = s
        return [
            base + timedelta(minutes=10),
            base + timedelta(minutes=40),
            base + timedelta(minutes=90),
            base + timedelta(minutes=120),
        ]

    monkeypatch.setattr(
        "app.services.recommendation_plan_service.generate_random_schedule",
        fake_schedule,
    )

    payload = {
        "474dbf4c-8922-4dac-aa35-92632a7bcaf8": {
            "contents": [
                {"id": "NRs6", "type": "resource", "mission_id": "NM31"},
                {"id": "NRc54", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc54", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc53", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc20", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc53", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc54", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc53", "type": "recommendation", "mission_id": "NM31"},
            ],
            "mission_start_time": (now - timedelta(days=2)).isoformat(),
            "mission_end_time": (now + timedelta(days=7)).isoformat(),
            "plan_id": "7b8a9f54-801a-4ab0-afe0-06f100441793",
        },
        "10b58b39-9a4c-46ad-b98d-03dc28735e6b": {
            "contents": [
                {"id": "NRc30", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc54", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc53", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc54", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc53", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc53", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc54", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc19", "type": "recommendation", "mission_id": "NM31"},
                {"id": "NRc53", "type": "recommendation", "mission_id": "NM31"},
            ],
            "mission_start_time": (now - timedelta(days=2)).isoformat(),
            "mission_end_time": (now + timedelta(days=7)).isoformat(),
            "plan_id": "3b8a9f54-701a-1ab0-afe0-05f100441796",
        },
    }

    start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_iso = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    response = client.post("/selected_contents/", json=payload)
    assert response.status_code == 200
    resp = client.get(
        f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}"
    )
    data = resp.json()

    # One existing + two newly generated = 3 plans
    assert resp.status_code == 200
    assert len(data["recommendation_plans"]) == 2
    assert len(data["recommendation_plans"][0]["plans"]) == 9


def test_existing_by_day_loop_runs_on_multiple_generations(client, monkeypatch):
    # Allow up to 2 notifications per day, require 2
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_DAY", "2")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_DAY", "2")
    monkeypatch.setenv("MIN_NOTIFICATIONS_PER_WEEK", "2")
    monkeypatch.setenv("MAX_NOTIFICATIONS_PER_WEEK", "14")
    monkeypatch.setenv("BETWEEN_NOTIFICATION_COOLDOWN", "1")  # minute

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        second=0, microsecond=0
    )
    user_id = str(uuid.uuid4())

    def fake_schedule(s, e):
        # s will be tomorrow 00:00 in the request below, so these are 10:00 and 12:00 tomorrow
        return [
            s.replace(hour=10, minute=0, second=0, microsecond=0),
            s.replace(hour=12, minute=0, second=0, microsecond=0),
        ]

    monkeypatch.setattr(
        "app.services.recommendation_plan_service.generate_random_schedule",
        fake_schedule,
    )

    with Session(engine) as session:
        # Two recommendations
        rec1 = Recommendation(
            content_id="TST_LOOP1",
            content_type="recommendation",
            missions=[],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        rec2 = Recommendation(
            content_id="TST_LOOP2",
            content_type="recommendation",
            missions=[],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        mission = Mission(mission_id="MID_LOOP")
        user = User(id=user_id, created_at=now, is_deleted=False)
        session.add_all([user, rec1, rec2, mission])
        session.commit()

        # Add selected content with two recommendations for the user
        selected = SelectedContent(
            id="5b5a9f55-701a-1ab0-afe0-05f100441795",
            user_id=user_id,
            contents=[
                {"id": "TST_LOOP1", "type": "recommendation", "mission_id": "MID_LOOP"},
                {"id": "TST_LOOP2", "type": "recommendation", "mission_id": "MID_LOOP"},
            ],
            created_at=future,
            mission_start_time=future.replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
            mission_end_time=future.replace(
                hour=23, minute=59, second=59, microsecond=0
            ),
        )
        session.add(selected)
        session.commit()

    # Query the whole day
    start_iso = (
        future.replace(hour=0, minute=0)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    end_iso = (
        future.replace(hour=23, minute=59)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    response = client.get(
        f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}"
    )
    assert response.status_code == 200

    data = response.json()
    print(data["recommendation_plans"])
    plans = data["recommendation_plans"][0]["plans"]
    assert len(plans) == 2
    scheduled_for_set = set(p["scheduled_for"] for p in plans)

    def _z(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    expected1 = future.replace(hour=10, minute=0, second=0, microsecond=0)
    expected2 = future.replace(hour=12, minute=0, second=0, microsecond=0)
    assert _z(expected1) in scheduled_for_set
    assert _z(expected2) in scheduled_for_set


def test_recommendation_plans_invalid_datetime_format(client):
    """Router should return 422 when start_time or end_time is not ISO‐8601."""
    resp = client.get(
        "/recommendation_plans/?start_time=not-a-date&end_time=2025-07-29T10:00:00"
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "Invalid datetime format"}


def test_recommendation_plan_skips_when_mode_not_test(client, monkeypatch):
    """Branch: MODE ≠ 'test' → service returns [] and router responds with empty list."""
    monkeypatch.setenv("MODE", "prod")  # trigger the early‑return branch

    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # minimal DB setup so the router will attempt generation
    with Session(engine) as session:
        session.add(User(id=user_id, created_at=now, is_deleted=False))
        rec = Recommendation(
            content_id="RC_PROD",
            content_type="recommendation",
            missions=["MC_PROD"],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        session.add_all([rec, Mission(mission_id="MC_PROD")])
        session.commit()
        session.add(
            SelectedContent(
                id="4b4a4f44-701a-1ab0-afe0-05f100444444",
                user_id=user_id,
                contents=[
                    {"id": "RC_PROD", "type": "recommendation", "mission_id": "MC_PROD"}
                ],
                created_at=now,
            )
        )
        session.commit()

    start_iso = now.replace(tzinfo=None).isoformat()
    end_iso = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    resp = client.get(
        f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}"
    )

    assert resp.status_code == 200
    assert resp.json() == {"recommendation_plans": []}


def test_no_duplicate_per_plan_and_content_across_hourly_windows(
    client, setup_selected_content
):
    # First run: current hour
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    s1 = now.replace(tzinfo=None).isoformat()
    e1 = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    r1 = client.get(f"/recommendation_plans/?start_time={s1}&end_time={e1}")
    assert r1.status_code == 200

    # Second run: next hour
    s2 = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    e2 = (now + timedelta(hours=2)).replace(tzinfo=None).isoformat()
    r2 = client.get(f"/recommendation_plans/?start_time={s2}&end_time={e2}")
    assert r2.status_code == 200

    # Assert uniqueness per (plan_id, recommendation_id)
    with Session(engine) as s:
        rows = s.exec(
            select(RecommendationPlan.plan_id, RecommendationPlan.recommendation_id)
        ).all()
        assert len(rows) == len(
            set(rows)
        ), "Duplicate (plan_id, recommendation_id) found"


def test_branch_no_new_schedules_returns_empty(client, monkeypatch):
    """
    Force generate_random_schedule() to return [] so the service returns [] via the
    `if not new_schedules: return []` branch.
    """
    monkeypatch.setenv("BETWEEN_NOTIFICATION_COOLDOWN", "180")
    # deterministic: zero candidates
    monkeypatch.setattr(
        "app.services.recommendation_plan_service.generate_random_schedule",
        lambda start, end: [],
    )

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        user_id = uuid.uuid4()
        session.add(User(id=user_id, created_at=now, is_deleted=False))
        session.add_all(
            [
                Mission(mission_id="MS_NO_SCHED"),
                Recommendation(
                    content_id="RC_NO_SCHED",
                    content_type="recommendation",
                    missions=["MS_NO_SCHED"],
                    objective=[],
                    hapa=[],
                    comb=[],
                    intervention_type=[],
                ),
            ]
        )
        session.commit()
        session.add(
            SelectedContent(
                id=uuid.uuid4(),
                user_id=user_id,
                contents=[
                    {
                        "id": "RC_NO_SCHED",
                        "type": "recommendation",
                        "mission_id": "MS_NO_SCHED",
                    }
                ],
                created_at=now,
            )
        )
        session.commit()

    start_iso = _iso(now)
    end_iso = _iso(now + timedelta(hours=1))
    r = client.get(f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}")
    assert r.status_code == 200
    assert r.json() == {"recommendation_plans": []}


# ---------------------------- Branch: if not content_items -> [] ----------------------------


def test_branch_no_content_items_returns_empty(client, monkeypatch):
    """
    Make sure the plan already contains this recommendation (same plan_id) so
    the per-(plan_id, content_id) filter empties content_items ->
    `if not content_items: return []`.

    Also ensure the existing plan is OUTSIDE the requested window so router
    doesn't short-circuit by returning existing rows.
    """
    # return one candidate time; should be irrelevant because content_items will be empty
    monkeypatch.setattr(
        "app.services.recommendation_plan_service.generate_random_schedule",
        lambda start, end: [start + timedelta(minutes=10)],
    )

    now = datetime.now(timezone.utc)
    plan_id = uuid.uuid4()
    with Session(engine) as session:
        user_id = uuid.uuid4()
        session.add(User(id=user_id, created_at=now, is_deleted=False))
        mission = Mission(mission_id="MS_ALREADY_IN_PLAN")
        rec = Recommendation(
            content_id="RC_ALREADY_IN_PLAN",
            content_type="recommendation",
            missions=["MS_ALREADY_IN_PLAN"],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        session.add_all([mission, rec])
        session.commit()

        # Selected content ties the plan to the content
        session.add(
            SelectedContent(
                id=plan_id,
                user_id=user_id,
                contents=[
                    {
                        "id": "RC_ALREADY_IN_PLAN",
                        "type": "recommendation",
                        "mission_id": "MS_ALREADY_IN_PLAN",
                    }
                ],
                created_at=now,
            )
        )
        session.commit()

        # Insert an existing recommendation instance for this (plan_id, rec_id) yesterday
        session.add(
            RecommendationPlan(
                user_id=user_id,
                plan_id=plan_id,
                recommendation_id=rec.id,
                mission_id=mission.id,
                scheduled_for=now - timedelta(days=1),
            )
        )
        session.commit()

    # Ask for a window in the future (no existing rows in range), so service is invoked
    start_iso = _iso(now + timedelta(hours=1))
    end_iso = _iso(now + timedelta(hours=2))
    r = client.get(f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}")
    # Generation should return [], since content_items is empty
    assert r.status_code == 200
    assert r.json() == {"recommendation_plans": []}


# ---------------------------- Branch: if rec_uuid is None -> continue ----------------------------


def test_branch_rec_uuid_none_skips(client, monkeypatch):
    """
    SelectedContent has a content_id that doesn't exist in Recommendation table.
    The generator hits `rec_uuid is None: continue` and ends up producing no plans.
    """
    monkeypatch.setattr(
        "app.services.recommendation_plan_service.generate_random_schedule",
        lambda start, end: [
            start + timedelta(minutes=5),
            start + timedelta(minutes=30),
        ],
    )

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        user_id = uuid.uuid4()
        session.add(User(id=user_id, created_at=now, is_deleted=False))
        # Mission exists; Recommendation for the content does NOT
        session.add(Mission(mission_id="MS_MISSING_REC"))
        session.commit()

        session.add(
            SelectedContent(
                id=uuid.uuid4(),
                user_id=user_id,
                contents=[
                    {
                        "id": "RC_DOES_NOT_EXIST",
                        "type": "recommendation",
                        "mission_id": "MS_MISSING_REC",
                    }
                ],
                created_at=now,
            )
        )
        session.commit()

    start_iso = _iso(now)
    end_iso = _iso(now + timedelta(hours=1))
    r = client.get(f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}")
    # Service creates nothing (continues on missing rec), router returns empty
    assert r.status_code == 200
    assert r.json() == {"recommendation_plans": []}


# --------------- Explicit: per (plan_id, content_id) stays unique across retries ---------------


def test_per_plan_content_unique_across_same_and_future_windows(client, monkeypatch):
    """
    1) Plan once in the next hour for specific (plan_id, content_id) -> 1 row
    2) Try the same hour again -> should not create an extra row (router returns existing)
    3) Try two hours later -> generator runs, but content_items is empty because that
       recommendation already exists for this plan_id -> no new rows
    """
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    plan_id = uuid.uuid4()
    user_id = uuid.uuid4()

    # deterministic schedule:
    # - for any window, propose the first minute inside it
    def fake_schedule(start, end):
        return [start + timedelta(minutes=5)]

    monkeypatch.setattr(
        "app.services.recommendation_plan_service.generate_random_schedule",
        fake_schedule,
    )

    with Session(engine) as session:
        session.add(User(id=user_id, created_at=now, is_deleted=False))
        mission = Mission(mission_id="MS_LOCK")
        rec = Recommendation(
            content_id="RC_LOCK",
            content_type="recommendation",
            missions=["MS_LOCK"],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        session.add_all([mission, rec])
        session.commit()

        session.add(
            SelectedContent(
                id=plan_id,
                user_id=user_id,
                contents=[
                    {"id": "RC_LOCK", "type": "recommendation", "mission_id": "MS_LOCK"}
                ],
                created_at=now,
                mission_start_time=now,
                mission_end_time=now + timedelta(hours=3),
            )
        )
        session.commit()

    # (1) First run for next hour
    s1 = _iso(now)
    e1 = _iso(now + timedelta(hours=1))
    r1 = client.get(f"/recommendation_plans/?start_time={s1}&end_time={e1}")
    assert r1.status_code == 200
    d1 = r1.json()
    # Exactly one user with one plan referencing RC_LOCK
    assert len(d1["recommendation_plans"]) == 1
    ublock = d1["recommendation_plans"][0]
    assert str(user_id) == ublock["user_id"]
    assert any(p["content_id"] == "RC_LOCK" for p in ublock["plans"])
    assert len(ublock["plans"]) == 1

    # Check DB row count = 1
    with Session(engine) as session:
        cnt1 = session.exec(
            select(RecommendationPlan).where(
                RecommendationPlan.user_id == user_id,
                RecommendationPlan.plan_id == plan_id,
            )
        ).all()
        assert len(cnt1) == 1

    # (2) Try same window again -> should not add a second row (router returns existing)
    r2 = client.get(f"/recommendation_plans/?start_time={s1}&end_time={e1}")
    assert r2.status_code == 200
    d2 = r2.json()
    assert len(d2["recommendation_plans"]) == 1
    assert len(d2["recommendation_plans"][0]["plans"]) == 1
    with Session(engine) as session:
        cnt2 = session.exec(
            select(RecommendationPlan).where(
                RecommendationPlan.user_id == user_id,
                RecommendationPlan.plan_id == plan_id,
            )
        ).all()
        assert len(cnt2) == 1  # still one row

    # (3) Try *two hours later* -> generator runs (no existing in that window),
    #     but per-(plan_id, content_id) filter empties content_items -> no new rows
    s3 = _iso(now + timedelta(hours=2))
    e3 = _iso(now + timedelta(hours=3))
    r3 = client.get(f"/recommendation_plans/?start_time={s3}&end_time={e3}")
    assert r3.status_code == 200
    # Could be empty (no generation happened) or unrelated user buckets,
    # but there must be no *additional* rows for our plan_id/user_id.
    with Session(engine) as session:
        cnt3 = session.exec(
            select(RecommendationPlan).where(
                RecommendationPlan.user_id == user_id,
                RecommendationPlan.plan_id == plan_id,
            )
        ).all()
        assert len(cnt3) == 1  # no duplicates added


def test_past_window_returns_empty(client):
    now = datetime.now(timezone.utc)
    # Entirely in the past
    start_iso = (now - timedelta(days=2)).replace(tzinfo=None).isoformat()
    end_iso = (now - timedelta(days=1)).replace(tzinfo=None).isoformat()

    r = client.get(f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}")
    assert r.status_code == 200
    assert r.json() == {"recommendation_plans": []}


def test_window_end_le_window_start_skips_generation(client):
    now = datetime.now(timezone.utc)

    # Create a user + SelectedContent whose mission window includes *now*
    user_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(User(id=user_id, created_at=now, is_deleted=False))
        s.commit()
        s.add(
            SelectedContent(
                id=uuid.uuid4(),
                user_id=user_id,
                contents=[{"id": "ANY", "type": "recommendation", "mission_id": "MID"}],
                mission_start_time=(now - timedelta(days=1)),
                mission_end_time=(now + timedelta(days=1)),
                is_deleted=False,
            )
        )
        s.commit()

    # Ask for a degenerate future window with start == end (and > now)
    start_dt = now + timedelta(minutes=5)
    end_dt = start_dt  # makes window_end == window_start
    start_iso = start_dt.replace(tzinfo=None).isoformat()
    end_iso = end_dt.replace(tzinfo=None).isoformat()

    r = client.get(f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}")
    assert r.status_code == 200
    # Router should skip this SelectedContent (intersection empty) and return nothing
    assert r.json() == {"recommendation_plans": []}


def test_empty_contents_skips_generation(client):
    now = datetime.now(timezone.utc)

    user_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(User(id=user_id, created_at=now, is_deleted=False))
        s.commit()
        s.add(
            SelectedContent(
                id=uuid.uuid4(),
                user_id=user_id,
                contents=[],  # ← empty → content_missions == {}
                mission_start_time=(now - timedelta(days=1)),
                mission_end_time=(now + timedelta(days=1)),
                is_deleted=False,
            )
        )
        s.commit()

    # Future window (so we don't hit the past-window early return)
    start_iso = (now + timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    end_iso = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()

    r = client.get(f"/recommendation_plans/?start_time={start_iso}&end_time={end_iso}")
    assert r.status_code == 200
    assert r.json() == {"recommendation_plans": []}


def test_generate_plan_returns_empty_when_end_le_now(client, monkeypatch):
    # Ensure service logic runs (not short-circuited by MODE)
    monkeypatch.setenv("MODE", "test")

    now = datetime.now(timezone.utc)
    user_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    with Session(engine) as s:
        # Minimal DB setup: user, mission, recommendation
        s.add(User(id=user_id, created_at=now, is_deleted=False))
        mission = Mission(mission_id="MS_PAST")
        rec = Recommendation(
            content_id="RC_PAST",
            content_type="recommendation",
            missions=["MS_PAST"],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        s.add_all([mission, rec])
        s.commit()

        # Entirely past window => end_time <= now
        start_time = now - timedelta(hours=2)
        end_time = now - timedelta(hours=1)

        plans = generate_recommendation_plan(
            user_id=user_id,
            content_missions={"RC_PAST": "MS_PAST"},
            plan_id=plan_id,
            start_time=start_time,
            end_time=end_time,
            db=s,
        )

    assert plans == []
