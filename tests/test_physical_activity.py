import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy.sql import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.dependencies import get_db
from app.main import app
from app.models.event import Event
from app.models.user import User

load_dotenv()

DATABASE_URL = f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@db:5432/{os.getenv('POSTGRES_DB')}_test"
engine = create_engine(DATABASE_URL)


def override_get_db():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="function")
def client():
    # Ensure the test database exists
    base_db_url = (
        f"postgresql://{os.getenv('POSTGRES_USER')}:"
        f"{os.getenv('POSTGRES_PASSWORD')}@db:5432/postgres"
    )
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


SAMPLE = [
    {
        "user_uuid": "f93fc9a8-2fa1-4f5f-b15f-83bf9da4ce8a",
        "pillar_id": "physical_activity",
        "start_date": "2025-08-03T02:00:00.000000Z",
        "end_date": "2025-08-06T22:23:25.000000Z",
        "data": {
            "singleindicator": {
                "vars": [
                    {"name": "class_5_min", "type": "string", "unit": ""},
                    {"name": "score", "type": "int", "unit": ""},
                    {"name": "steps", "type": "int", "unit": ""},
                    {"name": "active_calories", "type": "int", "unit": "kcal"},
                    {"name": "average_met_minutes", "type": "float", "unit": "METs"},
                    {
                        "name": "equivalent_walking_distance",
                        "type": "int",
                        "unit": "meters",
                    },
                    {
                        "name": "high_activity_met_minutes",
                        "type": "int",
                        "unit": "MET * minutes",
                    },
                    {"name": "high_activity_time", "type": "int", "unit": "minutes"},
                    {
                        "name": "medium_activity_met_minutes",
                        "type": "int",
                        "unit": "MET * minutes",
                    },
                    {"name": "medium_activity_time", "type": "int", "unit": "minutes"},
                    {
                        "name": "low_activity_met_minutes",
                        "type": "int",
                        "unit": "MET * minutes",
                    },
                    {"name": "low_activity_time", "type": "int", "unit": "minutes"},
                    {
                        "name": "sedentary_met_minutes",
                        "type": "int",
                        "unit": "MET * minutes",
                    },
                    {"name": "sedentary_time", "type": "int", "unit": "minutes"},
                    {"name": "non_wear_time", "type": "int", "unit": "minutes"},
                ],
                "rows": [
                    [
                        "111111111111111111111111111111111111111111122222223323333322234333333333333333333332333333333333232333332333333322222222332233334444233333303333433333433222222222222234334333333333333233333332332223334332322333332233333322333333221111111111111111111111111111111111111111111113332111111111",
                        82,
                        11197,
                        592,
                        1.6875,
                        14437,
                        0,
                        0,
                        187,
                        57,
                        421,
                        566,
                        8,
                        293,
                        9,
                    ]
                ],
            },
            "multipleindicator": {
                "vars": [
                    {"name": "start time", "type": "string", "unit": ""},
                    {"name": "end time", "type": "string", "unit": ""},
                    {"name": "type activity", "type": "string", "unit": ""},
                    {"name": "average MET", "type": "float", "unit": "METs"},
                    {"name": "duration", "type": "int", "unit": "minutes"},
                    {"name": "mean heart rate", "type": "float", "unit": "bpm"},
                    {"name": "id", "type": "string", "unit": ""},
                ],
                "rows": [
                    ["12:23:00", "12:37:00", "medium", 2.68, 15, None, "2025-08-03_activity_1"],
                    ["14:44:00", "15:00:00", "medium", 5.12, 17, None, "2025-08-03_activity_2"],
                    ["15:17:00", "15:31:00", "medium", 2.96, 15, None, "2025-08-03_activity_3"],
                    ["15:49:00", "16:10:00", "medium", 3.17, 22, None, "2025-08-03_activity_4"],
                    ["16:30:00", "16:45:00", "medium", 3.01, 16, None, "2025-08-03_activity_5"],
                    ["18:11:00", "18:29:00", "medium", 2.89, 19, None, "2025-08-03_activity_6"],
                    # duplicates (same start/end & characteristics as earlier rows):
                    ["12:23:00", "12:37:00", "medium", 2.68, 15, None, "2025-08-03_activity_7"],
                    ["14:44:00", "15:00:00", "medium", 5.12, 17, None, "2025-08-03_activity_8"],
                    ["15:17:00", "15:31:00", "medium", 2.96, 15, None, "2025-08-03_activity_9"],
                    ["15:49:00", "16:10:00", "medium", 3.17, 22, None, "2025-08-03_activity_10"],
                    ["16:30:00", "16:45:00", "medium", 3.01, 16, None, "2025-08-03_activity_11"],
                    ["18:11:00", "18:29:00", "medium", 2.89, 19, None, "2025-08-03_activity_12"],
                ],
            },
        },
    }
]


def _canon(v) -> str:
    """Make characteristics comparable regardless of dict/str."""
    if isinstance(v, str):
        obj = json.loads(v)
    else:
        obj = v
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _to_naive_utc(dt: datetime) -> datetime:
    """Convert an ISO string with Z/offset to UTC-naive datetime for Event comparisons."""
    if dt.tzinfo is None:
        return dt  # already naive
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _get_all_events(session: Session):
    return session.exec(select(Event)).all()


def _find_event(session: Session, name: str, start_iso: str, end_iso: str):
    start_dt = _to_naive_utc(datetime.fromisoformat(start_iso.replace("Z", "+00:00")))
    end_dt = _to_naive_utc(datetime.fromisoformat(end_iso.replace("Z", "+00:00")))
    return session.exec(
        select(Event).where(
            Event.event_name == name,
            Event.event_start_time == start_dt,
            Event.event_end_time == end_dt,
        )
    ).first()


