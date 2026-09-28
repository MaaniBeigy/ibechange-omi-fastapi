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
from app.routers.emotional_wellbeing import _extract_sleep_event

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


SAMPLE = [
    {
        "user_uuid": "f93fc9a8-2fa1-4f5f-b15f-83bf9da4ce8a",
        "pillar_id": "emotional_wellbeing",
        "start_date": "2025-08-22T21:49:26.000000Z",
        "end_date": "2025-08-25T11:43:29.000000Z",
        "data": {
            "singleindicator": {
                "vars": [
                    {
                        "name": "average_breath",
                        "type": "float",
                        "unit": "breath_per_minute",
                    },
                    {"name": "average_heart_rate", "type": "float", "unit": "bpm"},
                    {"name": "average_hrv", "type": "float", "unit": "ms"},
                    {"name": "lowest_heart_rate", "type": "int", "unit": "bpm"},
                    {"name": "min_hrv", "type": "int", "unit": "ms"},
                    {"name": "max_hrv", "type": "int", "unit": "ms"},
                    {"name": "movement_30_seconds", "type": "string", "unit": ""},
                    {"name": "number_restless_periods", "type": "int", "unit": ""},
                    {"name": "efficiency", "type": "float", "unit": "bpm"},
                    {"name": "awake_time", "type": "int", "unit": "minutes"},
                    {"name": "deep_sleep_duration", "type": "int", "unit": "minutes"},
                    {"name": "light_sleep_duration", "type": "int", "unit": "minutes"},
                    {"name": "rem_sleep_duration", "type": "int", "unit": "minutes"},
                    {"name": "sleep_phase_5_min", "type": "string", "unit": ""},
                    {"name": "bedtime_start", "type": "string", "unit": ""},
                    {"name": "bedtime_end", "type": "string", "unit": ""},
                    {"name": "latency", "type": "int", "unit": "minutes"},
                    {"name": "time_in_bed", "type": "int", "unit": "minutes"},
                    {"name": "total_sleep_duration", "type": "int", "unit": "minutes"},
                    {"name": "type_sleep", "type": "string", "unit": ""},
                ],
                "rows": [
                    [
                        19.5,
                        75.5,
                        25.54,
                        69.0,
                        10.0,
                        53.0,
                        "1222111..."
                        * 10,  # shortened for brevity; keep full string in real test
                        126,
                        93,
                        34,
                        104,
                        223,
                        126,
                        "4221111...",  # shortened; keep full string in real test
                        "2025-08-22T23:49:26+02:00",
                        "2025-08-23T07:56:39+02:00",
                        4,
                        487,
                        454,
                        "long_sleep",
                    ]
                ],
            },
            "multipleindicators": {
                "vars": [
                    {
                        "name": "average_breath",
                        "type": "float",
                        "unit": "breath_per_minute",
                    },
                    {"name": "average_heart_rate", "type": "float", "unit": "bpm"},
                    {"name": "average_hrv", "type": "float", "unit": "ms"},
                    {"name": "lowest_heart_rate", "type": "int", "unit": "bpm"},
                    {"name": "min_hrv", "type": "int", "unit": "ms"},
                    {"name": "max_hrv", "type": "int", "unit": "ms"},
                    {"name": "movement_30_seconds", "type": "string", "unit": ""},
                    {"name": "number_restless_periods", "type": "int", "unit": ""},
                    {"name": "efficiency", "type": "float", "unit": "bpm"},
                    {"name": "awake_time", "type": "int", "unit": "minutes"},
                    {"name": "deep_sleep_duration", "type": "int", "unit": "minutes"},
                    {"name": "light_sleep_duration", "type": "int", "unit": "minutes"},
                    {"name": "rem_sleep_duration", "type": "int", "unit": "minutes"},
                    {"name": "sleep_phase_5_min", "type": "string", "unit": ""},
                    {"name": "bedtime_start", "type": "string", "unit": ""},
                    {"name": "bedtime_end", "type": "string", "unit": ""},
                    {"name": "latency", "type": "int", "unit": "minutes"},
                    {"name": "time_in_bed", "type": "int", "unit": "minutes"},
                    {"name": "total_sleep_duration", "type": "int", "unit": "minutes"},
                    {"name": "type_sleep", "type": "string", "unit": ""},
                ],
                "rows": [
                    [
                        20.875,
                        "N/A",
                        "N/A",
                        "N/A",
                        "N/A",
                        "N/A",
                        "132333221121211111111111111111122223212321111111111221",
                        12,
                        41,
                        16,
                        1,
                        10,
                        0,
                        "444224",
                        "2025-08-23T15:29:56+02:00",
                        "2025-08-23T15:56:57+02:00",
                        18,
                        27,
                        11,
                        "sleep",
                    ]
                ],
            },
        },
    }
]

