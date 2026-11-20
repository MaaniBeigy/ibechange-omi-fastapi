"""Exercise delayed outcomes, durable replay, isolation and worker transactions."""

import copy
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.event import Event
from app.models.policy_decision import PolicyDecision
from app.models.policy_learning import PolicyLearner, PolicyTransition
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.services import policy_learning_service as service
from app.services.artemis_update_service import (
    _link_sent_decision,
    process_artemis_message,
)
from app.services.rl_observations import observed_history
from app.services.rl_policy import PolicyArtifact, load_policy_artifacts

NOW = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)


@pytest.fixture
def learning_database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for learning integration tests")
    schema = f"test_learning_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def policy():
    base = load_policy_artifacts()["nonwearer"]
    metadata = dict(
        base.metadata,
        batch_size=2,
        replay_capacity=4,
        target_copy_interval=2,
        updates_per_epoch=1,
    )
    return PolicyArtifact(
        metadata,
        [p.copy() for p in base.weights],
        [p.copy() for p in base.biases],
        offline=base.offline,
    )


def add_user(db):
    user = User()
    db.add(user)
    db.flush()
    selected = SelectedContent(user_id=user.id, contents=[])
    db.add(selected)
    db.flush()
    return user, selected


def add_decision(db, user, selected, policy, when, action=0):
    decision = PolicyDecision(
        user_id=user.id,
        plan_id=selected.id,
        occurrence_index=0,
        decided_at=when,
        action=action,
        propensity=1,
        state=[0.1] * len(policy.metadata["fields"]),
        safe_mask=[True, True],
        policy_version=policy.version + "-omi2x2-v1",
    )
    db.add(decision)
    db.flush()
    service.record_next_state(db, decision)
    return decision


def notification(
    user_id, when, name="notification_sent", content_id="R1", received=None
):
    return Event(
        user_id=user_id,
        event_name=name,
        process_id=42,
        event_start_time=when.replace(tzinfo=None),
        created_at=received or when,
        characteristics=dict(
            content_id=content_id,
            mission_id="NM1",
            mission_process_id=10,
            content_type="resource",
        ),
    )


@pytest.mark.parametrize("opened", [True, False])
def test_reward_waits_for_confirmed_delivery_and_closed_response_window(
    learning_database, policy, monkeypatch, opened
):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "frozen")
    with Session(learning_database) as db:
        user, selected = add_user(db)
        decision = add_decision(db, user, selected, policy, NOW, action=1)
        add_decision(db, user, selected, policy, NOW + timedelta(minutes=15))
        sent = notification(user.id, NOW)
        db.add(sent)
        db.flush()
        decision.outcome_event_id = sent.id
        if opened:
            db.add(
                notification(user.id, NOW + timedelta(minutes=5), "notification_opened")
            )
        service.prepare_policy(db, user.id, policy, NOW + timedelta(minutes=30))
        row = db.exec(select(PolicyTransition)).one()
        assert row.status == "pending"
        assert row.reward is None
        assert row.elapsed_min == 15
        assert row.bootstrap == 1
        frozen = service.prepare_policy(
            db, user.id, policy, NOW + timedelta(minutes=60)
        )
        expected = 0.9 * np.exp(-0.15 * 5) if opened else -0.15
        assert row.reward == pytest.approx(expected)
        assert row.status == "released"
        assert row.outcome_event_id == sent.id
        assert bool(row.opened_event_id) == opened
        assert not db.exec(select(PolicyLearner)).all()
        np.testing.assert_array_equal(frozen.weights[-1], policy.weights[-1])
        db.commit()
        service.prepare_policy(db, user.id, policy, NOW + timedelta(minutes=70))
        assert db.exec(select(PolicyTransition)).one().reward == pytest.approx(expected)


def test_future_ingestion_and_outside_window_opens_do_not_change_reward(
    learning_database, policy
):
    with Session(learning_database) as db:
        user, selected = add_user(db)
        decision = add_decision(db, user, selected, policy, NOW, 1)
        add_decision(db, user, selected, policy, NOW + timedelta(minutes=15))
        sent = notification(user.id, NOW)
        db.add(sent)
        db.flush()
        decision.outcome_event_id = sent.id
        db.add_all(
            [
                notification(
                    user.id,
                    NOW + timedelta(minutes=5),
                    "notification_opened",
                    received=NOW + timedelta(hours=2),
                ),
                notification(
                    user.id, NOW + timedelta(minutes=60), "notification_opened"
                ),
                notification(
                    user.id,
                    NOW + timedelta(minutes=3),
                    "notification_opened",
                    content_id="OTHER",
                ),
            ]
        )
        service.prepare_policy(db, user.id, policy, NOW + timedelta(minutes=61))
        row = db.exec(select(PolicyTransition)).one()
        assert row.reward == -0.15
        assert row.opened_event_id is None
        service.prepare_policy(db, user.id, policy, NOW + timedelta(hours=3))
        assert row.reward == -0.15


