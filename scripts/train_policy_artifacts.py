"""Export constrained conservative Double DQN models from simulator decisions."""

import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np

from app.rl.checkpoint import save_checkpoint
from app.rl.learner import ConstrainedConservativeDoubleDQN, TrainingConfig
from app.rl.network import Network
from app.rl.replay import write_replay
from app.services.rl_policy import PolicyArtifact, state_fields


def training_values(overrides: dict) -> dict:
    """Resolve the selected runtime settings without importing the simulator."""
    values = {f"agent.{key}": value for key, value in asdict(TrainingConfig()).items()}
    values.update(
        {
            "agent.grid_min": 15,
            "agent.blackout_min": 60.0,
            "agent.hidden_units": 64,
            "agent.hidden_layers": 1,
            "agent.history_window": 160,
            "agent.skip_prior": 0.0,
            "agent.con_cddqn_send_q_margin": 0.5,
            "agent.epsilon_online": 0.05,
            "agent.epsilon_final": 0.01,
            "agent.exploration_days": 90,
            "agent.weight_volume": 0.1,
            "agent.weight_pace": 0.1,
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
    row: dict,
    fields: tuple[str, ...],
    weight_engagement: float,
    weight_miss: float,
    source_values: dict | None = None,
) -> dict:
    """Rebuild a replay transition from one logged decision."""
    state = json.loads(row["state"])
    next_state = json.loads(row["next_state"])
    action = int(row["action"])
    opened = bool(int(row["opened"]))
    reward = float(row["reward"])
    arm = row.get("arm", "A")
    if arm not in {"A", "F"}:
        raise ValueError("Training rows must use arm A or F")
    source_values = source_values or training_values({})
    if arm == "F" and action:
        cap = int(source_values["policy.cap_per_day"])
        count = cap * (1 - state["day_budget_left"]) + 1
        if not np.isclose(count, round(count)) or not 1 <= round(count) <= cap:
            raise ValueError("Invalid delivery count in full-reward log")
        pace = round(count) / (cap * (cap + 1) / 2)
        reward += float(source_values["agent.weight_volume"])
        reward -= float(source_values["agent.weight_pace"]) * pace
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
    if not 0 <= objectives["engagement"] <= 1 + 1e-8:
        raise ValueError("Reconstructed engagement is outside its range")
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
    log_path: Path,
    fields_by_device: dict,
    values: dict,
    limit: int,
    scan_limit: int,
    cohorts: dict,
    seed: int,
) -> tuple[dict, dict]:
    """Sample across every source participant and their observed timeline by device."""
    participants = {device: {} for device in fields_by_device}
    for cohort, details in sorted(cohorts.items()):
        for index, wearer in enumerate(details["wearers"]):
            device = "wearer" if wearer else "nonwearer"
            participants[device][f"{cohort}/{index}"] = details["groups"][index]
    allocations, generators, samples, counts, day_ranges = {}, {}, {}, Counter(), {}
    for device, members in participants.items():
        if len(members) < 2 or limit < len(members):
            raise ValueError(
                "Each device needs multiple source participants and at least one sample per participant"
            )
        keys = list(members)
        np.random.default_rng(seed).shuffle(keys)
        quota, remainder = divmod(limit, len(keys))
        for index, key in enumerate(keys):
            allocations[key] = quota + int(index < remainder)
            participant_seed = int.from_bytes(
                hashlib.sha256(f"{seed}:{key}".encode()).digest()[:8], "big"
            )
            generators[key] = np.random.default_rng(participant_seed)
            samples[key] = []
    expected_device = {
        key: device for device, members in participants.items() for key in members
    }
    with gzip.open(log_path, "rt", encoding="utf-8") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            if scan_limit and index >= scan_limit:
                break
            if (
                row["agent"] != "observed"
                or row["arm"] not in {"A", "F"}
                or row["scale"] != "fielded"
            ):
                continue
            key = f"{row['cell']}/{row['replicate']}/{row['participant']}"
            if key not in allocations:
                raise ValueError("Behavior participant is missing from run cohorts")
            device = "wearer" if '"wearable_present"' in row["state"] else "nonwearer"
            if device != expected_device[key]:
                raise ValueError("Behavior device differs from run cohorts")
            counts[key] += 1
            day = int(row["day"])
            previous = day_ranges.get(key, (day, day))
            day_ranges[key] = (min(day, previous[0]), max(day, previous[1]))
            slot = (
                len(samples[key])
                if counts[key] <= allocations[key]
                else int(generators[key].integers(counts[key]))
            )
            if slot >= allocations[key]:
                continue
            if slot == len(samples[key]):
                samples[key].append(row)
            else:
                samples[key][slot] = row
    if any(len(samples[key]) != count for key, count in allocations.items()):
        raise ValueError(
            "Insufficient behavior transitions across the complete source cohort"
        )
    rows, provenance = {}, {}
    for device, members in participants.items():
        raw = [row for key in sorted(members) for row in samples[key]]
        digest = hashlib.sha256()
        for row in raw:
            digest.update(json.dumps(row, sort_keys=True).encode("utf-8"))
        rows[device] = [
            _training_row(
                row,
                fields_by_device[device],
                float(values["agent.weight_engagement"]),
                float(values["agent.weight_miss"]),
                values,
            )
            for row in raw
        ]
        provenance[device] = dict(
            method="participant_stratified_reservoir",
            seed=seed,
            source_participants=len(members),
            sample_sha256=digest.hexdigest(),
            behavior_profile_counts=dict(Counter(members.values())),
            participants=[
                dict(
                    source_key=key,
                    profile=members[key],
                    available_transitions=counts[key],
                    sampled_transitions=len(samples[key]),
                    observed_days=day_ranges[key],
                    sampled_days=sorted({int(row["day"]) for row in samples[key]}),
                )
                for key in sorted(members)
            ],
        )
    return rows, provenance


