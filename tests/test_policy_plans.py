"""Exercise current-epoch planning against ordered selected content."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.dependencies import get_db
from app.main import app
from app.models.artemis import MissionScheduledStep, MissionSelection
from app.models.event import Event
from app.models.mission import Mission
from app.models.policy_decision import PolicyDecision
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.routers import recommendation_plans as router
from app.schemas.recommendation_plan import (
    RecommendationPlanItem,
    RecommendationPlanList,
    RecommendationPlanResponse,
)
from app.services import recommendation_plan_service as service
from app.services.artemis_update_service import process_artemis_message
from app.services.mission_schedule_service import (
    record_scheduled_snapshot,
    record_scheduled_step,
)
from app.services.rl_policy import EpochDecision


@pytest.fixture
def database():
    """Create an isolated PostgreSQL schema for each planner test."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL tests")
    schema = f"test_policy_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    SQLModel.metadata.create_all(engine)

    def override_db():
        with Session(engine) as db:
            yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client, engine
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def epoch(monkeypatch):
    """Keep the tested decision epoch fixed across HTTP calls."""
    value = datetime(2026, 9, 28, 10, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
        timezone.utc
    )
    monkeypatch.setattr(router, "_next_epoch", lambda *_: value)
    return value


@pytest.fixture
def force_send(monkeypatch):
    """Return a feasible deterministic send action for database integration tests."""

    def decide(policy, items, *args, **kwargs):
        return EpochDecision(
            1,
            np.zeros(len(policy.metadata["fields"])),
            (False, True),
            policy.version,
        )

    monkeypatch.setattr(service, "decide_epoch", decide)


def _content(content_id, kind="recommendation", mission_process_id=10):
    return {
        "id": content_id,
        "type": kind,
        "mission_id": "NM1",
        "mission_process_id": mission_process_id,
    }


def _post_queue(client, epoch, user_id, plan_id, contents):
    payload = {
        str(user_id): {
            "plan_id": str(plan_id),
            "contents": contents,
            "mission_start_time": (epoch - timedelta(hours=1)).isoformat(),
            "mission_end_time": (epoch + timedelta(days=1)).isoformat(),
        }
    }
    response = client.post("/selected_contents/", json=payload)
    assert response.status_code == 200


def _query(client, epoch, endpoint="recommendation_plans"):
    start = (epoch - timedelta(minutes=1)).isoformat()
    end = (epoch + timedelta(minutes=1)).isoformat()
    return client.get(f"/{endpoint}/", params={"start_time": start, "end_time": end})


def test_current_epoch_send_is_recorded_once(database, epoch, force_send):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1"), _content("R2")])
    first = _query(client, epoch)
    second = _query(client, epoch)
    assert first.status_code == second.status_code == 200
    plans = first.json()["recommendation_plans"][0]["plans"]
    assert [item["content_id"] for item in plans] == ["R1"]
    assert second.json()["recommendation_plans"][0]["plans"] == plans
    assert plans[0]["policy_version"].endswith("-omi2x2-v1")
    with Session(engine) as db:
        assert len(db.exec(select(RecommendationPlan)).all()) == 1
        decisions = db.exec(select(PolicyDecision)).all()
        assert len(decisions) == 1
        assert decisions[0].action == 1
        assert decisions[0].safe_mask == [False, True]


