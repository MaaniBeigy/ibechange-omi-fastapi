"""Verify selected settings, device replay, exploration and restart fidelity."""

import copy
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from sqlmodel import Session, select

from app.models.policy_learning import (
    PolicyLearner,
    PolicyTrainingUpdate,
    PolicyTransition,
)
from app.rl.learner import ConstrainedConservativeDoubleDQN, TrainingConfig
from app.services import policy_learning_service as service
from app.services.rl_policy import (
    PolicyArtifact,
    QueueItem,
    decide_epoch,
    exploration_probability,
    load_policy_artifacts,
)
from scripts.train_policy_artifacts import _training_row, training_values
from tests.test_policy_learning_service import (
    NOW,
    add_decision,
    add_user,
    learning_database,
    policy,
)
from tests.test_rl_learning import artifact, transition
from tests.test_train_policy_artifacts import logged_row


def test_selected_profile_matches_training_defaults_and_final_study_values():
    settings = json.loads(Path("configs/omi_concddqn.json").read_text())
    defaults = TrainingConfig()
    for name in defaults.__dataclass_fields__:
        assert settings[f"agent.{name}"] == getattr(defaults, name)
    values = training_values({})
    for name, value in settings.items():
        assert values[name] == value
    assert settings["agent.hidden_units"] == 64
    assert settings["agent.history_window"] == 160
    assert settings["agent.con_cddqn_send_q_margin"] == 0.5


def test_full_comparison_reward_is_converted_to_the_fitting_objective():
    row = logged_row(arm="F", action="1", opened="1", reward="0.23")
    state = json.loads(row["state"])
    state["day_budget_left"] = 0.5
    row["state"] = json.dumps(state)
    from app.services.rl_policy import state_fields

    rebuilt = _training_row(row, state_fields(False), 1, 0.15)
    assert rebuilt["objectives"]["engagement"] == pytest.approx(0.3)
    assert rebuilt["reward"] == pytest.approx(0.3)
    row.update(opened="0", reward="-0.22")
    rebuilt = _training_row(row, state_fields(False), 1, 0.15)
    assert rebuilt["objectives"]["engagement"] == 0
    assert rebuilt["objectives"]["miss"] == -1
    row["reward"] = "-0.01"
    with pytest.raises(ValueError, match="reward"):
        _training_row(row, state_fields(False), 1, 0.15)
    row["arm"] = "B"
    with pytest.raises(ValueError, match="arm"):
        _training_row(row, state_fields(False), 1, 0.15)
    row.update(arm="F", reward="0")
    state["day_budget_left"] = 0.4
    row["state"] = json.dumps(state)
    with pytest.raises(ValueError, match="delivery count"):
        _training_row(row, state_fields(False), 1, 0.15)


def test_batched_release_earns_credits_per_transition_and_resumes_a_wrapped_ring():
    model = artifact()
    learner = ConstrainedConservativeDoubleDQN(model, seed=19)
    history = [transition(state=[value / 10]) for value in range(7)]
    learner.learn(history[:5], day=0)
    assert learner.owed == 5
    assert learner.write == 2
    restored = ConstrainedConservativeDoubleDQN(model, seed=91)
    restored.restore(json.loads(json.dumps(learner.checkpoint())))
    restored.restore_replay(history[2:5])
    assert restored.buffer == learner.buffer
    learner.learn(history[5:], day=1)
    restored.learn(history[5:], day=1)
    assert restored.checkpoint() == learner.checkpoint()
    assert learner.online.updates == 7
    with pytest.raises(ValueError, match="capacity"):
        restored.restore_replay(history)
    restored.write = 1
    with pytest.raises(ValueError, match="disagree"):
        restored.restore_replay(history[:1])