def export(
    log_path: Path,
    run_path: Path,
    output: Path,
    limit: int,
    scan_limit: int,
    steps: int,
    seed: int,
    configuration: Path | None = None,
    checkpoint_dir: Path | None = None,
    checkpoint_steps: int = 100,
) -> None:
    """Fit the source agent and write versioned NumPy weight files."""
    if min(limit, steps, checkpoint_steps) < 1 or scan_limit < 0:
        raise ValueError(
            "Training limits and steps must be positive; scan limit may be zero"
        )
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run["arms"] not in (["A"], ["F"]) or run["scales"] != ["fielded"]:
        raise ValueError("Training log must use arm A or F at fielded scale")
    source_values = training_values(run.get("overrides", {}))
    cells = run.get("cell_parameters", {})
    if len(cells) > 1:
        raise ValueError("Export one simulator configuration at a time")
    if cells:
        source_values.update(next(iter(cells.values())))
    values = dict(source_values)
    if configuration is not None:
        values.update(json.loads(configuration.read_text(encoding="utf-8")))
    for key in (
        "agent.grid_min",
        "agent.blackout_min",
        "policy.cap_per_day",
        "policy.cap_per_week",
        "agent.history_window",
    ):
        if values[key] != source_values[key]:
            raise ValueError(f"Source observations or masks use a different {key}")
    fields_by_device = {
        "nonwearer": state_fields(False),
        "wearer": state_fields(True),
    }
    rows_by_device, provenance = collect_rows(
        log_path,
        fields_by_device,
        source_values,
        limit,
        scan_limit,
        run.get("cohorts", {}),
        seed,
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
            "sample_sha256": provenance[device]["sample_sha256"],
            "source_run_sha256": hashlib.sha256(run_path.read_bytes()).hexdigest(),
            "source_reward_arm": run["arms"][0],
            "offline_replay_scope": "shared_device",
            "source_participants": provenance[device]["source_participants"],
            "source_behavior_profile_counts": provenance[device][
                "behavior_profile_counts"
            ],
            "sampling_method": provenance[device]["method"],
            "scan_limit": scan_limit,
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
            "epsilon_online": values["agent.epsilon_online"],
            "epsilon_final": values["agent.epsilon_final"],
            "exploration_days": values["agent.exploration_days"],
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
        if checkpoint_dir is None:
            agent.pretrain(steps)
        else:
            path = checkpoint_dir / f"{device}.json"
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved["skip_prior"] != values["agent.skip_prior"]:
                    raise ValueError("Pretraining skip prior differs from checkpoint")
                agent.restore(saved["checkpoint"])
                if agent.online.updates > steps:
                    raise ValueError("Checkpoint exceeds requested pretraining steps")
            while agent.online.updates < steps:
                agent.pretrain(min(checkpoint_steps, steps - agent.online.updates))
                save_checkpoint(
                    path,
                    dict(
                        checkpoint=agent.checkpoint(),
                        skip_prior=values["agent.skip_prior"],
                    ),
                )
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
        stem.with_suffix(".sampling.json").write_text(
            json.dumps(provenance[device], indent=2) + "\n", encoding="utf-8"
        )
        metadata["sampling_sha256"] = hashlib.sha256(
            stem.with_suffix(".sampling.json").read_bytes()
        ).hexdigest()
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
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=0,
        help="Maximum log rows to inspect; zero reads the complete log",
    )
    parser.add_argument("--steps", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--checkpoint-dir", type=Path, help="Save and resume the two offline optimizers"
    )
    parser.add_argument("--checkpoint-steps", type=int, default=100)
    args = parser.parse_args()
    export(
        args.log,
        args.run,
        args.out,
        args.limit,
        args.scan_limit,
        args.steps,
        args.seed,
        args.config,
        args.checkpoint_dir,
        args.checkpoint_steps,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
