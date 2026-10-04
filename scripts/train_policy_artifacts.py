"""Export constrained conservative Double DQN models from simulator decisions."""

import argparse
import csv
import gzip
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from app.rl.learner import ConstrainedConservativeDoubleDQN, TrainingConfig
from app.rl.network import Network
from app.rl.replay import write_replay
from app.services.rl_policy import PolicyArtifact, state_fields


def training_values(overrides: dict) -> dict:
    """Resolve the inspected source registry's runtime subset without notifsim."""
    values = {f"agent.{key}": value for key, value in asdict(TrainingConfig()).items()}
    values.update(
        {
            "agent.grid_min": 15,
            "agent.blackout_min": 60.0,
            "agent.hidden_units": 32,
            "agent.hidden_layers": 1,
            "agent.history_window": 20,
            "agent.skip_prior": 0.0,
            "agent.con_cddqn_send_q_margin": 0.0,
            "policy.quiet_hour_start": 22,
            "policy.quiet_hour_end": 7,
            "policy.floor_per_day": 2,
            "policy.floor_per_week": 14,
            "policy.cap_per_day": 4,
            "policy.cap_per_week": 28,
            "engagement.window_min": 60.0,
            "engagement.delay_decay": 0.15,
            "engagement.rating_weight": 0.1,
        }
    )
    values.update(overrides)
    return values


def _training_row(
    row: dict, fields: tuple[str, ...], weight_engagement: float, weight_miss: float
) -> dict:
    """Rebuild a replay transition from one logged decision."""
    state = json.loads(row["state"])
    next_state = json.loads(row["next_state"])
    action = int(row["action"])
    opened = bool(int(row["opened"]))
    reward = float(row["reward"])
    objectives = {
        "engagement": (
            reward / weight_engagement
            if action and opened and weight_engagement
            else 0.0
        ),
        "volume": 0.0,
        "pace": 0.0,
        "miss": -1.0 if action and not opened else 0.0,
    }
    reconstructed = (
        weight_engagement * objectives["engagement"] + weight_miss * objectives["miss"]
    )
    if not np.isclose(reward, reconstructed):
        raise ValueError("Decision reward does not match engagement and miss")
    elapsed = float(
        row.get("elapsed_min")
        or (state["week_remaining"] - next_state["week_remaining"]) * 10080.0
    )
    if elapsed <= 0:
        elapsed += 10080.0
    return {
        "state": np.asarray([state[field] for field in fields], dtype=float),
        "next_state": np.asarray([next_state[field] for field in fields], dtype=float),
        "action": action,
        "reward": reward,
        "objectives": objectives,
        "elapsed_min": elapsed,
        "bootstrap": float(
            1.0 if row.get("bootstrap") in (None, "") else row["bootstrap"]
        ),
        "safe_mask": np.asarray(json.loads(row["safe_mask"]), dtype=bool),
        "next_safe_mask": np.asarray(json.loads(row["next_safe_mask"]), dtype=bool),
    }


def collect_rows(
    log_path: Path, fields_by_device: dict, values: dict, limit: int, scan_limit: int
) -> tuple[dict, str]:
    """Collect source behavior transitions for both state widths."""
    rows = {name: [] for name in fields_by_device}
    digest = hashlib.sha256()
    with gzip.open(log_path, "rt", encoding="utf-8") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            if index >= scan_limit:
                break
            if (row["agent"], row["arm"], row["scale"]) != (
                "observed",
                "A",
                "fielded",
            ):
                continue
            state = json.loads(row["state"])
            device = "wearer" if "wearable_present" in state else "nonwearer"
            if len(rows[device]) >= limit:
                continue
            fields = fields_by_device[device]
            rows[device].append(
                _training_row(
                    row,
                    fields,
                    float(values["agent.weight_engagement"]),
                    float(values["agent.weight_miss"]),
                )
            )
            digest.update(json.dumps(row, sort_keys=True).encode("utf-8"))
            if all(len(items) >= limit for items in rows.values()):
                break
    if any(len(items) < limit for items in rows.values()):
        raise ValueError(
            f"Insufficient behavior transitions: {dict((k, len(v)) for k, v in rows.items())}"
        )
    return rows, digest.hexdigest()


