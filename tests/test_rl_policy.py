import json
from pathlib import Path

import numpy as np
import pytest

from app.schemas.selected_content import ContentItem
from app.services import rl_policy
from app.services.rl_policy import (
    InfeasibleQueueError,
    PolicyArtifact,
    QueueItem,
    build_observation,
    decide_epoch,
    load_policy_artifacts,
    plan_daily_queue,
    policy_kind,
    safe_actions,
    state_fields,
)


def test_resources_share_recommendation_state_and_deadline_behavior():
    assert policy_kind("resource") == policy_kind("recommendation")
    original = (QueueItem("recommendation", 1319),)
    resource = (QueueItem("resource", 1319),)
    args = (900, 240, 0, 0, 15, 60, 22, 7, 1, 1, 4, 28, 0)
    assert safe_actions(original, *args) == safe_actions(resource, *args)
    first = build_observation(900, 0, 0, 0, 4, 28, 240, 0, [], [], None, original)
    second = build_observation(900, 0, 0, 0, 4, 28, 240, 0, [], [], None, resource)
    np.testing.assert_array_equal(first, second)


def test_selected_reminder_derives_deadline_from_mission_timing():
    reminder = ContentItem(id="R1", type="reminder", mission_id="M1")
    assert reminder.deadline is None
    with pytest.raises(ValueError, match="Artemis steps or mission end times"):
        ContentItem(
            id="R1", type="reminder", mission_id="M1", deadline="2026-07-22T12:00:00"
        )
    with pytest.raises(ValueError, match="Artemis steps or mission end times"):
        ContentItem(
            id="R1", type="reminder", mission_id="M1", deadline="2026-07-22T12:00:00Z"
        )


def test_reminder_deadline_forces_send_when_skip_would_be_late():
    reminder = (QueueItem("reminder", 900),)
    mask = safe_actions(reminder, 900, 240, 0, 0, 15, 60, 22, 7, 1, 1, 4, 28, 0)
    assert mask == (False, True)
    with pytest.raises(InfeasibleQueueError):
        safe_actions(reminder, 915, 240, 0, 0, 15, 60, 22, 7, 1, 1, 4, 28, 0)


def test_quiet_hours_and_daily_cap_restrict_send():
    items = (QueueItem("recommendation", 1319),)
    mask = safe_actions(items, 360, 240, 0, 0, 15, 60, 22, 7, 1, 1, 4, 28, 0)
    assert mask == (True, False)
    with pytest.raises(InfeasibleQueueError):
        safe_actions(items, 900, 240, 4, 4, 15, 60, 22, 7, 1, 1, 4, 28, 0)


def test_observation_matches_exported_field_widths():
    items = (QueueItem("resource", 1319), QueueItem("reminder", 1100))
    nonwearer = build_observation(
        900, 2, 1, 3, 4, 28, 180, 0.5, [True, False], [5, 20], 1100, items
    )
    wearer = build_observation(
        900,
        2,
        1,
        3,
        4,
        28,
        180,
        0.5,
        [True, False],
        [5, 20],
        1100,
        items,
        wearable={"wearable_present": 1, "steps": 0.5},
    )
    assert nonwearer.shape == (len(state_fields(False)),)
    assert wearer.shape == (len(state_fields(True)),)
    assert nonwearer[-8:-4].tolist() == [1, 0, 0, 1]


@pytest.mark.parametrize("device", ["nonwearer", "wearer"])
def test_trained_artifact_loads_and_respects_action_mask(device):
    stem = Path(__file__).resolve().parents[1] / "app" / "policy_artifacts"
    policy = PolicyArtifact.load(stem / f"con_cddqn_{device}")
    observation = np.zeros(len(policy.metadata["fields"]))
    assert np.isfinite(policy.action_values(observation)).all()
    assert policy.decide(observation, (True, False)) == 0
    assert policy.decide(observation, (False, True)) == 1
    with pytest.raises(InfeasibleQueueError):
        policy.decide(observation, (False, False))


def test_deployed_artifacts_load_for_both_device_states():
    policies = load_policy_artifacts()
    assert set(policies) == {"nonwearer", "wearer"}
    assert policies["nonwearer"].version != policies["wearer"].version


