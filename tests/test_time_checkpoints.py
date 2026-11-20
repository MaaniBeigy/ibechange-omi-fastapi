"""Time-travel restoration uses only the participant's committed replay prefix."""

import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlmodel import Session, select

from app.models.policy_learning import PolicyCheckpoint, PolicyLearner, PolicyTransition
from app.rl.learner import ConstrainedConservativeDoubleDQN
from app.services import policy_learning_service as service
from scripts.export_policy_checkpoint import export_checkpoint
from tests.test_policy_learning_service import (
    NOW,
    add_decision,
    add_user,
    learning_database,
    policy,
)


def test_historical_continuation_across_wrap_and_refit(
    learning_database, policy, monkeypatch, tmp_path
):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "online")
    cut = NOW + timedelta(hours=2)
    refit = NOW + timedelta(days=1)
    with Session(learning_database) as db:
        user, selected = add_user(db)
        uid = user.id
        other, _ = add_user(db)
        service.prepare_policy(db, uid, policy, NOW)
        for index in range(8):
            decision = add_decision(
                db, user, selected, policy, NOW + timedelta(minutes=index * 15)
            )
            decision.state = [index / 10] * len(decision.state)
        service.prepare_policy(db, uid, policy, cut)
        snapshot = db.exec(select(PolicyLearner)).one().checkpoint
        assert snapshot["replay_count"] == 7 and snapshot["replay_write"] == 3
        service.prepare_policy(db, other.id, policy, refit)
        db.commit()
    with Session(learning_database) as db:
        service.prepare_policy(db, uid, policy, refit)
        head = (
            db.exec(select(PolicyLearner).where(PolicyLearner.user_id == uid))
            .one()
            .checkpoint
        )
        assert head["updates"] == 7
        db.commit()
    with Session(learning_database) as db:
        with pytest.raises(ValueError, match="No participant checkpoint"):
            service.restore_policy_at(db, uid, policy, NOW - timedelta(seconds=1))
        with pytest.raises(ValueError, match="precedes"):
            service.prepare_policy(db, uid, policy, cut)
        with pytest.raises(ValueError, match="UTC offset"):
            export_checkpoint(
                db, uid, policy, cut.replace(tzinfo=None), tmp_path / "bad"
            )
        for instant, expected in [(cut, snapshot), (refit, head)]:
            output = tmp_path / "personal.json"
            export_checkpoint(db, uid, policy, instant, output)
            saved = json.loads(output.read_text())
            assert saved["user_id"] == str(uid)
            assert saved["checkpoint"] == expected
            clone = ConstrainedConservativeDoubleDQN(policy)
            clone.restore_portable(saved)
            if instant == cut:
                clone.learn([], day=1)
                assert clone.checkpoint() == head
        first = service.restore_policy_at(db, uid, policy, NOW)
        assert not first.buffer and first.owed == 0
        assert len(db.exec(select(PolicyCheckpoint)).all()) == 3
        # Missing rows after a snapshot must fail visibly.
        db.delete(
            db.exec(
                select(PolicyTransition).where(PolicyTransition.replay_index == 2)
            ).one()
        )
        db.flush()
        with pytest.raises(ValueError, match="Gap"):
            service.restore_policy_at(db, uid, policy, cut)


def test_migration_preserves_existing_head_and_replay(
    learning_database, policy, monkeypatch
):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "online")
    with Session(learning_database) as db:
        user, selected = add_user(db)
        uid = user.id
        add_decision(db, user, selected, policy, NOW)
        cut = NOW + timedelta(minutes=15)
        add_decision(db, user, selected, policy, cut)
        service.prepare_policy(db, uid, policy, cut)
        expected = service.restore_policy_at(db, uid, policy, cut).portable_checkpoint()
        db.commit()
    spec = importlib.util.spec_from_file_location(
        "checkpoint_migration",
        Path("migrations/versions/20261120_07_time_checkpoints.py"),
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with learning_database.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        migration.upgrade()
        migration.upgrade()
    with Session(learning_database) as db:
        assert (
            service.restore_policy_at(db, uid, policy, cut).portable_checkpoint()
            == expected
        )
        with pytest.raises(ValueError, match="No participant checkpoint"):
            service.restore_policy_at(db, uid, policy, NOW)


def test_wait_reward_cannot_reveal_a_future_successor(
    learning_database, policy, monkeypatch
):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "online")
    with Session(learning_database) as db:
        user, selection = add_user(db)
        add_decision(db, user, selection, policy, NOW)
        add_decision(db, user, selection, policy, NOW + timedelta(minutes=15))
        service.prepare_policy(db, user.id, policy, NOW + timedelta(minutes=10))
        row = db.exec(select(PolicyTransition)).one()
        assert row.status == "pending" and row.replay_index is None
        service.prepare_policy(db, user.id, policy, NOW + timedelta(minutes=15))
        assert row.replay_index == 1
