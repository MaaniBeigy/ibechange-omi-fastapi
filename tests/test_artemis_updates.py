import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.artemis import (
    ArtemisFailure,
    ArtemisInbox,
    MissionScheduledStep,
    MissionSelection,
    PsychologicalAssessment,
)
from app.models.escalation_level import EscalationLevel
from app.models.event import Event
from app.models.mission import Mission
from app.models.policy_decision import PolicyDecision
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent
from app.models.user import HealthHabitAssessment, MissionContent, User
from app.schemas.update import parse_artemis_message
from app.services import artemis_update_service as update_service
from app.services.artemis_update_service import (
    process_artemis_message,
    record_invalid_message,
)
from app.services.rl_observations import observed_history

USER_ID = "3a6f5cbc-3e91-4a7a-8b41-7ce6bbd3372f"
TIMESTAMP = "2026-07-22T08:17:52.905150Z"


@pytest.fixture
def database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL tests")
    schema = f"test_artemis_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def messages():
    return {
        "new_user": {
            "uuid": USER_ID,
            "username": "test@example.org",
            "email": "test@example.org",
            "enabled": True,
            "attributes": {"wearable": "no", "level": 0},
        },
        "disabled_user": {"user_uuid": USER_ID, "date_disabled": TIMESTAMP},
        "health_habit_assessments": {
            "user_uuid": USER_ID,
            "pillar_id": "NO",
            "score": 34.9,
            "colour": "orange",
            "trend": 0,
            "subpillars": [{"score": 1, "colour": "green", "name": "fruit"}],
            "assessment_timestamp": TIMESTAMP,
        },
        "psychological_assessments": {
            "user_uuid": USER_ID,
            "pillar_id": "NO",
            "barriers": ["physical_opportunity"],
            "subpillar": "perceived_stress",
            "assessment_timestamp": TIMESTAMP,
            "answer_tasklog": {},
        },
        "escalation_level": {
            "user_uuid": USER_ID,
            "pillar_id": "SO",
            "level": 1,
            "update_timestamp": TIMESTAMP,
        },
        "new_pillar": {"user_uuid": USER_ID, "pillars": "NO"},
        "mission_selected": {
            "user_uuid": USER_ID,
            "mission": "NM1",
            "created_at": TIMESTAMP,
            "process_id": 12343,
            "mission_process_id": 209425,
        },
        "notification_sent": {
            "user_uuid": USER_ID,
            "created_at": TIMESTAMP,
            "process_id": 12343,
            "mission_process_id": 209425,
            "properties": {
                "content_id": "NRs25",
                "mission_id": "NM1",
                "content_type": "resource",
            },
        },
        "notification_opened": {
            "user_uuid": USER_ID,
            "created_at": TIMESTAMP,
            "process_id": 12343,
            "mission_process_id": 209425,
            "properties": {
                "content_id": "NRs25",
                "mission_id": "NM1",
                "content_type": "resource",
            },
        },
    }


@pytest.mark.parametrize("event_name", list(messages()))
def test_artemis_schema_accepts_each_document(event_name):
    name, parsed = parse_artemis_message({event_name: messages()[event_name]})
    assert name == event_name
    assert parsed is not None


def test_artemis_schema_accepts_unattributed_psychology_and_rejects_multiple_events():
    document = messages()["psychological_assessments"].copy()
    document.pop("user_uuid")
    _, parsed = parse_artemis_message({"psychological_assessments": document})
    assert parsed.user_uuid is None
    with pytest.raises(ValueError):
        parse_artemis_message(messages())


def captured_messages():
    """Read JSON bodies embedded in the supplied Artemis diagnostic transcript."""
    transcript = (
        Path(__file__).resolve().parents[1] / "docs/artemis_example.json"
    ).read_text()
    decoder = json.JSONDecoder()
    offset = 0
    while (marker := transcript.find("body (json) :", offset)) != -1:
        start = transcript.index("{", marker)
        body, length = decoder.raw_decode(transcript[start:])
        offset = start + length
        yield body


def test_captured_artemis_contract_parses_every_message():
    counts = Counter(parse_artemis_message(body)[0] for body in captured_messages())
    assert counts == {
        "new_user": 11,
        "new_pillar": 15,
        "health_habit_assessments": 7,
        "psychological_assessments": 4,
        "mission_selected": 2,
        "task_step_scheduled": 12,
        "task_step_done": 9,
        "mission_accomplished": 2,
    }


