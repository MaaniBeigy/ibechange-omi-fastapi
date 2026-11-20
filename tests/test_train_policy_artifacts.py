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
        cell="control",
        replicate="0",
        participant="2" if wearer else "0",
        day="0",
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


def source_cohorts():
    return {
        "control/0": {"groups": [0, 1, 2, 3], "wearers": [False, False, True, True]}
    }


def test_collect_covers_all_participants_and_later_days_reproducibly(tmp_path):
    path = tmp_path / "log.csv.gz"
    records = [logged_row(agent="ddqn")]
    for person in range(4):
        records.extend(
            logged_row(person >= 2, participant=str(person), day=str(day))
            for day in range(90)
        )
    write_log(path, records)
    fields = {"nonwearer": state_fields(False), "wearer": state_fields(True)}
    arguments = (
        path,
        fields,
        training.training_values({}),
        10,
        0,
        source_cohorts(),
        17,
    )
    rows, provenance = training.collect_rows(*arguments)
    assert all(len(group) == 10 for group in rows.values())
    assert training.collect_rows(*arguments)[1] == provenance
    for device in fields:
        assert provenance[device]["source_participants"] == 2
        assert len(provenance[device]["sample_sha256"]) == 64
        for person in provenance[device]["participants"]:
            assert person["available_transitions"] == 90
            assert person["sampled_transitions"] == 5
            assert person["observed_days"] == (0, 89)
            assert max(person["sampled_days"]) > 5
    with pytest.raises(ValueError, match="Insufficient"):
        training.collect_rows(
            path, fields, training.training_values({}), 10, 1, source_cohorts(), 17
        )
    with pytest.raises(ValueError, match="Insufficient"):
        training.collect_rows(
            path, fields, training.training_values({}), 1000, 0, source_cohorts(), 17
        )
    with pytest.raises(ValueError, match="multiple source participants"):
        training.collect_rows(
            path, fields, training.training_values({}), 1, 0, source_cohorts(), 17
        )
    write_log(path, [logged_row(participant="100")])
    with pytest.raises(ValueError, match="missing from run cohorts"):
        training.collect_rows(*arguments)
    write_log(path, [logged_row(True, participant="0")])
    with pytest.raises(ValueError, match="device differs"):
        training.collect_rows(*arguments)


def test_export_trains_and_loads_both_models_and_optional_offline_replay(tmp_path):
    log, run, out = tmp_path / "log.csv.gz", tmp_path / "run.json", tmp_path / "out"
    write_log(log, [logged_row(i >= 2, participant=str(i)) for i in range(4)])
    run.write_text(
        json.dumps(
            dict(
                arms=["A"],
                scales=["fielded"],
                overrides={"agent.batch_size": 2},
                cohorts=source_cohorts(),
            )
        )
    )
    training.export(log, run, out, 2, 10, 3, 17)
    for device in ("wearer", "nonwearer"):
        policy = PolicyArtifact.load(out / f"con_cddqn_{device}")
        assert policy.metadata["discount_half_life_min"] == 1440
        assert policy.metadata["weight_miss"] == 0.15
        assert len(policy.offline) == 2
        assert policy.metadata["source_participants"] == 2
        assert policy.metadata["offline_replay_scope"] == "shared_device"
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
    stem.with_suffix(".sampling.json").write_text("{}")
    with pytest.raises(ValueError, match="sampling checksum"):
        PolicyArtifact.load(stem)
    meta = json.loads(stem.with_suffix(".json").read_text())
    del meta["sampling_sha256"]
    del meta["offline_replay_sha256"]
    stem.with_suffix(".json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="replay archive is required"):
        PolicyArtifact.load(stem)


def test_export_rejects_unusable_limits_and_wrong_reward_arm(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        training.export(Path("missing"), Path("missing"), tmp_path, 0, 1, 1, 0)
    run = tmp_path / "run.json"
    run.write_text(json.dumps(dict(arms=["B"], scales=["fielded"])))
    with pytest.raises(ValueError, match="arm A or F"):
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
        Path("log.gz"), Path("run.json"), tmp_path, 20000, 0, 2000, 0, None, None, 100
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


def test_offline_export_continues_interrupted_optimizer(tmp_path, monkeypatch):
    log, run = tmp_path / "log.gz", tmp_path / "run.json"
    write_log(log, [logged_row(i >= 2, participant=str(i)) for i in range(4)])
    run.write_text(
        json.dumps(
            dict(
                arms=["A"],
                scales=["fielded"],
                overrides={"agent.batch_size": 2},
                cohorts=source_cohorts(),
            )
        )
    )
    training.export(log, run, tmp_path / "control", 2, 10, 5, 17)
    checkpoints = tmp_path / "checkpoint"
    save = training.save_checkpoint

    def interrupt(path, state):
        save(path, state)
        if path.stem == "wearer" and state["checkpoint"]["updates"] == 2:
            raise InterruptedError

    monkeypatch.setattr(training, "save_checkpoint", interrupt)
    args = (log, run, tmp_path / "resumed", 2, 10, 5, 17, None, checkpoints, 2)
    with pytest.raises(InterruptedError):
        training.export(*args)
    monkeypatch.setattr(training, "save_checkpoint", save)
    training.export(*args)
    for path in (tmp_path / "control").iterdir():
        assert path.read_bytes() == (tmp_path / "resumed" / path.name).read_bytes()