def test_skip_is_recorded_once_without_a_plan(database, epoch, monkeypatch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1")])

    def skip(policy, items, *args, **kwargs):
        return EpochDecision(
            0,
            np.zeros(len(policy.metadata["fields"])),
            (True, False),
            policy.version,
        )

    monkeypatch.setattr(service, "decide_epoch", skip)
    assert _query(client, epoch).json() == {"recommendation_plans": []}
    assert _query(client, epoch).json() == {"recommendation_plans": []}
    with Session(engine) as db:
        assert not db.exec(select(RecommendationPlan)).all()
        assert len(db.exec(select(PolicyDecision)).all()) == 1


def test_queue_reorder_keeps_sent_item_consumed(
    database, epoch, force_send, monkeypatch
):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1"), _content("R2")])
    assert _query(client, epoch).status_code == 200
    _post_queue(client, epoch, user_id, plan_id, [_content("R2"), _content("R1")])
    later = epoch + timedelta(minutes=60)
    monkeypatch.setattr(router, "_next_epoch", lambda *_: later)
    assert _query(client, later).status_code == 200
    with Session(engine) as db:
        rows = db.exec(
            select(RecommendationPlan).order_by(RecommendationPlan.scheduled_for)
        ).all()
        assert len(rows) == 2
        assert [row.occurrence_index for row in rows] == [0, 0]
        assert rows[0].occurrence_key != rows[1].occurrence_key


def test_duplicate_content_ids_are_separate_occurrences(
    database, epoch, force_send, monkeypatch
):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1"), _content("R1")])
    assert _query(client, epoch).status_code == 200
    later = epoch + timedelta(minutes=60)
    monkeypatch.setattr(router, "_next_epoch", lambda *_: later)
    assert _query(client, later).status_code == 200
    with Session(engine) as db:
        plans = db.exec(select(RecommendationPlan)).all()
        assert len(plans) == 2
        assert len({row.occurrence_key for row in plans}) == 2


def test_resource_shares_recommendation_class_cap(database, force_send, monkeypatch):
    client, engine = database
    epoch = datetime(2026, 9, 28, 10, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
        timezone.utc
    )
    monkeypatch.setattr(router, "_next_epoch", lambda *_: epoch)
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(
        client,
        epoch,
        user_id,
        plan_id,
        [_content("R1"), _content("S1", "resource"), _content("S2", "resource")],
    )
    assert _query(client, epoch).status_code == 200
    later = epoch + timedelta(minutes=60)
    monkeypatch.setattr(router, "_next_epoch", lambda *_: later)
    assert _query(client, later).status_code == 200
    monkeypatch.setattr(router, "_next_epoch", lambda *_: later + timedelta(minutes=60))
    assert _query(client, later + timedelta(minutes=60)).json() == {
        "recommendation_plans": []
    }
    with Session(engine) as db:
        assert len(db.exec(select(RecommendationPlan)).all()) == 2


def test_resource_and_reminder_satisfy_daily_class_minimums(
    database, epoch, monkeypatch
):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(
        client,
        epoch,
        user_id,
        plan_id,
        [_content("S1", "resource"), _content("M1", "reminder")],
    )
    observed = []

    def decide(policy, items, *args, **kwargs):
        observed.append((tuple(item.kind for item in items), kwargs["floor_day"]))
        return EpochDecision(
            1,
            np.zeros(len(policy.metadata["fields"])),
            (False, True),
            policy.version,
        )

    monkeypatch.setattr(service, "decide_epoch", decide)
    assert _query(client, epoch).status_code == 200
    later = epoch + timedelta(minutes=60)
    monkeypatch.setattr(router, "_next_epoch", lambda *_: later)
    assert _query(client, later).status_code == 200
    assert observed == [(("recommendation", "reminder"), 2), (("reminder",), 1)]
    with Session(engine) as db:
        assert {plan.content_type for plan in db.exec(select(RecommendationPlan))} == {
            "resource",
            "reminder",
        }


def test_queue_order_cannot_hide_a_supplied_class_minimum(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(
        client,
        epoch,
        user_id,
        plan_id,
        [
            _content("R1"),
            _content("R2"),
            _content("R3"),
            _content("M1", "reminder"),
        ],
    )
    response = _query(client, epoch)
    assert response.status_code == 409
    assert "content class minimum" in response.json()["detail"]
    with Session(engine) as db:
        assert not db.exec(select(PolicyDecision)).all()


def test_remaining_queue_continues_next_day(database, force_send, monkeypatch):
    client, engine = database
    epoch = datetime(2026, 9, 28, 10, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
        timezone.utc
    )
    monkeypatch.setattr(router, "_next_epoch", lambda *_: epoch)
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(
        client,
        epoch,
        user_id,
        plan_id,
        [_content("R1"), _content("S1", "resource"), _content("R2")],
    )
    assert _query(client, epoch).status_code == 200
    same_day = epoch + timedelta(minutes=60)
    monkeypatch.setattr(router, "_next_epoch", lambda *_: same_day)
    assert _query(client, same_day).status_code == 200
    next_day = epoch + timedelta(hours=23)
    monkeypatch.setattr(router, "_next_epoch", lambda *_: next_day)
    response = _query(client, next_day)
    assert response.status_code == 200
    assert response.json()["recommendation_plans"][0]["plans"][0]["content_id"] == "R2"
    with Session(engine) as db:
        assert len(db.exec(select(RecommendationPlan)).all()) == 3


def test_reminder_without_artemis_step_uses_mission_end(database, epoch, force_send):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("M1", "reminder")])
    response = _query(client, epoch)
    assert response.status_code == 200
    plan = response.json()["recommendation_plans"][0]["plans"][0]
    assert datetime.fromisoformat(plan["deadline"]) == epoch + timedelta(days=1)
    with Session(engine) as db:
        assert len(db.exec(select(PolicyDecision)).all()) == 1