def test_captured_artemis_messages_ingest_and_replay(database):
    bodies = list(captured_messages())
    with Session(database) as db:
        for index, body in enumerate(bodies):
            assert process_artemis_message(body, f"capture-{index}", db)
        assert len(db.exec(select(ArtemisInbox)).all()) == len(bodies)
        steps = db.exec(select(MissionScheduledStep)).all()
        assert len(steps) == 5
        assert {step.step_id for step in steps} == {"1", "2", "3", "4", "5"}
        assert all(step.start_date_raw and step.end_date_raw for step in steps)
        assert all(step.scheduled_for is None for step in steps)
        psychology = db.exec(select(PsychologicalAssessment)).all()
        assert len(psychology) == 4
        assert all(row.user_id is None and row.raw_payload for row in psychology)
        events = db.exec(select(Event)).all()
        assert {row.event_name for row in events} >= {
            "task_step_scheduled",
            "task_step_done",
            "mission_accomplished",
        }
        for index, body in enumerate(bodies):
            assert not process_artemis_message(body, f"capture-{index}", db)
        assert len(db.exec(select(ArtemisInbox)).all()) == len(bodies)
        assert len(db.exec(select(Event)).all()) == len(events)


def test_schedule_interval_validation_and_replacement(database):
    scheduled = {
        "user_uuid": USER_ID,
        "created_at": TIMESTAMP,
        "process_id": 12343,
        "mission_process_id": 209425,
        "properties": {
            "mission_id": "NM1",
            "total_steps": 2,
            "scheduled_for": [
                {"startDate": "2026-07-22T09:00:00", "endDate": "2026-07-23T09:00:00"},
                {"startDate": "2026-07-23T09:00:00", "endDate": "2026-07-24T09:00:00"},
            ],
        },
    }
    bad = json.loads(json.dumps(scheduled))
    bad["properties"]["total_steps"] = 1
    with pytest.raises(ValidationError):
        parse_artemis_message({"task_step_scheduled": bad})
    bad["properties"]["total_steps"] = 2
    bad["properties"]["scheduled_for"][0]["endDate"] = "2026-07-21T09:00:00"
    with pytest.raises(ValidationError):
        parse_artemis_message({"task_step_scheduled": bad})
    with Session(database) as db:
        assert process_artemis_message(
            {"task_step_scheduled": scheduled}, "schedule-1", db
        )
        assert len(db.exec(select(MissionScheduledStep)).all()) == 2
        revised = json.loads(json.dumps(scheduled))
        revised["created_at"] = "2026-07-22T08:18:52.905150Z"
        revised["properties"]["total_steps"] = 1
        revised["properties"]["scheduled_for"] = [
            {"startDate": "2026-07-25T09:00:00", "endDate": "2026-07-26T09:00:00"}
        ]
        assert process_artemis_message(
            {"task_step_scheduled": revised}, "schedule-2", db
        )
        steps = db.exec(select(MissionScheduledStep)).all()
        assert len(steps) == 2
        assert [
            (row.step_id, row.is_active)
            for row in sorted(steps, key=lambda s: s.step_id)
        ] == [("1", True), ("2", False)]
        assert next(row for row in steps if row.step_id == "1").scheduled_for is None
        old = json.loads(json.dumps(scheduled))
        old["created_at"] = "2026-07-22T08:16:52.905150Z"
        assert process_artemis_message({"task_step_scheduled": old}, "schedule-old", db)
        steps = db.exec(select(MissionScheduledStep)).all()
        assert [
            (row.step_id, row.is_active)
            for row in sorted(steps, key=lambda s: s.step_id)
        ] == [
            ("1", True),
            ("2", False),
        ]
        conflict = json.loads(json.dumps(revised))
        conflict["properties"]["scheduled_for"][0]["endDate"] = "2026-07-27T09:00:00"
        with pytest.raises(ValueError, match="Conflicting mission schedules"):
            process_artemis_message(
                {"task_step_scheduled": conflict}, "schedule-conflict", db
            )


def test_old_mission_completion_does_not_close_new_selection(database):
    with Session(database) as db:
        first = messages()["mission_selected"]
        second = dict(first)
        second["created_at"] = "2026-07-23T08:17:52.905150Z"
        second["mission_process_id"] = 209426
        process_artemis_message({"mission_selected": first}, "mission-first", db)
        process_artemis_message({"mission_selected": second}, "mission-second", db)
        old_completion = {
            "user_uuid": USER_ID,
            "mission_id": "NM1",
            "created_at": "2026-07-23T09:17:52.905150Z",
            "process_id": 12343,
            "mission_process_id": 209425,
        }
        process_artemis_message(
            {"mission_accomplished": old_completion}, "old-completion", db
        )
        assert db.exec(select(MissionContent)).one().mission_end_time is None
        current_completion = dict(old_completion)
        current_completion["mission_process_id"] = 209426
        process_artemis_message(
            {"mission_accomplished": current_completion}, "new-completion", db
        )
        end_time = db.exec(select(MissionContent)).one().mission_end_time
        assert end_time is not None
        process_artemis_message(
            {"mission_accomplished": current_completion}, "new-completion-replay", db
        )
        assert db.exec(select(MissionContent)).one().mission_end_time == end_time
        stale = dict(current_completion, created_at="2026-07-23T08:47:52.905150Z")
        process_artemis_message({"mission_accomplished": stale}, "stale-completion", db)
        assert db.exec(select(MissionContent)).one().mission_end_time == end_time