def export(
    log_path: Path,
    run_path: Path,
    output: Path,
    limit: int,
    scan_limit: int,
    steps: int,
    seed: int,
) -> None:
    """Fit the source agent and write versioned NumPy weight files."""
    if min(limit, scan_limit, steps) < 1:
        raise ValueError("Training limits and steps must be positive")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run["arms"] != ["A"] or run["scales"] != ["fielded"]:
        raise ValueError("Training log must use the engagement-plus-miss arm A")
    values = training_values(run["overrides"])
    fields_by_device = {
        "nonwearer": state_fields(False),
        "wearer": state_fields(True),
    }
    rows_by_device, digest = collect_rows(
        log_path, fields_by_device, values, limit, scan_limit
    )
    output.mkdir(parents=True, exist_ok=True)
    for device, rows in rows_by_device.items():
        fields = fields_by_device[device]
        metadata = {
            "format_version": 1,
            "policy": "con_cddqn",
            "device": device,
            "fields": fields,
            "source_decisions": str(log_path),
            "source_run": str(run_path),
            "sample_sha256": digest,
            "seed": seed,
            "transitions": len(rows),
            "pretrain_steps": steps,
            "cql_alpha_offline": values["agent.cql_alpha_offline"],
            "cql_alpha_online": values["agent.cql_alpha_online"],
            "lr_offline": values["agent.lr_offline"],
            "lr_online": values["agent.lr_online"],
            "hidden_layers": values["agent.hidden_layers"],
            "hidden_units": values["agent.hidden_units"],
            "batch_size": values["agent.batch_size"],
            "send_q_margin": values["agent.con_cddqn_send_q_margin"],
            "grid_min": values["agent.grid_min"],
            "blackout_min": values["agent.blackout_min"],
            "quiet_hour_start": values["policy.quiet_hour_start"],
            "quiet_hour_end": values["policy.quiet_hour_end"],
            "floor_per_day": values["policy.floor_per_day"],
            "floor_per_week": values["policy.floor_per_week"],
            "cap_per_day": values["policy.cap_per_day"],
            "cap_per_week": values["policy.cap_per_week"],
            "history_window": values["agent.history_window"],
            "response_window_min": values["engagement.window_min"],
            "delay_decay": values["engagement.delay_decay"],
            "rating_weight": values["engagement.rating_weight"],
        }
        metadata.update(
            {key: values[f"agent.{key}"] for key in TrainingConfig.__dataclass_fields__}
        )
        metadata["send_q_margin"] = values["agent.con_cddqn_send_q_margin"]
        network = Network(
            len(fields),
            int(values["agent.hidden_units"]),
            np.random.default_rng(seed),
            int(values["agent.hidden_layers"]),
        )
        network.bias_out[0] += values["agent.skip_prior"]
        initial = PolicyArtifact(metadata, network.weights, network.biases)
        agent = ConstrainedConservativeDoubleDQN(initial, seed=seed, offline=rows)
        agent.rng = np.random.default_rng(seed)
        agent.pretrain(steps)
        # The source calls prefer_skip again after its offline phase.
        agent.online.bias_out[0] += values["agent.skip_prior"]
        weights = {
            f"weight_{index}": weight
            for index, weight in enumerate(agent.online.weights)
        }
        weights.update(
            {f"bias_{index}": bias for index, bias in enumerate(agent.online.biases)}
        )
        stem = output / f"con_cddqn_{device}"
        np.savez_compressed(stem.with_suffix(".npz"), **weights)
        metadata["model_sha256"] = hashlib.sha256(
            stem.with_suffix(".npz").read_bytes()
        ).hexdigest()
        write_replay(stem.with_suffix(".replay.npz"), rows, len(fields))
        metadata["offline_replay_sha256"] = hashlib.sha256(
            stem.with_suffix(".replay.npz").read_bytes()
        ).hexdigest()
        (output / f"con_cddqn_{device}.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )


def main() -> None:
    """Read command-line paths and export both state-width models."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--scan-limit", type=int, default=1000000)
    parser.add_argument("--steps", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    export(
        args.log, args.run, args.out, args.limit, args.scan_limit, args.steps, args.seed
    )


if __name__ == "__main__":  # pragma: no cover
    main()