def test_reminder_without_artemis_or_mission_times_fails_closed(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("M1", "reminder")])
    with Session(engine) as db:
        selected = db.get(SelectedContent, plan_id)
        selected.mission_start_time = None
        selected.mission_end_time = None
        db.add(selected)
        db.commit()
    response = _query(client, epoch)
    assert response.status_code == 409
    assert "no Artemis deadline or mission end time" in response.json()["detail"]
    with Session(engine) as db:
        assert not db.exec(select(PolicyDecision)).all()


def test_resolved_reminder_deadline_is_recorded(
    database, epoch, force_send, monkeypatch
):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    deadline = epoch + timedelta(minutes=60)
    monkeypatch.setattr(service, "resolve_reminder_deadline", lambda *_: deadline)
    _post_queue(client, epoch, user_id, plan_id, [_content("M1", "reminder")])
    response = _query(client, epoch)
    assert response.status_code == 200
    item = response.json()["recommendation_plans"][0]["plans"][0]
    assert item["content_type"] == "reminder"
    assert datetime.fromisoformat(item["deadline"]) == deadline
    with Session(engine) as db:
        assert db.exec(select(RecommendationPlan)).one().deadline is not None


def test_artemis_task_step_supplies_updated_reminder_deadline(
    database, epoch, force_send
):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    reminder = _content("M1", "reminder")
    reminder["mission_process_id"] = 999
    reminder["task_step_id"] = "unrelated-step"
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    with Session(engine) as db:
        record_scheduled_step(
            db, user_id, 10, "NM1", "step-1", epoch + timedelta(minutes=60)
        )
        db.commit()
        record_scheduled_step(
            db, user_id, 10, "NM1", "step-1", epoch + timedelta(minutes=75)
        )
        db.commit()
    response = _query(client, epoch)
    assert response.status_code == 200
    deadline = response.json()["recommendation_plans"][0]["plans"][0]["deadline"]
    assert datetime.fromisoformat(deadline) == epoch + timedelta(minutes=75)


def test_multiple_task_steps_use_last_end_on_decision_day(database, epoch, force_send):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("M1", "reminder")])
    with Session(engine) as db:
        record_scheduled_step(
            db, user_id, 10, "NM1", "step-1", epoch + timedelta(minutes=60)
        )
        record_scheduled_step(
            db, user_id, 10, "NM1", "step-2", epoch + timedelta(minutes=75)
        )
        db.commit()
    response = _query(client, epoch)
    assert response.status_code == 200
    deadline = response.json()["recommendation_plans"][0]["plans"][0]["deadline"]
    assert datetime.fromisoformat(deadline) == epoch + timedelta(minutes=75)


def test_single_task_step_needs_no_step_id_in_queue(database, epoch, force_send):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("M1", "reminder")])
    with Session(engine) as db:
        record_scheduled_step(
            db, user_id, 10, "NM1", "step-1", epoch + timedelta(minutes=60)
        )
        db.commit()
    response = _query(client, epoch)
    assert response.status_code == 200
    assert (
        response.json()["recommendation_plans"][0]["plans"][0]["content_type"]
        == "reminder"
    )


def test_reminder_uses_todays_artemis_end_date_without_process_in_queue(
    database, force_send, monkeypatch
):
    client, engine = database
    zone = ZoneInfo("Europe/Berlin")
    local_epoch = datetime(2026, 9, 28, 10, tzinfo=zone)
    epoch = local_epoch.astimezone(timezone.utc)
    monkeypatch.setattr(router, "_next_epoch", lambda *_: epoch)
    user_id, plan_id = uuid4(), uuid4()
    reminder = _content("M1", "reminder")
    reminder.pop("mission_process_id")
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    with Session(engine) as db:
        process_artemis_message(
            {
                "mission_selected": {
                    "user_uuid": str(user_id),
                    "mission": "NM1",
                    "created_at": (epoch - timedelta(hours=2)).isoformat(),
                    "process_id": 7,
                    "mission_process_id": 25,
                }
            },
            "selected-25",
            db,
        )
        process_artemis_message(
            {
                "task_step_scheduled": {
                    "user_uuid": str(user_id),
                    "created_at": (epoch - timedelta(hours=1)).isoformat(),
                    "process_id": 7,
                    "mission_process_id": 25,
                    "properties": {
                        "mission_id": "NM1",
                        "total_steps": 2,
                        "scheduled_for": [
                            {
                                "startDate": "2026-09-28T09:00:00",
                                "endDate": "2026-09-28T11:00:00",
                            },
                            {
                                "startDate": "2026-09-29T09:00:00",
                                "endDate": "2026-09-30T09:00:00",
                            },
                        ],
                    },
                }
            },
            "schedule-25",
            db,
        )
    response = _query(client, epoch)
    assert response.status_code == 200
    plan = response.json()["recommendation_plans"][0]["plans"][0]
    assert datetime.fromisoformat(plan["deadline"]) == datetime(
        2026, 9, 28, 11, tzinfo=zone
    ).astimezone(timezone.utc)
    with Session(engine) as db:
        process_artemis_message(
            {
                "mission_accomplished": {
                    "user_uuid": str(user_id),
                    "mission_id": "NM1",
                    "created_at": (epoch + timedelta(minutes=1)).isoformat(),
                    "process_id": 7,
                    "mission_process_id": 25,
                }
            },
            "mission-complete-25",
            db,
        )
        selected = db.get(SelectedContent, plan_id)
        assert service.resolve_reminder_deadline(
            db, selected, reminder, epoch + timedelta(minutes=2)
        ) == datetime(2026, 9, 28, 11, tzinfo=zone).astimezone(timezone.utc)