def test_unknown_artemis_event_is_rejected():
    with pytest.raises(ValueError, match="Unknown Artemis event"):
        parse_artemis_message({"unexpected_event": {}})


def test_artemis_notification_type_is_normalized_and_preserved():
    event = messages()["notification_sent"].copy()
    event["properties"] = dict(event["properties"], content_type="RESOURCE")
    _, parsed = parse_artemis_message({"notification_sent": event})
    assert parsed.properties.content_type == "resource"
    event["properties"]["content_type"] = "Other"
    _, parsed = parse_artemis_message({"notification_sent": event})
    assert parsed.properties.content_type == "other"


def test_artemis_message_commits_once_and_preserves_occurrences(database):
    with Session(database) as db:
        for name, payload in messages().items():
            if name == "disabled_user":
                continue
            assert process_artemis_message({name: payload}, name, db)
        assert not process_artemis_message(
            {"mission_selected": messages()["mission_selected"]},
            "mission_selected",
            db,
        )
        user = db.get(User, USER_ID)
        assert user.characteristics["attributes"]["wearable"] == "no"
        assert len(db.exec(select(MissionSelection)).all()) == 1
        assessment = db.exec(select(HealthHabitAssessment)).one()
        assert assessment.hhs["pillar_id"] == "NO"
        assert db.exec(select(PsychologicalAssessment)).one().barriers == [
            "physical_opportunity"
        ]
        assert {event.event_name for event in db.exec(select(Event)).all()} == {
            "notification_sent",
            "notification_opened",
        }
        assert len(db.exec(select(ArtemisInbox)).all()) == 8
        assert process_artemis_message(
            {"disabled_user": messages()["disabled_user"]}, "disabled_user", db
        )
        db.refresh(user)
        assert user.is_deleted


def test_artemis_reused_message_id_rejects_changed_body(database):
    with Session(database) as db:
        first = {"new_pillar": messages()["new_pillar"]}
        assert process_artemis_message(first, "same-id", db)
        changed = {"new_pillar": {"user_uuid": USER_ID, "pillars": "SO"}}
        with pytest.raises(ValueError, match="reused"):
            process_artemis_message(changed, "same-id", db)


def test_artemis_repeated_assessments_and_escalation_keep_one_source_record(database):
    with Session(database) as db:
        for name in (
            "health_habit_assessments",
            "psychological_assessments",
            "escalation_level",
        ):
            body = {name: messages()[name]}
            process_artemis_message(body, f"{name}-a", db)
            process_artemis_message(body, f"{name}-b", db)
        assert len(db.exec(select(HealthHabitAssessment)).all()) == 1
        assert len(db.exec(select(PsychologicalAssessment)).all()) == 1
        assert len(db.exec(select(EscalationLevel)).all()) == 1
        changed = json.loads(json.dumps(messages()["psychological_assessments"]))
        changed["barriers"] = ["social_opportunity"]
        process_artemis_message(
            {"psychological_assessments": changed}, "psychology-changed", db
        )
        assert len(db.exec(select(PsychologicalAssessment)).all()) == 2


def test_disabled_new_user_and_mission_identity_conflict_roll_back(database):
    with Session(database) as db:
        disabled = dict(messages()["new_user"], enabled=False)
        process_artemis_message({"new_user": disabled}, "disabled-at-creation", db)
        assert db.get(User, USER_ID).is_deleted
        process_artemis_message(
            {"mission_selected": messages()["mission_selected"]}, "selected", db
        )
        conflict = dict(messages()["mission_selected"], mission="NM2")
        with pytest.raises(ValueError, match="reused"):
            process_artemis_message({"mission_selected": conflict}, "conflict", db)
        assert db.get(ArtemisInbox, "conflict") is None
        assert len(db.exec(select(MissionSelection)).all()) == 1


