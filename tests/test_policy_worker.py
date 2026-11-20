"""Check real clock scheduling, independent transactions and clean shutdown."""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from sqlmodel import Session, select

from app import bootstrap, policy_worker
from app.models.policy_decision import PolicyDecision
from app.models.selected_content import SelectedContent
from app.models.user import User
from tests.test_policy_learning_service import NOW, learning_database


def test_worker_commits_users_independently_and_handles_disabled_or_missing_queue(
    learning_database, monkeypatch
):
    with Session(learning_database) as db:
        users = [User() for _ in range(4)]
        users[2].is_deleted = True
        db.add_all(users)
        db.flush()
        for user in users[:3]:
            db.add(SelectedContent(user_id=user.id, contents=[]))
        bad_user = users[0].id
        db.commit()
    called = []

    def generate(selected, epoch, db):
        called.append(selected.user_id)
        if selected.user_id == bad_user:
            raise ValueError("infeasible queue")

    monkeypatch.setattr(policy_worker, "generate_recommendation_plan", generate)
    assert policy_worker.run_epoch(learning_database, NOW) == 3
    assert len(called) == 2


def test_worker_records_real_policy_decisions_without_http(learning_database):
    with Session(learning_database) as db:
        user = User()
        db.add(user)
        db.flush()
        db.add(
            SelectedContent(
                user_id=user.id,
                contents=[{"id": "R1", "type": "resource", "mission_id": "NM1"}],
            )
        )
        db.commit()
    # In the morning a zero-preference network skips; no catalog lookup is needed.
    from app.services.rl_policy import load_policy_artifacts

    policies = load_policy_artifacts()
    original = policies["nonwearer"].metadata["send_q_margin"]
    policies["nonwearer"].metadata["send_q_margin"] = 1e8
    try:
        assert policy_worker.run_epoch(learning_database, NOW) == 1
        assert policy_worker.run_epoch(learning_database, NOW) == 1
    finally:
        policies["nonwearer"].metadata["send_q_margin"] = original
    with Session(learning_database) as db:
        assert len(db.exec(select(PolicyDecision)).all()) == 1


def test_worker_waits_on_restart_runs_boundaries_once_and_stops(monkeypatch):
    moments = iter([NOW + timedelta(minutes=2), NOW, NOW + timedelta(milliseconds=200)])

    class Clock:
        @staticmethod
        def now(zone):
            return next(moments)

        fromtimestamp = datetime.fromtimestamp

    class Stop:
        calls = 0

        def is_set(self):
            return self.calls == 3

        def wait(self, duration):
            assert 0 < duration <= 30
            self.calls += 1

    runs = []
    monkeypatch.setattr(policy_worker, "datetime", Clock)
    monkeypatch.setattr(
        policy_worker, "run_epoch", lambda engine, epoch: runs.append(epoch)
    )
    policy_worker.run_worker(object(), Stop())
    # The first clock sample is a mid-epoch restart, so that epoch is not backdated.
    assert runs == []


def test_worker_runs_the_next_boundary_once(monkeypatch):
    moments = iter(
        [
            NOW + timedelta(minutes=2),
            NOW + timedelta(minutes=15),
            NOW + timedelta(minutes=15, milliseconds=200),
        ]
    )

    class Clock:
        @staticmethod
        def now(zone):
            return next(moments)

        fromtimestamp = datetime.fromtimestamp

    stopped = Mock()
    stopped.is_set.side_effect = [False, False, False, True]
    runs = []
    monkeypatch.setattr(policy_worker, "datetime", Clock)
    monkeypatch.setattr(
        policy_worker, "run_epoch", lambda engine, epoch: runs.append(epoch)
    )
    policy_worker.run_worker(object(), stopped)
    assert runs == [NOW + timedelta(minutes=15)]
    assert stopped.wait.call_count == 3


def test_worker_main_validates_configuration_and_registers_signals(monkeypatch):
    import app.dependencies

    signals = []
    ran = []
    monkeypatch.setattr(
        policy_worker.signal, "signal", lambda sig, callback: signals.append(callback)
    )
    monkeypatch.setattr(
        policy_worker, "run_worker", lambda engine, event: ran.append(event)
    )
    policy_worker.main()
    assert len(signals) == 2
    assert not ran[0].is_set()
    signals[0](None, None)
    assert ran[0].is_set()
    ran[0].clear()
    signals[1](None, None)
    assert ran[0].is_set()


def test_bootstrap_creates_baseline_before_migration(monkeypatch):
    steps = []
    monkeypatch.setattr(
        bootstrap.SQLModel.metadata,
        "create_all",
        lambda engine: steps.append("baseline"),
    )
    monkeypatch.setattr(
        bootstrap.command, "upgrade", lambda config, revision: steps.append(revision)
    )
    bootstrap.main()
    assert steps == ["baseline", "head"]


def test_worker_finishes_old_device_feedback_without_rewinding(
    learning_database, monkeypatch
):
    from app.models.policy_learning import PolicyLearner, PolicyTransition
    from app.services.rl_policy import load_policy_artifacts
    from tests.test_policy_learning_service import add_decision, add_user

    monkeypatch.setenv("POLICY_LEARNING_MODE", "online")
    policy = load_policy_artifacts()["nonwearer"]
    with Session(learning_database) as db:
        user, selected = add_user(db)
        add_decision(db, user, selected, policy, NOW)
        user.characteristics = {"attributes": {"wearable": "yes"}}
        db.commit()
    assert policy_worker.run_epoch(learning_database, NOW + timedelta(minutes=15)) == 1
    assert policy_worker.run_epoch(learning_database, NOW) == 0
    with Session(learning_database) as db:
        learners = db.exec(select(PolicyLearner)).all()
        assert {row.device: row.checkpoint["replay_count"] for row in learners} == {
            "wearer": 0,
            "nonwearer": 1,
        }
        assert db.exec(select(PolicyTransition)).one().replay_index == 1