# --- helpers -----------------------------------------------------------------


def _canon(v) -> str:
    if isinstance(v, str):
        obj = json.loads(v)
    else:
        obj = v
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _get_all_events(session: Session):
    return session.exec(select(Event)).all()


def _find_event(session: Session, name: str, start_iso: str, end_iso: str):
    start_dt = _to_naive_utc(datetime.fromisoformat(start_iso))
    end_dt = _to_naive_utc(datetime.fromisoformat(end_iso))
    return session.exec(
        select(Event).where(
            Event.event_name == name,
            Event.event_start_time == start_dt,
            Event.event_end_time == end_dt,
        )
    ).first()


# --- tests -------------------------------------------------------------------


def test_ingest_creates_user_and_events_and_returns_inserted(client):
    r = client.post("/emotional_wellbeing/", json=SAMPLE)
    assert r.status_code == 200

    body = r.json()
    assert "inserted" in body
    assert isinstance(body["inserted"], list)
    assert len(body["inserted"]) == 2

    # expected UTC-Z times converted from +02:00 inputs
    expected = {
        ("sleep", "2025-08-23T13:29:56Z", "2025-08-23T13:56:57Z"),
        ("long_sleep", "2025-08-22T21:49:26Z", "2025-08-23T05:56:39Z"),
    }
    got = {(i["event_name"], i["start_time"], i["end_time"]) for i in body["inserted"]}
    assert got == expected

    # DB checks
    with Session(engine) as s:
        uid = uuid.UUID(SAMPLE[0]["user_uuid"])
        user = s.exec(select(User).where(User.id == uid)).first()
        assert user is not None

        evs = _get_all_events(s)
        assert len(evs) == 2  # 1 from single, 1 from multiple

        # multipleindicators event exists with those times (stored as UTC-naive)
        ev = _find_event(
            s,
            "sleep",
            "2025-08-23T15:29:56+02:00",
            "2025-08-23T15:56:57+02:00",
        )
        assert ev is not None
        assert ev.event_start_time.tzinfo is None
        assert ev.event_end_time.tzinfo is None

        # singleindicator event exists with those times (stored as UTC-naive)
        ev2 = _find_event(
            s,
            "long_sleep",
            "2025-08-22T23:49:26+02:00",
            "2025-08-23T07:56:39+02:00",
        )
        assert ev2 is not None
        assert ev2.event_start_time.tzinfo is None
        assert ev2.event_end_time.tzinfo is None


def test_sleep_event_needs_type_and_both_times():
    assert (
        _extract_sleep_event(
            {"type_sleep": "", "bedtime_start": "time", "bedtime_end": "time"}
        )
        is None
    )
    assert (
        _extract_sleep_event(
            {"type_sleep": "sleep", "bedtime_start": "", "bedtime_end": "time"}
        )
        is None
    )
    assert (
        _extract_sleep_event({"type_sleep": "sleep", "bedtime_start": "time"}) is None
    )


def test_unrelated_or_empty_emotional_records_insert_nothing(client):
    valid_user = SAMPLE[0]["user_uuid"]
    records = [
        {
            "user_uuid": valid_user,
            "pillar_id": "another_pillar",
            "start_date": SAMPLE[0]["start_date"],
            "end_date": SAMPLE[0]["end_date"],
            "data": {},
        },
        {
            "user_uuid": valid_user,
            "pillar_id": "emotional_wellbeing",
            "start_date": SAMPLE[0]["start_date"],
            "end_date": SAMPLE[0]["end_date"],
            "data": {},
        },
        {
            "user_uuid": valid_user,
            "pillar_id": "emotional_wellbeing",
            "start_date": SAMPLE[0]["start_date"],
            "end_date": SAMPLE[0]["end_date"],
            "data": {
                "singleindicator": {
                    "vars": [{"name": "type_sleep"}],
                    "rows": [[""]],
                },
                "multipleindicators": {
                    "vars": [{"name": "type_sleep"}],
                    "rows": [[""]],
                },
            },
        },
    ]
    response = client.post("/emotional_wellbeing/", json=records)
    assert response.status_code == 200
    assert response.json() == {"inserted": []}