def test_ingest_creates_user_and_events(client):
    r = client.post("/physical_activity/", json=SAMPLE)
    assert r.status_code == 200
    with Session(engine) as s:
        # user created
        uid = uuid.UUID(SAMPLE[0]["user_uuid"])
        user = s.exec(select(User).where(User.id == uid)).first()
        assert user is not None

        evs = _get_all_events(s)
        # Dedupe within request: 1 single + 6 unique multiples = 7
        assert len(evs) == 7

        # Check one multiple row mapping + timestamps + name
        ev = _find_event(
            s,
            "medium_intensity_physical_activity",
            "2025-08-03T12:23:00.000000Z",
            "2025-08-03T12:37:00.000000Z",
        )
        assert ev is not None
        characteristics = ev.characteristics if isinstance(ev.characteristics, dict) else json.loads(ev.characteristics)
        assert characteristics == {"average MET": 2.68, "duration": 15, "mean heart rate": None}

        # Check the summary event (singleindicator) is stored with naive datetimes
        summary = s.exec(
            select(Event).where(Event.event_name == "physical_activity_summary")
        ).first()
        assert summary is not None
        assert summary.event_start_time.tzinfo is None
        assert summary.event_end_time.tzinfo is None


def test_idempotent_same_payload(client):
    # First insert
    r1 = client.post("/physical_activity/", json=SAMPLE)
    assert r1.status_code == 200
    # Same payload again → no new rows
    r2 = client.post("/physical_activity/", json=SAMPLE)
    assert r2.status_code == 200

    with Session(engine) as s:
        evs = _get_all_events(s)
        assert len(evs) == 7  # still 7 due to idempotency + within-request dedupe


def test_idempotent_different_characteristics_in_multiple_inserts_new_row(client):
    # Insert once
    r1 = client.post("/physical_activity/", json=SAMPLE)
    assert r1.status_code == 200

    # Change a characteristic of the FIRST multiple row only (duration 15 → 16)
    modified = json.loads(json.dumps(SAMPLE))
    modified[0]["data"]["multipleindicator"]["rows"][0][4] = 16  # duration field index

    r2 = client.post("/physical_activity/", json=modified)
    assert r2.status_code == 200

    with Session(engine) as s:
        # Expect +1 new event (only that one row differs)
        evs = _get_all_events(s)
        assert len(evs) == 8

        # Ensure the new event has duration 16
        ev = _find_event(
            s,
            "medium_intensity_physical_activity",
            "2025-08-03T12:23:00.000000Z",
            "2025-08-03T12:37:00.000000Z",
        )
        assert ev is not None
        characteristics = ev.characteristics if isinstance(ev.characteristics, dict) else json.loads(ev.characteristics)
        assert characteristics["duration"] in (15, 16)  # both versions present

        # Count how many events match the same name+times (should be 2 now)
        start_dt = _to_naive_utc(datetime.fromisoformat("2025-08-03T12:23:00.000000+00:00"))
        end_dt = _to_naive_utc(datetime.fromisoformat("2025-08-03T12:37:00.000000+00:00"))
        matches = s.exec(
            select(Event).where(
                Event.event_name == "medium_intensity_physical_activity",
                Event.event_start_time == start_dt,
                Event.event_end_time == end_dt,
            )
        ).all()
        assert len(matches) == 2
        # and their characteristics differ
        cset = {_canon(m.characteristics) for m in matches}
        assert len(cset) == 2


def test_idempotent_different_characteristics_in_single_inserts_new_summary(client):
    # Insert once
    r1 = client.post("/physical_activity/", json=SAMPLE)
    assert r1.status_code == 200

    # Modify singleindicator first-row "steps" (11197 → 11198)
    modified = json.loads(json.dumps(SAMPLE))
    modified[0]["data"]["singleindicator"]["rows"][0][2] = 11198  # steps position

    r2 = client.post("/physical_activity/", json=modified)
    assert r2.status_code == 200

    with Session(engine) as s:
        # Expect +1 new summary (so 8 total)
        evs = _get_all_events(s)
        assert len(evs) == 8

        # Count summaries with same times
        summaries = s.exec(
            select(Event).where(Event.event_name == "physical_activity_summary")
        ).all()
        assert len(summaries) == 2
        # Verify characteristics differ
        cset = {_canon(e.characteristics) for e in summaries}
        assert len(cset) == 2


def test_singleindicator_uses_first_row_only(client):
    # Copy sample; add a second (different) row to singleindicator
    modified = json.loads(json.dumps(SAMPLE))
    si_rows = modified[0]["data"]["singleindicator"]["rows"]
    # append a different second row
    second = si_rows[0].copy()
    second[2] = 99999  # steps changed
    si_rows.append(second)

    r = client.post("/physical_activity/", json=modified)
    assert r.status_code == 200

    with Session(engine) as s:
        # Still 1 summary + 6 unique multiple = 7
        evs = _get_all_events(s)
        assert len(evs) == 7
        summaries = s.exec(
            select(Event).where(Event.event_name == "physical_activity_summary")
        ).all()
        assert len(summaries) == 1  # only the FIRST singleindicator row used


def test_skips_invalid_user_uuid(client):
    bad = json.loads(json.dumps(SAMPLE))
    bad[0]["user_uuid"] = "not-a-uuid"

    r = client.post("/physical_activity/", json=bad)
    assert r.status_code == 200

    with Session(engine) as s:
        users = s.exec(select(User)).all()
        assert users == []
        evs = _get_all_events(s)
        assert evs == []