@pytest.mark.parametrize(
    "send_kind", ["missing", "future", "deleted", "virtual", "proposal"]
)
def test_unconfirmed_plan_is_excluded_from_learning(
    learning_database, policy, send_kind
):
    with Session(learning_database) as db:
        user, selected = add_user(db)
        decision = add_decision(db, user, selected, policy, NOW, 1)
        add_decision(db, user, selected, policy, NOW + timedelta(minutes=15))
        if send_kind != "missing":
            sent = notification(
                user.id,
                NOW,
                received=NOW + timedelta(hours=2) if send_kind == "future" else NOW,
            )
            sent.is_deleted = send_kind == "deleted"
            sent.is_virtual = send_kind == "virtual"
            if send_kind == "proposal":
                sent.event_name = "notification_plan"
            db.add(sent)
            db.flush()
            decision.outcome_event_id = sent.id
        service.prepare_policy(db, user.id, policy, NOW + timedelta(minutes=30))
        assert db.exec(select(PolicyTransition)).one().status == "pending"
        service.prepare_policy(db, user.id, policy, NOW + timedelta(minutes=61))
        row = db.exec(select(PolicyTransition)).one()
        assert row.status == "undelivered"
        assert row.reward is None
        assert row.consumed_at is None


def test_checkpoint_resumes_and_concurrent_workers_fit_each_reward_once(
    learning_database, policy, monkeypatch
):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "online")
    with Session(learning_database) as db:
        user, selected = add_user(db)
        user_id = user.id
        for minutes in (0, 15, 30):
            add_decision(db, user, selected, policy, NOW + timedelta(minutes=minutes))
        original = service.prepare_policy(
            db, user.id, policy, NOW + timedelta(minutes=30)
        )
        stored = db.exec(select(PolicyLearner)).one()
        assert stored.checkpoint["updates"] == 0
        assert stored.checkpoint["owed"] == 2
        assert "-online-" in original.version
        db.commit()

    def fit(_):
        with Session(learning_database) as db:
            result = service.prepare_policy(
                db, user_id, policy, NOW + timedelta(days=1)
            )
            db.commit()
            return result.version

    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = list(pool.map(fit, range(2)))
    assert versions[0] == versions[1]
    with Session(learning_database) as db:
        stored = db.exec(select(PolicyLearner)).one()
        assert stored.checkpoint["updates"] == 2
        assert stored.checkpoint["owed"] == 0
        checkpoint = copy.deepcopy(stored.checkpoint)
        resumed = service.prepare_policy(
            db, user_id, policy, NOW + timedelta(days=1, minutes=15)
        )
        assert resumed.version == versions[0]
        assert stored.checkpoint == checkpoint
        assert (
            len(
                db.exec(
                    select(PolicyTransition).where(PolicyTransition.consumed_at != None)
                ).all()
            )
            == 2
        )
        other, _ = add_user(db)
        clean = service.prepare_policy(db, other.id, policy, NOW + timedelta(days=1))
        assert clean.version != resumed.version
        np.testing.assert_array_equal(clean.weights[0], policy.weights[0])


def test_invalid_checkpoint_rolls_back_reward_release_and_does_not_reset(
    learning_database, policy, monkeypatch
):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "online")
    with Session(learning_database) as db:
        user, selected = add_user(db)
        add_decision(db, user, selected, policy, NOW)
        service.prepare_policy(db, user.id, policy, NOW)
        stored = db.exec(select(PolicyLearner)).one()
        stored.checkpoint = dict(stored.checkpoint, config_sha256="invalid")
        db.commit()
        add_decision(db, user, selected, policy, NOW + timedelta(minutes=15))
        db.commit()
        with pytest.raises(ValueError, match="checkpoint"):
            service.prepare_policy(db, user.id, policy, NOW + timedelta(minutes=15))
        db.rollback()
        assert db.exec(select(PolicyTransition)).one().status == "pending"
        assert (
            db.exec(select(PolicyLearner)).one().checkpoint["config_sha256"]
            == "invalid"
        )


