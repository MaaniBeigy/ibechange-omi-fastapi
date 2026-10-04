"""Check the source equations, gradients, masks and restart behavior independently."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from app.rl.learner import (
    ConstrainedConservativeDoubleDQN,
    TrainingConfig,
    validate_transition,
)
from app.rl.network import Network
from app.services.rl_policy import PolicyArtifact, QueueItem, safe_actions


def artifact(**settings):
    metadata = dict(
        fields=["x"],
        device="nonwearer",
        sample_sha256="a" * 64,
        batch_size=2,
        replay_capacity=3,
        target_copy_interval=2,
        updates_per_epoch=1,
        send_q_margin=0,
        **settings,
    )
    return PolicyArtifact(
        metadata, [np.asarray([[0.2, 0.7]])], [np.asarray([0.1, 0.2])]
    )


def transition(**settings):
    row = dict(
        state=[0.5],
        next_state=[1.0],
        action=1,
        objectives={"engagement": 0.3, "miss": 0},
        safe_mask=[True, True],
        next_safe_mask=[True, True],
        elapsed_min=15.0,
        bootstrap=1,
    )
    row.update(settings)
    return row


def test_network_backward_matches_finite_differences_and_adam_step():
    network = Network(2, 3, np.random.default_rng(8), layers=2)
    network.weights[-1][...] = [[0.3, 0.2], [0.1, 0.4], [-0.2, 0.1]]
    network.biases[0][...] = 2
    network.biases[1][...] = 2
    features = np.asarray([[0.3, 0.7], [0.5, 0.2]])
    upstream = np.asarray([[0.2, -0.4], [-0.3, 0.1]])
    _, hidden = network.forward(features)
    gradients = network.backward(features, hidden, upstream)
    for parameter, gradient in zip(network.parameters(), gradients):
        numeric = np.zeros_like(parameter)
        for index in np.ndindex(parameter.shape):
            value = parameter[index]
            parameter[index] = value + 1e-6
            right = (network.values(features) * upstream).sum()
            parameter[index] = value - 1e-6
            left = (network.values(features) * upstream).sum()
            parameter[index] = value
            numeric[index] = (right - left) / 2e-6
        np.testing.assert_allclose(gradient, numeric, atol=1e-8)
    before = [p.copy() for p in network.parameters()]
    network.apply(gradients, 0.001)
    for parameter, old, gradient in zip(network.parameters(), before, gradients):
        np.testing.assert_allclose(
            parameter, old - 0.001 * gradient / (np.abs(gradient) + 1e-8)
        )
    other = Network(2, 3, np.random.default_rng(3), layers=2)
    network.copy_into(other)
    assert not np.shares_memory(other.weight_out, network.weight_out)
    np.testing.assert_array_equal(other.bias_out, network.bias_out)


def test_double_target_uses_online_selection_target_evaluation_discount_and_mask():
    learner = ConstrainedConservativeDoubleDQN(artifact())
    learner.online.weights[0][...] = [[1, 100]]
    learner.online.biases[0][...] = 0
    learner.target.weights[0][...] = [[8, 999]]
    learner.target.biases[0][...] = 0
    row = transition(
        next_safe_mask=[True, False],
        elapsed_min=2880,
        objectives={"engagement": 0.3, "miss": -1},
    )
    result = learner.update([row], 0.001, 0)
    assert result["targets"] == pytest.approx([0.3 - 0.15 + 0.5 * 8])
    assert not np.array_equal(learner.online.weight_out, learner.target.weight_out)
    learner.update([row], 0.001, 0)
    np.testing.assert_array_equal(learner.online.weight_out, learner.target.weight_out)


def test_terminal_transition_has_no_bootstrap_even_without_next_safe_action():
    learner = ConstrainedConservativeDoubleDQN(artifact())
    result = learner.update(
        [transition(bootstrap=0, next_safe_mask=[False, False])], 0.001, 1
    )
    assert result["targets"] == pytest.approx([0.3])


@pytest.mark.parametrize("mask", [[True, True], [False, True]])
def test_cql_gradient_is_softmax_only_over_feasible_actions(monkeypatch, mask):
    learner = ConstrainedConservativeDoubleDQN(artifact())
    row = transition(safe_mask=mask, bootstrap=0)
    q = learner.online.values([row["state"]])[0]
    expected = np.asarray([0.0, 2 * (q[1] - 0.3)])
    feasible_q = np.where(mask, q, -np.inf)
    weights = np.exp(feasible_q - np.max(feasible_q))
    expected += 0.7 * (weights / weights.sum() - [0, 1])
    captured = []
    monkeypatch.setattr(
        learner.online, "apply", lambda gradients, rate: captured.extend(gradients)
    )
    learner.update([row], 0.001, 0.7)
    np.testing.assert_allclose(captured[-1], expected)


def test_bounded_replay_mixing_pretraining_period_and_checkpoint_continuation():
    policy = artifact()
    offline = [transition(objectives={"engagement": 0.8, "miss": 0})]
    learner = ConstrainedConservativeDoubleDQN(policy, seed=12, offline=offline)
    learner.pretrain(2)
    assert learner.online.updates == 2
    learner.learn([transition()], day=0)
    learner.learn([transition(state=[0.7])], day=0)
    assert learner.online.updates == 2
    learner.learn([], day=1)
    assert learner.online.updates == 4
    learner.store([transition(state=[0.8]), transition(state=[0.9])])
    assert len(learner.buffer) == 3
    assert learner.buffer[0]["state"] == [0.9]
    assert learner._draw()[0]["objectives"]["engagement"] == 0.8
    checkpoint = json.loads(json.dumps(learner.checkpoint()))
    restored = ConstrainedConservativeDoubleDQN(policy, seed=999, offline=offline)
    restored.restore(checkpoint)
    restored.buffer = copy.deepcopy(learner.buffer)
    restored.write = learner.write
    row = transition(state=[0.6])
    learner.learn([row], day=2)
    restored.learn([row], day=2)
    assert learner.checkpoint() == restored.checkpoint()
    assert learner.revision == restored.revision
    empty = ConstrainedConservativeDoubleDQN(policy)
    empty.pretrain(3)
    assert empty.online.updates == 0
    empty.store([transition(), transition()])
    assert len(empty._draw()) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"state": [np.nan]},
        {"next_state": [1, 2]},
        {"safe_mask": [True]},
        {"safe_mask": [2, 1]},
        {"action": 2},
        {"safe_mask": [True, False]},
        {"bootstrap": 0.5},
        {"next_safe_mask": [False, False]},
        {"elapsed_min": -1},
        {"elapsed_min": np.inf},
        {"objectives": {"engagement": np.nan, "miss": 0}},
    ],
)
def test_replay_rejects_invalid_states_actions_rewards_and_durations(change):
    with pytest.raises(ValueError):
        validate_transition(transition(**change), 1)


@pytest.mark.parametrize(
    "settings",
    [
        {"batch_size": 0},
        {"batch_size": True},
        {"batch_size": 2.5},
        {"lr_online": 0},
        {"lr_offline": 0},
        {"cql_alpha_online": -1},
        {"discount_half_life_min": 0},
        {"weight_miss": np.nan},
        {"offline_batch_share": 1.1},
        {"replay_capacity": 1},
    ],
)
def test_invalid_training_configuration_fails_closed(settings):
    with pytest.raises(ValueError):
        TrainingConfig.from_metadata(settings)


@pytest.mark.parametrize(
    "key,value",
    [
        ("format_version", 99),
        ("base_version", "another"),
        ("config_sha256", "other"),
        ("online", []),
        ("online", [[[1, 2, 3]], [0, 0]]),
        ("moment", [[[np.nan, 0]], [0, 0]]),
        ("velocity", [[[-1, 0]], [0, 0]]),
        ("updates", -1),
        ("refit_day", -1),
        ("owed", 0.5),
    ],
)
def test_checkpoint_rejects_incompatibility_and_corruption(key, value):
    learner = ConstrainedConservativeDoubleDQN(artifact())
    checkpoint = learner.checkpoint()
    checkpoint[key] = value
    with pytest.raises(ValueError):
        learner.restore(checkpoint)


def test_nonfinite_gradient_is_rejected_before_weight_update(monkeypatch):
    learner = ConstrainedConservativeDoubleDQN(artifact())
    monkeypatch.setattr(
        learner.online, "backward", lambda *args: [np.asarray([np.nan])]
    )
    with pytest.raises(ValueError, match="gradient"):
        learner.update([transition()], 0.001, 0)
    assert learner.online.updates == 0


def test_viability_accounts_for_blackout_remaining_after_skip():
    # At 15:00 a send at 14:45 blocks delivery until 15:45; a 15:30 reminder is impossible.
    with pytest.raises(ValueError, match="No safe action"):
        safe_actions(
            (QueueItem("reminder", 930),), 900, 15, 0, 0, 15, 60, 22, 7, 1, 0, 4, 28, 0
        )


def test_multi_layer_updates_match_original_constrained_cddqn_reference():
    reference = json.loads(
        (Path(__file__).parent / "fixtures/con_cddqn_source_parity.json").read_text()
    )
    parameters = [np.asarray(row) for row in reference["initial"]]
    policy = PolicyArtifact(
        dict(
            fields=["a", "b", "c"],
            device="nonwearer",
            sample_sha256="b" * 64,
            target_copy_interval=2,
        ),
        parameters[::2],
        parameters[1::2],
    )
    learner = ConstrainedConservativeDoubleDQN(policy)
    for target, source in zip(learner.target.parameters(), reference["initial_target"]):
        target[...] = source
    for expected in reference["expected"]:
        diagnostics = learner.update(reference["rows"], 0.0001, 1.0)
        for name, actual in (
            ("online", learner.online.parameters()),
            ("target", learner.target.parameters()),
            ("moment", learner.online.moment),
            ("velocity", learner.online.velocity),
        ):
            for value, target in zip(actual, expected[name]):
                np.testing.assert_allclose(value, target, rtol=1e-13, atol=1e-15)
        assert diagnostics["td_error"] == pytest.approx(expected["diagnostics"][0])