def test_other_user_and_mission_schedules_do_not_change_reminder_deadline(
    database, epoch
):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    reminder = {"id": "M1", "type": "reminder", "mission_id": "NM1"}
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    with Session(engine) as db:
        expected = epoch + timedelta(hours=2)
        record_scheduled_step(db, user_id, 10, "NM1", "1", expected)
        record_scheduled_step(db, uuid4(), 10, "NM1", "1", epoch + timedelta(days=7))
        record_scheduled_step(db, user_id, 11, "NM2", "1", epoch + timedelta(days=8))
        db.flush()
        selected = db.get(SelectedContent, plan_id)
        assert (
            service.resolve_reminder_deadline(db, selected, reminder, epoch) == expected
        )


def test_reminder_uses_active_snapshot_last_end_on_decision_day(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    reminder = {"id": "M1", "type": "reminder", "mission_id": "NM1"}
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    with Session(engine) as db:
        start = epoch.isoformat()
        first_end = (epoch + timedelta(hours=2)).isoformat()
        last_end = (epoch + timedelta(hours=4)).isoformat()
        record_scheduled_snapshot(
            db, user_id, 10, "NM1", [(start, last_end), (start, first_end)], "old"
        )
        selected = db.get(SelectedContent, plan_id)
        assert service.resolve_reminder_deadline(db, selected, reminder, epoch) == (
            epoch + timedelta(hours=4)
        )
        replacement_end = (epoch + timedelta(hours=3)).isoformat()
        record_scheduled_snapshot(
            db, user_id, 10, "NM1", [(start, replacement_end)], "new"
        )
        assert service.resolve_reminder_deadline(db, selected, reminder, epoch) == (
            epoch + timedelta(hours=3)
        )
        assert (
            sum(step.is_active for step in db.exec(select(MissionScheduledStep))) == 1
        )


def test_other_day_step_uses_global_mission_end(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    reminder = {"id": "M1", "type": "reminder", "mission_id": "NM1"}
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    with Session(engine) as db:
        record_scheduled_step(db, user_id, 10, "NM1", "1", epoch + timedelta(days=2))
        selected = db.get(SelectedContent, plan_id)
        assert service.resolve_reminder_deadline(db, selected, reminder, epoch) == (
            epoch + timedelta(days=1)
        )


def test_global_mission_end_and_seven_day_null_end_limit(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    reminder = {"id": "M1", "type": "reminder", "mission_id": "NM1"}
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    with Session(engine) as db:
        selected = db.get(SelectedContent, plan_id)
        assert service.resolve_reminder_deadline(db, selected, reminder, epoch) == (
            epoch + timedelta(days=1)
        )
        selected.mission_end_time = None
        selected.mission_start_time = epoch - timedelta(days=6)
        assert service.resolve_reminder_deadline(db, selected, reminder, epoch) == (
            epoch + timedelta(days=1)
        )
        selected.mission_start_time = epoch - timedelta(days=7, minutes=1)
        assert service.resolve_reminder_deadline(db, selected, reminder, epoch) == (
            epoch - timedelta(minutes=1)
        )
        selected.mission_start_time = None
        assert service.resolve_reminder_deadline(db, selected, reminder, epoch) is None


def test_todays_past_step_cannot_use_later_global_end(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("M1", "reminder")])
    with Session(engine) as db:
        record_scheduled_step(
            db, user_id, 10, "NM1", "1", epoch - timedelta(minutes=15)
        )
        db.commit()
    response = _query(client, epoch)
    assert response.status_code == 409
    assert "No safe action completes" in response.json()["detail"]


def test_past_global_mission_end_is_infeasible(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("M1", "reminder")])
    with Session(engine) as db:
        selected = db.get(SelectedContent, plan_id)
        selected.mission_end_time = epoch - timedelta(minutes=15)
        db.add(selected)
        db.commit()
    response = _query(client, epoch)
    assert response.status_code == 409
    assert "No safe action completes" in response.json()["detail"]


def test_reminder_rejects_ambiguous_artemis_end_date_on_decision_day(database):
    client, engine = database
    epoch = datetime(2026, 10, 25, 1, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
        timezone.utc
    )
    user_id, plan_id = uuid4(), uuid4()
    reminder = {"id": "M1", "type": "reminder", "mission_id": "NM1"}
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    with Session(engine) as db:
        record_scheduled_step(db, user_id, 10, "NM1", "1", epoch + timedelta(hours=2))
        record_scheduled_step(
            db,
            user_id,
            10,
            "NM1",
            "2",
            None,
            start_date_raw="2026-10-25T01:30:00",
            end_date_raw="2026-10-25T02:30:00",
        )
        selected = db.get(SelectedContent, plan_id)
        assert service.resolve_reminder_deadline(db, selected, reminder, epoch) is None
        assert service.resolve_reminder_deadline(
            db, selected, reminder, epoch - timedelta(days=1)
        ) == epoch + timedelta(days=1)


def test_artemis_source_time_rejects_ambiguous_local_clock(monkeypatch):
    monkeypatch.setenv("ARTEMIS_SCHEDULE_TIMEZONE", "Europe/Berlin")
    assert service._source_time("2026-10-25T02:30:00") is None
    assert service._source_time("2026-03-29T02:30:00") is None
    assert service._source_time("2026-09-28T09:00:00") == datetime(
        2026, 9, 28, 7, tzinfo=timezone.utc
    )
    assert service._source_time("2026-09-28T09:00:00+02:00") == datetime(
        2026, 9, 28, 7, tzinfo=timezone.utc
    )


def test_reminder_uses_latest_end_across_mission_processes(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    reminder = _content("M1", "reminder")
    reminder.pop("mission_process_id")
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    with Session(engine) as db:
        for process_id, minutes in ((10, 60), (11, 90)):
            record_scheduled_step(
                db, user_id, process_id, "NM1", "1", epoch + timedelta(minutes=minutes)
            )
        db.flush()
        selected = db.get(SelectedContent, plan_id)
        assert service.resolve_reminder_deadline(
            db, selected, reminder, epoch
        ) == epoch + timedelta(minutes=90)


def test_reminder_finds_only_scheduled_process_without_selection(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    reminder = _content("M1", "reminder")
    reminder.pop("mission_process_id")
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    deadline = epoch + timedelta(hours=1)
    with Session(engine) as db:
        record_scheduled_step(db, user_id, 10, "NM1", "1", deadline)
        db.flush()
        selected = db.get(SelectedContent, plan_id)
        assert (
            service.resolve_reminder_deadline(db, selected, reminder, epoch) == deadline
        )


def test_reminder_without_mission_or_usable_interval_returns_none(database, epoch):
    _, engine = database
    user_id = uuid4()
    selected = SelectedContent(user_id=user_id, contents=[])
    content = {"id": "M1", "type": "reminder", "mission_id": "MISSING"}
    with Session(engine) as db:
        record_scheduled_step(
            db, user_id, 10, "MISSING", "1", epoch + timedelta(hours=1)
        )
        db.flush()
        assert service.resolve_reminder_deadline(
            db, selected, content, epoch
        ) == epoch + timedelta(hours=1)
        step = db.exec(select(MissionScheduledStep)).one()
        step.scheduled_for = None
        db.flush()
        assert service.resolve_reminder_deadline(db, selected, content, epoch) is None


def test_reminder_deadline_does_not_depend_on_mission_selection(database, epoch):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    reminder = _content("M1", "reminder")
    reminder.pop("mission_process_id")
    _post_queue(client, epoch, user_id, plan_id, [reminder])
    with Session(engine) as db:
        mission = db.exec(select(Mission).where(Mission.mission_id == "NM1")).one()
        for process_id, minutes in ((10, 60), (11, 90)):
            db.add(
                MissionSelection(
                    user_id=user_id,
                    mission_id=mission.id,
                    process_id=process_id,
                    mission_process_id=process_id,
                    selected_at=epoch - timedelta(hours=1),
                )
            )
            record_scheduled_step(
                db, user_id, process_id, "NM1", "1", epoch + timedelta(minutes=minutes)
            )
        db.flush()
        selected = db.get(SelectedContent, plan_id)
        assert service.resolve_reminder_deadline(
            db, selected, reminder, epoch
        ) == epoch + timedelta(minutes=90)


def test_reminder_deadline_before_decision_is_infeasible(database, monkeypatch):
    client, _ = database
    epoch = datetime(2026, 9, 28, 10, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
        timezone.utc
    )
    monkeypatch.setattr(router, "_next_epoch", lambda *_: epoch)
    monkeypatch.setattr(
        service,
        "resolve_reminder_deadline",
        lambda *_: epoch - timedelta(days=1),
    )
    _post_queue(client, epoch, uuid4(), uuid4(), [_content("M1", "reminder")])
    response = _query(client, epoch)
    assert response.status_code == 409
    assert "No safe action completes" in response.json()["detail"]


def test_three_due_reminders_exceed_daily_class_budget(
    database, force_send, monkeypatch
):
    client, engine = database
    epoch = datetime(2026, 9, 28, 10, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
        timezone.utc
    )
    monkeypatch.setattr(
        service, "resolve_reminder_deadline", lambda *_: epoch + timedelta(hours=6)
    )
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(
        client,
        epoch,
        user_id,
        plan_id,
        [_content(f"M{index}", "reminder") for index in range(3)],
    )
    monkeypatch.setattr(router, "_next_epoch", lambda *_: epoch)
    response = _query(client, epoch)
    assert response.status_code == 409
    assert "class limit" in response.json()["detail"]
    with Session(engine) as db:
        assert not db.exec(select(RecommendationPlan)).all()


def test_future_third_reminder_can_wait_for_next_day(database, force_send, monkeypatch):
    client, engine = database
    epoch = datetime(2026, 9, 28, 10, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
        timezone.utc
    )
    monkeypatch.setattr(router, "_next_epoch", lambda *_: epoch)
    monkeypatch.setattr(
        service,
        "resolve_reminder_deadline",
        lambda *_: epoch + timedelta(days=2),
    )
    _post_queue(
        client,
        epoch,
        uuid4(),
        uuid4(),
        [_content(f"M{index}", "reminder") for index in range(3)],
    )
    assert _query(client, epoch).status_code == 200
    with Session(engine) as db:
        assert len(db.exec(select(RecommendationPlan)).all()) == 1


def test_completed_queue_has_no_new_occurrence(
    database, epoch, force_send, monkeypatch
):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1")])
    assert _query(client, epoch).status_code == 200
    later = epoch + timedelta(minutes=15)
    monkeypatch.setattr(router, "_next_epoch", lambda *_: later)
    assert _query(client, later).json() == {"recommendation_plans": []}
    with Session(engine) as db:
        assert len(db.exec(select(PolicyDecision)).all()) == 1


def test_missing_catalog_and_mission_roll_back_policy_decision(
    database, epoch, force_send
):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1")])
    with Session(engine) as db:
        recommendation = db.exec(
            select(Recommendation).where(Recommendation.content_id == "R1")
        ).one()
        recommendation.is_deleted = True
        db.commit()
    with pytest.raises(ValueError, match="no catalog record"):
        _query(client, epoch)
    with Session(engine) as db:
        assert not db.exec(select(PolicyDecision)).all()
        recommendation = db.exec(select(Recommendation)).one()
        recommendation.is_deleted = False
        db.exec(select(Mission)).one().is_deleted = True
        db.commit()
    with pytest.raises(ValueError, match="no mission record"):
        _query(client, epoch)


def test_planner_returns_none_for_deleted_selection_or_missing_user(database, epoch):
    _, engine = database
    with Session(engine) as db:
        selected = SelectedContent(user_id=uuid4(), contents=[])
        selected.is_deleted = True
        assert service.generate_recommendation_plan(selected, epoch, db) is None
        selected.is_deleted = False
        assert service.generate_recommendation_plan(selected, epoch, db) is None


def test_occurrences_filter_delivery_date_and_waking_deadline():
    contents = [
        {
            "id": "R1",
            "type": "resource",
            "mission_id": "NM1",
            "delivery_date": "2026-09-29",
        },
        {"id": "R2", "type": "recommendation", "mission_id": "NM1"},
    ]
    assert [
        row[2]["id"]
        for row in service._occurrences(contents, datetime(2026, 9, 28).date())
    ] == ["R2"]
    assert service._waking_deadline(7, 22) == 1439


def test_plan_router_rejects_bad_windows_and_accepts_grid_epoch():
    with pytest.raises(HTTPException) as invalid:
        router._window("bad", "2026-09-28T10:00:00Z")
    assert invalid.value.status_code == 422
    with pytest.raises(HTTPException) as reversed_window:
        router._window("2026-09-29T10:00:00Z", "2026-09-28T10:00:00Z")
    assert reversed_window.value.status_code == 422
    exact = datetime(2026, 9, 28, 10, tzinfo=timezone.utc)
    assert router._next_epoch(exact) == exact


def test_plan_router_uses_latest_selection_for_each_user(database, epoch):
    client, engine = database
    user_id = uuid4()
    older, newer = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, older, [_content("R1")])
    _post_queue(client, epoch, user_id, newer, [_content("R2")])
    with Session(engine) as db:
        first = db.get(SelectedContent, older)
        second = db.get(SelectedContent, newer)
        first.created_at = epoch - timedelta(days=1)
        second.created_at = epoch
        db.commit()
        assert [row.id for row in router._latest_selections(db)] == [newer]
        first.created_at = epoch + timedelta(minutes=1)
        second.created_at = epoch - timedelta(days=1)
        db.commit()
        assert [row.id for row in router._latest_selections(db)] == [older]


def test_plan_router_rolls_back_unexpected_decision_error(monkeypatch):
    selected = SelectedContent(user_id=uuid4(), contents=[])
    calls = []

    class FakeDatabase:
        def execute(self, statement, parameters):
            calls.append("lock")

        def exec(self, statement):
            return SimpleNamespace(one=lambda: selected)

        def rollback(self):
            calls.append("rollback")

    monkeypatch.setattr(router, "_latest_selections", lambda db: [selected])

    def failure(*args):
        raise OSError("database unavailable")

    monkeypatch.setattr(router, "generate_recommendation_plan", failure)
    with pytest.raises(OSError, match="database unavailable"):
        router._decide_current_epoch(FakeDatabase(), datetime.now(timezone.utc))
    assert calls == ["lock", "rollback"]


def test_plan_router_reads_legacy_row_from_selected_content():
    user_id, plan_id = uuid4(), uuid4()
    selected = SelectedContent(
        id=plan_id,
        user_id=user_id,
        contents=[{"id": "R1", "type": "resource", "mission_id": "NM1"}],
    )
    recommendation = Recommendation(content_id="R1", content_type="recommendation")
    plan = RecommendationPlan(
        user_id=user_id,
        plan_id=plan_id,
        recommendation_id=recommendation.id,
        scheduled_for=datetime(2026, 9, 28, tzinfo=timezone.utc),
        occurrence_index=0,
    )

    class Rows:
        def exec(self, statement):
            return SimpleNamespace(all=lambda: [(plan, recommendation, None, selected)])

    result = router._read_plans(
        Rows(),
        datetime(2026, 9, 28, tzinfo=timezone.utc),
        datetime(2026, 9, 29, tzinfo=timezone.utc),
    )
    item = result.recommendation_plans[0].plans[0]
    assert item.mission_id == "NM1"
    assert item.content_type == "resource"
    plan.occurrence_index = None
    fallback = (
        router._read_plans(
            Rows(),
            datetime(2026, 9, 28, tzinfo=timezone.utc),
            datetime(2026, 9, 29, tzinfo=timezone.utc),
        )
        .recommendation_plans[0]
        .plans[0]
    )
    assert fallback.content_type == "recommendation"
    assert fallback.mission_id == "UNKNOWN"


def test_plan_query_outside_current_epoch_only_reads_committed_rows(database):
    client, engine = database
    past = datetime(2025, 1, 1, tzinfo=timezone.utc)
    response = client.get(
        "/recommendation_plans/",
        params={"start_time": past.isoformat(), "end_time": past.isoformat()},
    )
    assert response.status_code == 200
    assert response.json() == {"recommendation_plans": []}
    with Session(engine) as db:
        assert not db.exec(select(PolicyDecision)).all()


@pytest.mark.parametrize(
    ("mission_id", "content_type", "detail"),
    [
        ("UNKNOWN", "resource", "Missing mission_id"),
        ("NM1", None, "Missing content_type"),
    ],
)
def test_notification_router_rejects_incomplete_legacy_row(
    monkeypatch, mission_id, content_type, detail
):
    result = RecommendationPlanList(
        recommendation_plans=[
            RecommendationPlanResponse(
                user_id=uuid4(),
                plans=[
                    RecommendationPlanItem(
                        content_id="R1",
                        content_type=content_type,
                        scheduled_for=datetime(2026, 9, 28, tzinfo=timezone.utc),
                        plan_id=uuid4(),
                        mission_id=mission_id,
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr(router, "get_recommendation_plans", lambda *args: result)
    with pytest.raises(HTTPException, match=detail):
        router.get_notification_plans("start", "end", None)


def test_scheduled_step_requires_timezone_and_stable_mission(database, epoch):
    _, engine = database
    user_id = uuid4()
    with Session(engine) as db:
        with pytest.raises(ValueError, match="timezone offset"):
            record_scheduled_step(
                db, user_id, 10, "NM1", "step-1", epoch.replace(tzinfo=None)
            )
        record_scheduled_step(db, user_id, 10, "NM1", "step-1", epoch)
        with pytest.raises(ValueError, match="another mission"):
            record_scheduled_step(db, user_id, 10, "NM2", "step-1", epoch)
        with pytest.raises(ValueError, match="source interval"):
            record_scheduled_step(db, user_id, 10, "NM1", "step-2", None)
        with pytest.raises(ValueError, match="Mission code and step ID"):
            record_scheduled_step(db, user_id, 10, "", "step-2", epoch)


def test_repeated_task_interval_keeps_resolved_deadline(database, epoch):
    _, engine = database
    user_id = uuid4()
    with Session(engine) as db:
        deadline = epoch + timedelta(hours=2)
        record_scheduled_step(
            db,
            user_id,
            10,
            "NM1",
            "1",
            deadline,
            start_date_raw="2026-09-28T09:00:00",
            end_date_raw="2026-09-29T09:00:00",
        )
        step = record_scheduled_step(
            db,
            user_id,
            10,
            "NM1",
            "1",
            None,
            start_date_raw="2026-09-28T09:00:00",
            end_date_raw="2026-09-29T09:00:00",
        )
        assert step.scheduled_for == deadline


def test_disabled_user_is_excluded(database, epoch, force_send):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1")])
    with Session(engine) as db:
        user = db.get(User, user_id)
        user.is_deleted = True
        db.commit()
    response = _query(client, epoch)
    assert response.status_code == 200
    assert response.json() == {"recommendation_plans": []}


def test_notification_plan_keeps_original_resource_type(database, epoch, force_send):
    client, _ = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("S1", "resource")])
    response = _query(client, epoch, "notification_plans")
    assert response.status_code == 200
    assert response.json()[str(user_id)]["contents"][0]["type"] == "resource"


def test_queue_update_needs_no_mission_window(database, epoch, force_send):
    client, _ = database
    user_id, plan_id = uuid4(), uuid4()
    response = client.post(
        "/selected_contents/",
        json={
            str(user_id): {
                "plan_id": str(plan_id),
                "contents": [_content("R1")],
            }
        },
    )
    assert response.status_code == 200
    assert _query(client, epoch).status_code == 200


def test_recommendation_has_no_mission_time_deadline(database, epoch, force_send):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1")])
    with Session(engine) as db:
        selected = db.get(SelectedContent, plan_id)
        selected.mission_end_time = epoch - timedelta(minutes=15)
        db.commit()
    response = _query(client, epoch)
    assert response.status_code == 200
    assert response.json()["recommendation_plans"][0]["plans"][0]["deadline"] is None


def test_concurrent_requests_keep_one_decision(database, epoch, force_send):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1")])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _query(client, epoch), range(2)))
    assert all(response.status_code == 200 for response in responses)
    with Session(engine) as db:
        assert len(db.exec(select(PolicyDecision)).all()) == 1
        assert len(db.exec(select(RecommendationPlan)).all()) == 1


def _sent_event(user_id, content_id, when):
    return Event(
        user_id=user_id,
        event_name="notification_sent",
        event_start_time=when.replace(tzinfo=None),
        process_id=15,
        created_at=when,
        characteristics={
            "content_id": content_id,
            "content_type": "recommendation",
            "mission_id": "NM1",
            "mission_process_id": 10,
        },
    )


def test_confirmed_send_consumes_a_queued_occurrence(database, epoch, force_send):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1"), _content("R2")])
    with Session(engine) as db:
        db.add(_sent_event(user_id, "R1", epoch - timedelta(minutes=15)))
        db.commit()
    response = _query(client, epoch)
    assert response.status_code == 200
    assert response.json()["recommendation_plans"][0]["plans"][0]["content_id"] == "R2"


def test_disjoint_confirmed_send_uses_the_class_budget(
    database, force_send, monkeypatch
):
    client, engine = database
    epoch = datetime(2026, 9, 28, 10, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
        timezone.utc
    )
    monkeypatch.setattr(router, "_next_epoch", lambda *_: epoch)
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1"), _content("R2")])
    assert _query(client, epoch).status_code == 200
    with Session(engine) as db:
        db.add(_sent_event(user_id, "R3", epoch - timedelta(minutes=15)))
        db.commit()
    later = epoch + timedelta(minutes=60)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(router, "_next_epoch", lambda *_: later)
        response = _query(client, later)
    assert response.status_code == 200
    assert response.json() == {"recommendation_plans": []}


def test_confirmation_of_planned_send_is_counted_once(database, epoch, force_send):
    client, engine = database
    user_id, plan_id = uuid4(), uuid4()
    _post_queue(client, epoch, user_id, plan_id, [_content("R1"), _content("R2")])
    assert _query(client, epoch).status_code == 200
    with Session(engine) as db:
        db.add(_sent_event(user_id, "R1", epoch))
        db.commit()
    later = epoch + timedelta(minutes=60)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(router, "_next_epoch", lambda *_: later)
        response = _query(client, later)
    assert response.status_code == 200