def test_day_boundary_device_change_and_empty_queue_close_once(
    learning_database, policy
):
    with Session(learning_database) as db:
        user, selected = add_user(db)
        first = add_decision(db, user, selected, policy, NOW)
        second = add_decision(db, user, selected, policy, NOW + timedelta(days=1))
        assert db.exec(select(PolicyTransition)).one().bootstrap == 0
        service.close_episode(db, user.id, NOW + timedelta(days=1, minutes=15))
        service.close_episode(db, user.id, NOW + timedelta(days=1, minutes=30))
        rows = db.exec(select(PolicyTransition)).all()
        assert len(rows) == 2
        assert {row.decision_id for row in rows} == {first.id, second.id}
        # A new device's state width cannot be bootstrapped through an old network.
        third = add_decision(db, user, selected, policy, NOW + timedelta(days=2))
        third.policy_version = third.policy_version.replace("nonwearer", "wearer")
        fourth = add_decision(
            db, user, selected, policy, NOW + timedelta(days=2, minutes=15)
        )
        row = db.exec(
            select(PolicyTransition).where(PolicyTransition.decision_id == third.id)
        ).one()
        assert row.bootstrap == 0
        assert row.device == "wearer"
        service.close_episode(db, uuid4(), NOW)


def test_observation_excludes_events_received_after_epoch(learning_database, policy):
    with Session(learning_database) as db:
        user, _ = add_user(db)
        db.add(notification(user.id, NOW, received=NOW + timedelta(hours=2)))
        db.flush()
        early = observed_history(
            db, user.id, NOW + timedelta(minutes=61), policy.metadata, ZoneInfo("UTC")
        )
        assert early.sent_day == 0
        assert early.recent_opens == []
        late = observed_history(
            db, user.id, NOW + timedelta(hours=3), policy.metadata, ZoneInfo("UTC")
        )
        assert late.sent_day == 1
        assert late.recent_opens == [False]


def test_notification_proposal_is_not_delivery_evidence(learning_database):
    with Session(learning_database) as db:
        user_id = uuid4()
        body = {
            "notification_plan": {
                "user_id": str(user_id),
                "start_time": NOW.isoformat(),
                "end_time": (NOW + timedelta(hours=1)).isoformat(),
                "notifications": [{"id": "R1", "type": "resource", "mission": "NM1"}],
            }
        }
        process_artemis_message(body, "proposal", db)
        assert db.exec(select(Event)).one().event_name == "notification_plan"
        assert not db.exec(
            select(Event).where(Event.event_name == "notification_sent")
        ).all()


def test_learning_migration_upgrade_downgrade_and_existing_rows(
    learning_database, policy
):
    with Session(learning_database) as db:
        user, selected = add_user(db)
        decision_id = add_decision(db, user, selected, policy, NOW).id
        db.commit()
    path = Path("migrations/versions/20261004_06_policy_learning.py")
    spec = importlib.util.spec_from_file_location("learning_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with learning_database.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        assert "PolicyLearner" not in inspect(connection).get_table_names()
        migration.upgrade()
        migration.upgrade()
        tables = inspect(connection).get_table_names()
        assert {"PolicyLearner", "PolicyTransition", "PolicyDecision"} <= set(tables)
        unique = inspect(connection).get_unique_constraints("PolicyTransition")
        assert unique[0]["column_names"] == ["decision_id"]
        assert (
            connection.execute(text('SELECT id FROM "PolicyDecision"')).scalar()
            == decision_id
        )


def test_learning_mode_validation(monkeypatch):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "invalid")
    with pytest.raises(ValueError, match="frozen or online"):
        service.learning_mode()


def test_database_rejects_two_queues_deciding_for_one_user_at_same_epoch(
    learning_database, policy
):
    with Session(learning_database) as db:
        user, selected = add_user(db)
        add_decision(db, user, selected, policy, NOW)
        another = SelectedContent(user_id=user.id, contents=[])
        db.add(another)
        db.flush()
        with pytest.raises(IntegrityError, match="uq_policy_user_epoch"):
            add_decision(db, user, another, policy, NOW)
        db.rollback()


def test_unique_epoch_migration_rolls_back_when_legacy_data_conflicts(
    learning_database, policy
):
    with Session(learning_database) as db:
        user, selected = add_user(db)
        add_decision(db, user, selected, policy, NOW)
        db.commit()
        db.execute(
            text('ALTER TABLE "PolicyDecision" DROP CONSTRAINT uq_policy_user_epoch')
        )
        another = SelectedContent(user_id=user.id, contents=[])
        db.add(another)
        db.flush()
        add_decision(db, user, another, policy, NOW)
        db.commit()
    spec = importlib.util.spec_from_file_location(
        "learning_conflict_migration",
        Path("migrations/versions/20261004_06_policy_learning.py"),
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with pytest.raises(IntegrityError, match="uq_policy_user_epoch"):
        with learning_database.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
    with learning_database.connect() as connection:
        assert (
            connection.execute(text('SELECT COUNT(*) FROM "PolicyDecision"')).scalar()
            == 2
        )
        assert "uq_policy_user_epoch" not in {
            row["name"]
            for row in inspect(connection).get_unique_constraints("PolicyDecision")
        }