def test_online_exploration_records_exact_probabilities_and_keeps_forced_actions():
    base = load_policy_artifacts()["nonwearer"]
    model = PolicyArtifact(
        dict(base.metadata, learner_revision="test", learning_day=0),
        [np.zeros_like(w) for w in base.weights],
        [np.zeros_like(b) for b in base.biases],
    )
    arguments = dict(
        policy=model,
        items=(QueueItem("recommendation", 1319),),
        minute=420,
        weekday=0,
        sent_day=0,
        sent_week=0,
        since_last=10080,
        engagement_ema=0,
        recent_opens=[],
        recent_delays=[],
        mission_minute=None,
        floor_day=1,
        floor_week=0,
    )
    found = {}
    for seed in range(1000):
        choice = decide_epoch(**arguments, exploration_seed=seed)
        found[choice.action] = choice
        if len(found) == 2:
            break
    assert found[1].propensity == pytest.approx(0.025)
    assert found[0].propensity == pytest.approx(0.975)
    for choice in found.values():
        assert choice.action_probabilities == pytest.approx([0.975, 0.025])
        assert choice.q_values == [0.0, 0.0]
    assert exploration_probability(
        dict(model.metadata, learning_day=45)
    ) == pytest.approx(0.03)
    assert exploration_probability(
        dict(model.metadata, learning_day=90)
    ) == pytest.approx(0.01)
    assert exploration_probability(base.metadata) == 0
    for changes in [{"epsilon_online": 2}, {"exploration_days": 0}]:
        with pytest.raises(ValueError, match="exploration"):
            exploration_probability(dict(model.metadata, **changes))
    forced = decide_epoch(**dict(arguments, minute=1305), exploration_seed=20)
    assert forced.action == 1
    assert forced.propensity == 1
    assert forced.action_probabilities == [0.0, 1.0]
    assert forced.q_values == [0.0, 0.0]
    assert (
        model.version
        == PolicyArtifact(
            dict(model.metadata, learning_day=90), model.weights, model.biases
        ).version
    )


def test_new_rct_users_share_device_history_and_keep_online_learning_separate(
    learning_database, monkeypatch
):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "online")
    model = load_policy_artifacts()["nonwearer"]
    shared_ids = {id(row) for row in model.offline}
    sampled = []
    draw = ConstrainedConservativeDoubleDQN._draw

    def capture(learner):
        batch, offline, personal = draw(learner)
        sampled.append(
            (
                sum(id(row) in shared_ids for row in batch),
                [row for row in batch if id(row) not in shared_ids],
            )
        )
        return batch, offline, personal

    monkeypatch.setattr(ConstrainedConservativeDoubleDQN, "_draw", capture)
    with Session(learning_database) as db:
        user, selected = add_user(db)
        other, other_selected = add_user(db)
        first = service.prepare_policy(db, user.id, model, NOW)
        second = service.prepare_policy(db, other.id, model, NOW)
        np.testing.assert_array_equal(first.weights[0], model.weights[0])
        np.testing.assert_array_equal(second.weights[0], model.weights[0])
        for index in range(33):
            decision = add_decision(
                db, user, selected, model, NOW + timedelta(minutes=15 * index)
            )
            decision.state = [0.7] * len(model.metadata["fields"])
        for index in range(3):
            decision = add_decision(
                db, other, other_selected, model, NOW + timedelta(minutes=15 * index)
            )
            decision.state = [0.9] * len(model.metadata["fields"])
        db.commit()
        fitted = service.prepare_policy(db, user.id, model, NOW + timedelta(days=1))
        stored = db.exec(
            select(PolicyLearner).where(PolicyLearner.user_id == user.id)
        ).one()
        assert stored.checkpoint["offline_source"] == "shared_device"
        assert "participant_offline" not in stored.checkpoint
        assert stored.checkpoint["updates"] == 128
        assert len(sampled) == 128
        audits = db.exec(
            select(PolicyTrainingUpdate).order_by(PolicyTrainingUpdate.update_number)
        ).all()
        assert [u.update_number for u in audits] == list(range(1, 129))
        for u in audits:
            assert u.learner_id == stored.id
            assert len(u.offline_indices) == 8 and len(u.personal_replay_indices) == 24
            assert set(u.personal_replay_indices) <= set(range(1, 33))
            assert len(u.targets) == len(u.q_values) == len(u.td_residuals) == 32
        assert all(count == 8 and len(online) == 24 for count, online in sampled)
        assert all(
            row["state"] == [0.7] * len(model.metadata["fields"])
            for _, online in sampled
            for row in online
        )
        other_stored = db.exec(
            select(PolicyLearner).where(PolicyLearner.user_id == other.id)
        ).one()
        assert other_stored.checkpoint["updates"] == 0
        before = copy.deepcopy(stored.checkpoint)
        db.commit()
        restored = service.prepare_policy(
            db, user.id, model, NOW + timedelta(days=1, minutes=1)
        )
        assert stored.checkpoint == before
        assert len(db.exec(select(PolicyTrainingUpdate)).all()) == 128
        np.testing.assert_array_equal(restored.weights[0], fitted.weights[0])
        assert not np.array_equal(fitted.weights[0], model.weights[0])
        assert set(shared_ids) == {id(row) for row in model.offline}