def test_notification_content_adds_another_mission(database):
    with Session(database) as db:
        sent = messages()["notification_sent"]
        process_artemis_message({"notification_sent": sent}, "first-mission", db)
        other = json.loads(json.dumps(sent))
        other["properties"]["mission_id"] = "NM2"
        process_artemis_message({"notification_sent": other}, "second-mission", db)
        assert db.exec(select(Recommendation)).one().missions == ["NM1", "NM2"]


def test_broker_message_identity_and_invalid_body_checks(database):
    with Session(database) as db:
        with pytest.raises(ValueError, match="message ID is required"):
            process_artemis_message({"new_pillar": messages()["new_pillar"]}, "", db)
        record_invalid_message("invalid", "bad-id", "reason", db)
        with pytest.raises(ValueError, match="reused"):
            record_invalid_message("different", "bad-id", "reason", db)


class RacingSession:
    """Simulate another worker committing the same broker identity first."""

    def __init__(self, existing):
        self.existing = existing
        self.calls = 0
        self.rolled_back = False

    def get(self, model, message_id):
        self.calls += 1
        return None if self.calls == 1 else self.existing

    def add(self, row):
        pass

    def execute(self, statement, parameters):
        pass

    def commit(self):
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    def rollback(self):
        self.rolled_back = True


def test_concurrent_broker_replay_uses_committed_inbox(monkeypatch):
    body = {"new_pillar": messages()["new_pillar"]}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    monkeypatch.setattr(update_service, "_apply", lambda *args: None)
    duplicate = RacingSession(
        ArtemisInbox(message_id="race", body_sha256=digest, event_name="new_pillar")
    )
    assert not process_artemis_message(body, "race", duplicate)
    assert duplicate.rolled_back
    different = RacingSession(
        ArtemisInbox(
            message_id="race", body_sha256="different", event_name="new_pillar"
        )
    )
    with pytest.raises(IntegrityError):
        process_artemis_message(body, "race", different)


def test_concurrent_invalid_message_replay_uses_committed_failure():
    body = "invalid"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    duplicate = RacingSession(
        ArtemisFailure(message_id="race", body_sha256=digest, reason="bad body")
    )
    record_invalid_message(body, "race", "bad body", duplicate)
    assert duplicate.rolled_back
    different = RacingSession(
        ArtemisFailure(message_id="race", body_sha256="different", reason="bad body")
    )
    with pytest.raises(IntegrityError):
        record_invalid_message(body, "race", "bad body", different)


def test_unexpected_commit_failure_rolls_back_valid_and_invalid_messages(monkeypatch):
    class FailedCommit:
        def __init__(self):
            self.rolled_back = False

        def get(self, model, message_id):
            return None

        def add(self, row):
            pass

        def execute(self, statement, parameters):
            pass

        def commit(self):
            raise OSError("database unavailable")

        def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(update_service, "_apply", lambda *args: None)
    valid_db = FailedCommit()
    with pytest.raises(OSError, match="database unavailable"):
        process_artemis_message(
            {"new_pillar": messages()["new_pillar"]}, "valid-failed", valid_db
        )
    assert valid_db.rolled_back
    invalid_db = FailedCommit()
    with pytest.raises(OSError, match="database unavailable"):
        record_invalid_message("broken", "invalid-failed", "bad body", invalid_db)
    assert invalid_db.rolled_back


def test_distinct_broker_ids_do_not_repeat_one_mission_or_notification(database):
    with Session(database) as db:
        for name in ("mission_selected", "notification_opened", "notification_sent"):
            body = {name: messages()[name]}
            assert process_artemis_message(body, f"{name}-first", db)
            assert process_artemis_message(body, f"{name}-second", db)
        assert len(db.exec(select(MissionSelection)).all()) == 1
        events = db.exec(select(Event)).all()
        assert len(events) == 2
        assert {event.event_name for event in events} == {
            "notification_sent",
            "notification_opened",
        }


def test_open_arriving_first_is_used_only_after_response_window(database):
    sent = messages()["notification_sent"]
    opened = dict(messages()["notification_opened"])
    opened["created_at"] = "2026-07-22T08:22:52.905150Z"
    sent_time = datetime.fromisoformat(sent["created_at"].replace("Z", "+00:00"))
    settings = {
        "response_window_min": 60,
        "history_window": 20,
        "rating_weight": 0.1,
        "delay_decay": 0.15,
    }
    with Session(database) as db:
        process_artemis_message({"notification_opened": opened}, "opened", db)
        process_artemis_message({"notification_sent": sent}, "sent", db)
        for event in db.exec(select(Event)).all():
            event.created_at = event.event_start_time.replace(tzinfo=timezone.utc)
        db.flush()
        early = observed_history(
            db,
            sent["user_uuid"],
            sent_time + timedelta(minutes=30),
            settings,
            ZoneInfo("Europe/Berlin"),
        )
        assert early.recent_opens == []
        assert early.sent_day == 1
        late = observed_history(
            db,
            sent["user_uuid"],
            sent_time + timedelta(minutes=61),
            settings,
            ZoneInfo("Europe/Berlin"),
        )
        assert late.recent_opens == [True]
        assert late.recent_delays == [5]
        assert late.engagement_ema > 0