def test_artifact_rejects_unknown_device(tmp_path):
    source = Path(__file__).resolve().parents[1] / "app" / "policy_artifacts"
    metadata = json.loads((source / "con_cddqn_nonwearer.json").read_text())
    metadata["device"] = "unknown"
    (tmp_path / "invalid.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="Unsupported policy device"):
        PolicyArtifact.load(tmp_path / "invalid")


def test_epoch_decision_uses_full_reminder_queue():
    stem = Path(__file__).resolve().parents[1] / "app" / "policy_artifacts"
    policy = PolicyArtifact.load(stem / "con_cddqn_nonwearer")
    items = (QueueItem("resource", 1319), QueueItem("reminder", 960))
    decision = decide_epoch(policy, items, 900, 0, 0, 0, 240, 0, [], [], 960)
    assert decision.action == 1
    assert decision.safe_mask == (False, True)
    assert decision.policy_version.startswith("con_cddqn-nonwearer-")


def test_daily_runner_preserves_queue_order_and_reminder_deadline():
    policy = load_policy_artifacts()["nonwearer"]
    items = (QueueItem("resource", 1319), QueueItem("reminder", 960))
    decisions = plan_daily_queue(policy, items, 900, 0, 0, 0, 240, 0, [], [], 960)
    sends = [row for row in decisions if row.decision.action == 1]
    assert [(row.occurrence_index, row.minute) for row in sends] == [
        (0, 900),
        (1, 960),
    ]
    assert all(row.decision.policy_version == policy.version for row in decisions)


def test_daily_runner_reports_an_impossible_reminder_queue():
    policy = load_policy_artifacts()["nonwearer"]
    items = (QueueItem("recommendation", 1319), QueueItem("reminder", 950))
    with pytest.raises(InfeasibleQueueError):
        plan_daily_queue(policy, items, 900, 0, 0, 0, 240, 0, [], [], 950)


def test_daily_runner_caps_resources_with_recommendations():
    policy = load_policy_artifacts()["nonwearer"]
    items = (
        QueueItem("recommendation", 1319),
        QueueItem("resource", 1319),
        QueueItem("resource", 1319),
    )
    with pytest.raises(InfeasibleQueueError, match="class limit"):
        plan_daily_queue(policy, items, 900, 0, 0, 0, 240, 0, [], [], None)


def test_daily_runner_counts_prior_reminders_against_limit():
    policy = load_policy_artifacts()["nonwearer"]
    items = (QueueItem("reminder", 960), QueueItem("reminder", 1080))
    with pytest.raises(InfeasibleQueueError, match="class limit"):
        plan_daily_queue(
            policy,
            items,
            900,
            0,
            1,
            1,
            240,
            0,
            [],
            [],
            1080,
            sent_reminders=1,
        )


def test_policy_rejects_unknown_item_and_invalid_clock():
    with pytest.raises(ValueError, match="Unsupported content type"):
        policy_kind("unknown")
    with pytest.raises(ValueError, match="decision time"):
        build_observation(1440, 0, 0, 0, 4, 28, 0, 0, [], [], None, ())
    with pytest.raises(ValueError, match="decision time"):
        build_observation(900, 7, 0, 0, 4, 28, 0, 0, [], [], None, ())


def test_safe_actions_empty_queue_and_floor_constraints():
    settings = (15, 60, 22, 7, 1, 1, 4, 28, 0)
    assert safe_actions((), 900, 240, 0, 0, *settings) == (True, False)
    with pytest.raises(InfeasibleQueueError):
        safe_actions(
            (QueueItem("reminder", 900),), 900, 240, 0, 0, 15, 60, 22, 7, 2, 1, 4, 28, 0
        )
    with pytest.raises(InfeasibleQueueError):
        safe_actions(
            (QueueItem("reminder", 900),),
            900,
            240,
            0,
            0,
            15,
            60,
            22,
            7,
            1,
            28,
            4,
            28,
            6,
        )
    assert safe_actions(
        (QueueItem("recommendation", 1439),),
        1305,
        240,
        0,
        0,
        15,
        60,
        7,
        22,
        0,
        0,
        4,
        28,
        0,
    ) == (True, False)


@pytest.fixture
def artifact_files(tmp_path):
    source = Path(__file__).resolve().parents[1] / "app" / "policy_artifacts"
    metadata = json.loads((source / "con_cddqn_nonwearer.json").read_text())
    with np.load(source / "con_cddqn_nonwearer.npz") as archive:
        arrays = {key: archive[key].copy() for key in archive.files}

    def write(metadata_change=None, arrays_change=None):
        current_metadata = dict(metadata)
        current_metadata.pop("model_sha256", None)
        current_arrays = {key: value.copy() for key, value in arrays.items()}
        if metadata_change:
            current_metadata.update(metadata_change)
        if arrays_change:
            arrays_change(current_arrays)
            current_metadata.pop("model_sha256", None)
        (tmp_path / "policy.json").write_text(json.dumps(current_metadata))
        np.savez(tmp_path / "policy.npz", **current_arrays)
        return tmp_path / "policy"

    return write


@pytest.mark.parametrize(
    ("metadata_change", "error"),
    [
        ({"format_version": 2}, "format"),
        ({"fields": []}, "state fields"),
    ],
)
def test_artifact_rejects_metadata_mismatch(artifact_files, metadata_change, error):
    with pytest.raises(ValueError, match=error):
        PolicyArtifact.load(artifact_files(metadata_change))


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda arrays: arrays.clear(), "layers"),
        (lambda arrays: arrays.__setitem__("weight_0", np.zeros((2, 2))), "dimensions"),
        (lambda arrays: arrays["bias_0"].__setitem__(0, np.nan), "bias"),
        (lambda arrays: arrays.__setitem__("bias_1", np.zeros(3)), "dimensions"),
    ],
)
def test_artifact_rejects_invalid_network(artifact_files, change, error):
    with pytest.raises((ValueError, KeyError), match=error):
        PolicyArtifact.load(artifact_files(arrays_change=change))