def test_live_learning_cannot_silently_omit_shared_replay(
    learning_database, policy, monkeypatch
):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "online")
    missing = PolicyArtifact(policy.metadata, policy.weights, policy.biases)
    with Session(learning_database) as db:
        user, _ = add_user(db)
        with pytest.raises(ValueError, match="shared device replay"):
            service.prepare_policy(db, user.id, missing, NOW)


def test_daily_refit_uses_all_released_rows_in_one_batch(
    learning_database, policy, monkeypatch
):
    monkeypatch.setenv("POLICY_LEARNING_MODE", "online")
    with Session(learning_database) as db:
        user, selected = add_user(db)
        service.prepare_policy(db, user.id, policy, NOW)
        for index in range(4):
            add_decision(
                db, user, selected, policy, NOW + timedelta(minutes=15 * index)
            )
        service.prepare_policy(db, user.id, policy, NOW + timedelta(days=1))
        stored = db.exec(select(PolicyLearner)).one()
        assert stored.checkpoint["updates"] == 3
        assert stored.checkpoint["owed"] == 0
        assert (
            len(
                db.exec(
                    select(PolicyTransition).where(PolicyTransition.consumed_at != None)
                ).all()
            )
            == 3
        )


@pytest.mark.parametrize(
    "change,message",
    [
        ({"replay_write": -1}, "position"),
        ({"offline_source": "unknown"}, "source"),
        ({"offline_source": "participant", "participant_offline": []}, "source"),
    ],
)
def test_corrupt_replay_checkpoint_is_rejected(change, message):
    learner = ConstrainedConservativeDoubleDQN(artifact())
    with pytest.raises(ValueError, match=message):
        learner.restore(dict(learner.checkpoint(), **change))


def test_learning_check_records_real_updates_and_fixed_weights_in_frozen_mode():
    from scripts.check_policy_learning import run_check

    daily, online = run_check(days=3, seed=11)
    repeated, again = run_check(days=3, seed=11)
    frozen_daily, frozen = run_check(days=3, seed=11, mode="frozen")
    assert daily == repeated
    assert online == again
    assert online["gradient_updates"] > 0
    assert online["weights_changed"]
    assert daily[0]["gradient_updates"] == 0
    assert daily[-1]["gradient_updates"] > daily[1]["gradient_updates"]
    assert not frozen["weights_changed"]
    assert frozen["gradient_updates"] == 0
    for summary in (online, frozen):
        assert summary["constraint_violations"] == 0
        assert summary["pending_transitions"] == 0
        assert summary["sends"] == 12
    assert all(row["sends"] == 4 for row in daily + frozen_daily)
    with pytest.raises(ValueError):
        run_check(days=0)


@pytest.mark.parametrize(
    "device,mode", [("nonwearer", "online"), ("wearer", "online"), ("wearer", "frozen")]
)
def test_learning_check_resumes_at_day_boundary(tmp_path, device, mode):
    from scripts.check_policy_learning import run_check

    path = tmp_path / "training.json"
    run_check(2, 11, mode, path, device)
    resumed = run_check(3, 11, mode, path, device)
    control = tmp_path / "control"
    assert resumed == run_check(3, 11, mode, device=device, audit_dir=control)
    files = sorted(control.glob("day-*.json"))
    assert len(files) == 3
    for file in files:
        assert (
            file.read_bytes() == (path.with_suffix(".audit") / file.name).read_bytes()
        )
        day = json.loads(file.read_bytes())
        assert all(
            d["propensity"] == d["action_probabilities"][d["action"]]
            for d in day["decisions"]
        )
        assert all(isinstance(t["decision_minute"], int) for t in day["transitions"])
    with pytest.raises(ValueError, match="precedes"):
        run_check(1, 11, mode, path, device)
    with pytest.raises(ValueError, match="seed"):
        run_check(3, 32, mode, path, device)