def test_invalid_message_is_recorded_once_without_its_body(database):
    body = '{"new_user": {"email": "private@example.org"}}'
    with Session(database) as db:
        record_invalid_message(body, "invalid-1", "missing uuid", db)
        record_invalid_message(body, "invalid-1", "missing uuid", db)
        failure = db.get(ArtemisFailure, "invalid-1")
        assert failure.reason == "missing uuid"
        assert "private@example.org" not in failure.reason
        assert len(db.exec(select(ArtemisFailure)).all()) == 1


def test_sent_confirmation_links_to_policy_decision(database):
    sent_at = datetime.fromisoformat(TIMESTAMP.replace("Z", "+00:00"))
    scheduled_for = sent_at - timedelta(minutes=5)
    with Session(database) as db:
        user = User(id=USER_ID)
        mission = Mission(mission_id="NM1")
        content = Recommendation(
            content_id="NRs25",
            content_type="resource",
            missions=["NM1"],
            objective=[],
            hapa=[],
            comb=[],
            intervention_type=[],
        )
        selected = SelectedContent(
            user_id=USER_ID,
            contents=[
                {
                    "id": "NRs25",
                    "type": "resource",
                    "mission_id": "NM1",
                    "mission_process_id": 209425,
                }
            ],
        )
        db.add_all([user, mission, content])
        db.flush()
        db.add(selected)
        db.flush()
        plan = RecommendationPlan(
            user_id=user.id,
            plan_id=selected.id,
            recommendation_id=content.id,
            mission_id=mission.id,
            scheduled_for=scheduled_for,
            occurrence_index=0,
            content_type="resource",
            mission_process_id=209425,
        )
        decision = PolicyDecision(
            user_id=user.id,
            plan_id=selected.id,
            occurrence_index=0,
            decided_at=scheduled_for,
            action=1,
            propensity=1,
            state=[0],
            safe_mask=[False, True],
            policy_version="test",
        )
        db.add_all([plan, decision])
        db.commit()
        assert process_artemis_message(
            {"notification_sent": messages()["notification_sent"]}, "sent-link", db
        )
        db.refresh(decision)
        event = db.exec(select(Event)).one()
        assert decision.outcome_event_id == event.id
        process_artemis_message(
            {"notification_sent": messages()["notification_sent"]},
            "same-delivery-new-broker-id",
            db,
        )
        assert len(db.exec(select(Event)).all()) == 1
        later = json.loads(json.dumps(messages()["notification_sent"]))
        later["created_at"] = (sent_at + timedelta(minutes=1)).isoformat()
        process_artemis_message({"notification_sent": later}, "sent-again", db)
        unmatched = json.loads(json.dumps(later))
        unmatched["created_at"] = (sent_at + timedelta(minutes=2)).isoformat()
        unmatched["properties"]["content_id"] = "OTHER"
        process_artemis_message({"notification_sent": unmatched}, "sent-unmatched", db)
        db.refresh(decision)
        assert decision.outcome_event_id == event.id


def test_observed_history_ignores_an_unmatched_open(database):
    sent = messages()["notification_sent"]
    opened = json.loads(json.dumps(messages()["notification_opened"]))
    opened["properties"]["content_id"] = "OTHER"
    sent_at = datetime.fromisoformat(TIMESTAMP.replace("Z", "+00:00"))
    metadata = {
        "response_window_min": 60,
        "history_window": 20,
        "rating_weight": 0.1,
        "delay_decay": 0.15,
    }
    with Session(database) as db:
        process_artemis_message({"notification_sent": sent}, "history-sent", db)
        process_artemis_message({"notification_opened": opened}, "history-open", db)
        for event in db.exec(select(Event)).all():
            event.created_at = event.event_start_time.replace(tzinfo=timezone.utc)
        db.flush()
        history = observed_history(
            db,
            sent["user_uuid"],
            sent_at + timedelta(minutes=61),
            metadata,
            ZoneInfo("Europe/Berlin"),
        )
        assert history.recent_opens == [False]