def test_idempotent_same_payload(client):
    r1 = client.post("/emotional_wellbeing/", json=SAMPLE)
    assert r1.status_code == 200
    r2 = client.post("/emotional_wellbeing/", json=SAMPLE)
    assert r2.status_code == 200
    # second call should return empty "inserted" or duplicates filtered;
    # we assert DB count remains 2
    with Session(engine) as s:
        evs = _get_all_events(s)
        assert len(evs) == 2


def test_idempotent_different_characteristics_in_multiple_inserts_new_row(client):
    r1 = client.post("/emotional_wellbeing/", json=SAMPLE)
    assert r1.status_code == 200

    # change multipleindicators: efficiency 41 -> 42 (index 8)
    modified = json.loads(json.dumps(SAMPLE))
    modified[0]["data"]["multipleindicators"]["rows"][0][8] = 42

    r2 = client.post("/emotional_wellbeing/", json=modified)
    assert r2.status_code == 200

    body = r2.json()
    assert "inserted" in body
    # one new "sleep" with same times but different characteristics
    assert len(body["inserted"]) == 1
    assert body["inserted"][0]["event_name"] == "sleep"
    assert (body["inserted"][0]["start_time"], body["inserted"][0]["end_time"]) == (
        "2025-08-23T13:29:56Z",
        "2025-08-23T13:56:57Z",
    )

    with Session(engine) as s:
        # total events now 3
        evs = _get_all_events(s)
        assert len(evs) == 3

        # both sleep events share same times
        matches = s.exec(
            select(Event).where(
                Event.event_name == "sleep",
                Event.event_start_time
                == _to_naive_utc(datetime.fromisoformat("2025-08-23T15:29:56+02:00")),
                Event.event_end_time
                == _to_naive_utc(datetime.fromisoformat("2025-08-23T15:56:57+02:00")),
            )
        ).all()
        assert len(matches) == 2


def test_idempotent_different_characteristics_in_single_inserts_new_row(client):
    r1 = client.post("/emotional_wellbeing/", json=SAMPLE)
    assert r1.status_code == 200

    # change singleindicator: awake_time (index 9) 34 -> 35
    modified = json.loads(json.dumps(SAMPLE))
    modified[0]["data"]["singleindicator"]["rows"][0][9] = 35

    r2 = client.post("/emotional_wellbeing/", json=modified)
    assert r2.status_code == 200

    body = r2.json()
    assert "inserted" in body
    # one new "long_sleep" with same times but different characteristics
    assert any(i["event_name"] == "long_sleep" for i in body["inserted"])

    with Session(engine) as s:
        evs = _get_all_events(s)
        assert len(evs) == 3

        matches = s.exec(
            select(Event).where(
                Event.event_name == "long_sleep",
                Event.event_start_time
                == _to_naive_utc(datetime.fromisoformat("2025-08-22T23:49:26+02:00")),
                Event.event_end_time
                == _to_naive_utc(datetime.fromisoformat("2025-08-23T07:56:39+02:00")),
            )
        ).all()
        assert len(matches) == 2


def test_within_request_dedupe_multiple(client):
    payload = json.loads(json.dumps(SAMPLE))
    row = payload[0]["data"]["multipleindicators"]["rows"][0]
    payload[0]["data"]["multipleindicators"]["rows"].append(
        list(row)
    )  # exact duplicate

    r = client.post("/emotional_wellbeing/", json=payload)
    assert r.status_code == 200

    body = r.json()
    # only one multiple event should be returned in "inserted"
    names = [i["event_name"] for i in body.get("inserted", [])]
    assert names.count("sleep") == 1

    with Session(engine) as s:
        # 1 single + 1 multiple
        evs = _get_all_events(s)
        assert len(evs) == 2


def test_skips_invalid_user_uuid(client):
    bad = json.loads(json.dumps(SAMPLE))
    bad[0]["user_uuid"] = "not-a-uuid"

    r = client.post("/emotional_wellbeing/", json=bad)
    assert r.status_code == 200

    with Session(engine) as s:
        assert s.exec(select(User)).all() == []
        assert _get_all_events(s) == []