def test_artifact_rejects_three_action_outputs(artifact_files):
    def three_outputs(arrays):
        arrays["weight_1"] = np.zeros((arrays["weight_1"].shape[0], 3))
        arrays["bias_1"] = np.zeros(3)

    with pytest.raises(ValueError, match="two action values"):
        PolicyArtifact.load(artifact_files(arrays_change=three_outputs))


def test_policy_decision_uses_q_values_and_checks_observation():
    metadata = {
        "device": "nonwearer",
        "sample_sha256": "a" * 64,
        "send_q_margin": 0.2,
        "fields": ["state"],
    }
    policy = PolicyArtifact(metadata, [np.asarray([[0.0, 1.0]])], [np.zeros(2)])
    assert policy.decide(np.asarray([1.0]), (True, True)) == 1
    assert policy.decide(np.asarray([0.0]), (True, True)) == 0
    with pytest.raises(ValueError, match="observation"):
        policy.action_values(np.asarray([np.nan]))
    with pytest.raises(ValueError, match="observation"):
        policy.action_values(np.asarray([0.0, 1.0]))


def test_policy_artifacts_reject_different_delivery_constraints(monkeypatch):
    policies = load_policy_artifacts()
    wearer = policies["wearer"]
    changed = PolicyArtifact(dict(wearer.metadata), wearer.weights, wearer.biases)
    changed.metadata["grid_min"] += 1
    monkeypatch.setattr(
        rl_policy.PolicyArtifact,
        "load",
        classmethod(
            lambda cls, stem: (
                changed
                if "wearer" in stem.name and "nonwearer" not in stem.name
                else policies["nonwearer"]
            )
        ),
    )
    rl_policy.load_policy_artifacts.cache_clear()
    with pytest.raises(ValueError, match="different delivery constraints"):
        rl_policy.load_policy_artifacts()
    rl_policy.load_policy_artifacts.cache_clear()


def test_epoch_rejects_wrong_wearable_artifact():
    policy = load_policy_artifacts()["wearer"]
    with pytest.raises(ValueError, match="Wearable state"):
        decide_epoch(policy, (), 900, 0, 0, 0, 240, 0, [], [], None)


def test_daily_runner_validates_grid_start_and_unfinished_queue(monkeypatch):
    policy = load_policy_artifacts()["nonwearer"]
    assert plan_daily_queue(policy, (), 900, 0, 0, 0, 240, 0, [], [], None) == []
    item = (QueueItem("recommendation", 1439),)
    invalid_grid = PolicyArtifact(dict(policy.metadata), policy.weights, policy.biases)
    invalid_grid.metadata["grid_min"] = 17
    with pytest.raises(ValueError, match="grid"):
        plan_daily_queue(invalid_grid, item, 900, 0, 0, 0, 240, 0, [], [], None)
    with pytest.raises(ValueError, match="outside the day"):
        plan_daily_queue(policy, item, 1440, 0, 0, 0, 240, 0, [], [], None)
    monkeypatch.setattr(
        rl_policy,
        "decide_epoch",
        lambda policy, items, *args, **kwargs: rl_policy.EpochDecision(
            0, np.zeros(len(policy.metadata["fields"])), (True, False), policy.version
        ),
    )
    with pytest.raises(InfeasibleQueueError, match="before midnight"):
        plan_daily_queue(policy, item, 1425, 0, 0, 0, 240, 0, [], [], None)


@pytest.mark.parametrize(
    "settings,error",
    [
        ({"grid_min": 17}, "grid"),
        ({"cap_per_day": 0}, "setting"),
        ({"send_q_margin": -1}, "setting"),
        ({"quiet_hour_end": 24}, "quiet hour"),
        ({"rating_weight": 2}, "rating weight"),
        ({"sample_sha256": "invalid"}, "sample checksum"),
        ({"hidden_layers": 8}, "architecture"),
    ],
)
def test_artifact_rejects_invalid_runtime_settings(artifact_files, settings, error):
    with pytest.raises(ValueError, match=error):
        PolicyArtifact.load(artifact_files(settings))


def test_finite_input_cannot_silently_produce_infinite_action_values():
    policy = PolicyArtifact({"fields": ["x"]}, [np.full((1, 2), 1e308)], [np.zeros(2)])
    with np.errstate(over="ignore"):
        with pytest.raises(ValueError, match="Nonfinite"):
            policy.action_values(np.asarray([1e308]))
