"""Verify portable offline exports without installing the research simulator."""

import csv
import gzip
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from app.rl.replay import read_replay, write_replay
from app.services.rl_policy import PolicyArtifact, state_fields
from scripts import train_policy_artifacts as training


def logged_row(wearer=False, **changes):
    state = {field: 0.1 for field in state_fields(wearer)}
    state["week_remaining"] = 0.9
    following = dict(state, week_remaining=0.9 - 15 / 10080)
    result = dict(
        state=json.dumps(state),
        next_state=json.dumps(following),
        action="0",
        opened="0",
        reward="0",
        safe_mask="[1, 1]",
        next_safe_mask="[1, 1]",
        agent="observed",
        arm="A",
        scale="fielded",
    )
    result.update(changes)
    return result


def write_log(path, rows):
    with gzip.open(path, "wt", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def test_training_row_reconstructs_reward_elapsed_week_wrap_and_bootstrap():
    row = logged_row(action="1", reward="-0.15", bootstrap="0")
    rebuilt = training._training_row(row, state_fields(False), 1, 0.15)
    assert rebuilt["objectives"]["miss"] == -1
    assert rebuilt["bootstrap"] == 0
    assert rebuilt["elapsed_min"] == pytest.approx(15)
    state = json.loads(row["state"])
    following = json.loads(row["next_state"])
    state["week_remaining"] = 0.001
    following["week_remaining"] = 0.999
    row.update(
        state=json.dumps(state),
        next_state=json.dumps(following),
        action="0",
        reward="0",
    )
    rebuilt = training._training_row(row, state_fields(False), 1, 0.15)
    assert rebuilt["elapsed_min"] == pytest.approx(20.16)
    rebuilt = training._training_row(
        logged_row(action="1", opened="1", reward="0.3"), state_fields(False), 2, 0.15
    )
    assert rebuilt["objectives"]["engagement"] == 0.15
    with pytest.raises(ValueError, match="reward"):
        training._training_row(logged_row(reward="1"), state_fields(False), 1, 0.15)


def test_collect_filters_behavior_and_bounds_both_devices(tmp_path):
    path = tmp_path / "log.csv.gz"
    write_log(
        path, [logged_row(agent="ddqn"), logged_row(), logged_row(), logged_row(True)]
    )
    fields = {"nonwearer": state_fields(False), "wearer": state_fields(True)}
    rows, digest = training.collect_rows(
        path, fields, training.training_values({}), 1, 10
    )
    assert all(len(group) == 1 for group in rows.values())
    assert len(digest) == 64
    with pytest.raises(ValueError, match="Insufficient"):
        training.collect_rows(path, fields, training.training_values({}), 2, 1)
    with pytest.raises(ValueError, match="Insufficient"):
        training.collect_rows(path, fields, training.training_values({}), 2, 100)


def test_export_trains_and_loads_both_models_and_optional_offline_replay(tmp_path):
    log, run, out = tmp_path / "log.csv.gz", tmp_path / "run.json", tmp_path / "out"
    write_log(log, [logged_row(), logged_row(True)])
    run.write_text(
        json.dumps(
            dict(arms=["A"], scales=["fielded"], overrides={"agent.batch_size": 2})
        )
    )
    training.export(log, run, out, 1, 10, 3, 17)
    for device in ("wearer", "nonwearer"):
        policy = PolicyArtifact.load(out / f"con_cddqn_{device}")
        assert policy.metadata["discount_half_life_min"] == 2880
        assert policy.metadata["weight_miss"] == 0.15
        assert len(policy.offline) == 1
        assert np.isfinite(
            policy.action_values(np.zeros(len(policy.metadata["fields"])))
        ).all()
        assert "model-" in policy.version
        assert "config-" in policy.version
    # The immutable artifact's digest detects changes before serving them.
    stem = out / "con_cddqn_nonwearer"
    original = stem.with_suffix(".npz").read_bytes()
    stem.with_suffix(".npz").write_bytes(original + b"corrupt")
    with pytest.raises(ValueError, match="weight checksum"):
        PolicyArtifact.load(stem)
    stem.with_suffix(".npz").write_bytes(original)
    stem.with_suffix(".replay.npz").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="replay checksum"):
        PolicyArtifact.load(stem)


def test_export_rejects_unusable_limits_and_wrong_reward_arm(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        training.export(Path("missing"), Path("missing"), tmp_path, 0, 1, 1, 0)
    run = tmp_path / "run.json"
    run.write_text(json.dumps(dict(arms=["F"], scales=["fielded"])))
    with pytest.raises(ValueError, match="arm A"):
        training.export(Path("missing"), run, tmp_path, 1, 1, 1, 0)


def test_cli_forwards_training_configuration(monkeypatch, tmp_path):
    export = Mock()
    monkeypatch.setattr(training, "export", export)
    monkeypatch.setattr(
        sys,
        "argv",
        ["train", "--log", "log.gz", "--run", "run.json", "--out", str(tmp_path)],
    )
    training.main()
    export.assert_called_once_with(
        Path("log.gz"), Path("run.json"), tmp_path, 20000, 1000000, 2000, 0
    )


def test_replay_archive_rejects_corruption_and_invalid_masks(tmp_path):
    path = tmp_path / "replay.npz"
    row = training._training_row(logged_row(), state_fields(False), 1, 0.15)
    write_replay(path, [row], len(state_fields(False)))
    restored = read_replay(path, len(state_fields(False)))
    assert restored[0]["elapsed_min"] == pytest.approx(15)
    with np.load(path) as archive:
        arrays = dict(archive)
    np.savez(path, **dict(arrays, action=np.asarray([0, 1])))
    with pytest.raises(ValueError, match="lengths"):
        read_replay(path, len(state_fields(False)))
    np.savez(path, **dict(arrays, unused=np.zeros(1)))
    with pytest.raises(ValueError, match="fields"):
        read_replay(path, len(state_fields(False)))
    np.savez(path, **{key: value[:0] for key, value in arrays.items()})
    with pytest.raises(ValueError, match="lengths"):
        read_replay(path, len(state_fields(False)))
