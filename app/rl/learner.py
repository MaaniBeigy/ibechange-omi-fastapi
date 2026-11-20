"""Constrained conservative Double DQN with durable optimizer checkpoints.

SPDX-License-Identifier: GPL-3.0-only
Adapted from notifsim/agents/{ddqn,cddqn,constrained}.py; see NOTICE.md.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np

from app.rl.network import Network


@dataclass(frozen=True)
class TrainingConfig:
    """Selected ConCDDQN settings; versioned artifact metadata overrides them."""

    lr_offline: float = 0.001
    lr_online: float = 0.0001
    cql_alpha_offline: float = 5.0
    cql_alpha_online: float = 0.5
    batch_size: int = 32
    target_copy_interval: int = 100
    replay_capacity: int = 20000
    retrain_period_days: int = 1
    updates_per_epoch: int = 4
    offline_batch_share: float = 0.25
    discount_half_life_min: float = 1440.0
    weight_engagement: float = 1.0
    weight_miss: float = 0.15

    @classmethod
    def from_metadata(cls, metadata: dict) -> "TrainingConfig":
        values = {
            key: metadata[key] for key in cls.__dataclass_fields__ if key in metadata
        }
        config = cls(**values)
        integers = (
            "batch_size",
            "target_copy_interval",
            "replay_capacity",
            "retrain_period_days",
            "updates_per_epoch",
        )
        for key in integers:
            value = getattr(config, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Invalid training setting: {key}")
        for key, value in asdict(config).items():
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"Invalid training setting: {key}")
        if (
            config.lr_online <= 0
            or config.lr_offline <= 0
            or config.discount_half_life_min <= 0
            or config.offline_batch_share > 1
            or config.replay_capacity < config.batch_size
        ):
            raise ValueError("Invalid training rates, replay capacity or mixing share")
        return config

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


def validate_transition(row: dict, width: int) -> dict:
    """Reject corrupt replay and preserve exact serving feasibility sets."""
    result = dict(row)
    for key in ("state", "next_state"):
        value = np.asarray(row[key], dtype=float)
        if value.shape != (width,) or not np.isfinite(value).all():
            raise ValueError("Invalid replay state")
        result[key] = value.tolist()
    for key in ("safe_mask", "next_safe_mask"):
        value = np.asarray(row[key])
        if value.shape != (2,) or not np.isin(value, (False, True)).all():
            raise ValueError("Invalid replay action mask")
        result[key] = value.astype(bool).tolist()
    if row["action"] not in (0, 1) or not result["safe_mask"][row["action"]]:
        raise ValueError("Replay action is infeasible")
    if row["bootstrap"] not in (0, 1):
        raise ValueError("Invalid replay bootstrap")
    if row["bootstrap"] and not any(result["next_safe_mask"]):
        raise ValueError("Nonterminal replay has no next action")
    if not np.isfinite(row["elapsed_min"]) or row["elapsed_min"] < 0:
        raise ValueError("Invalid elapsed minutes")
    objectives = row["objectives"]
    if not all(np.isfinite(objectives[key]) for key in ("engagement", "miss")):
        raise ValueError("Invalid replay reward")
    return result


class ConstrainedConservativeDoubleDQN:
    """Fit engagement plus unopened-delivery cost over stored safe actions."""

    def __init__(self, artifact, seed: int = 0, offline=None):
        self.config = TrainingConfig.from_metadata(artifact.metadata)
        self.base_version = artifact.version
        self.width = len(artifact.metadata["fields"])
        self.rng = np.random.default_rng(seed)
        hidden = artifact.weights[0].shape[1]
        self.online = Network(self.width, hidden, self.rng, len(artifact.weights) - 1)
        self.target = Network(self.width, hidden, self.rng, len(artifact.weights) - 1)
        for target, source in zip(
            self.online.parameters(),
            [
                array
                for pair in zip(artifact.weights, artifact.biases)
                for array in pair
            ],
        ):
            target[...] = source
        self.online.copy_into(self.target)
        self.offline = (
            artifact.offline
            if offline is None
            else [validate_transition(row, self.width) for row in offline]
        )
        self.buffer = []
        self.replay_count = 0
        self.write = 0
        self.refit_day = 0
        self.owed = 0
        self.offline_source = "shared_device" if self.offline else "none"

    def store(self, rows: list) -> None:
        self._require_replay()
        for row in [validate_transition(source, self.width) for source in rows]:
            self.replay_count += 1
            if len(self.buffer) < self.config.replay_capacity:
                self.buffer.append(row)
            else:
                self.buffer[self.write] = row
                self.write = (self.write + 1) % self.config.replay_capacity

    def _draw(self) -> list:
        size = self.config.batch_size
        old_count = (
            int(round(self.config.offline_batch_share * size)) if self.offline else 0
        )
        old = self.rng.integers(len(self.offline), size=old_count) if old_count else []
        fresh = self.rng.integers(len(self.buffer), size=size - old_count)
        return [self.offline[i] for i in old] + [self.buffer[i] for i in fresh]

    def pretrain(self, steps: int) -> None:
        """Fit an optional exported behavior log using the source offline rate."""
        if not self.offline:
            return
        for _ in range(steps):
            indexes = self.rng.integers(len(self.offline), size=self.config.batch_size)
            self.update(
                [self.offline[i] for i in indexes],
                self.config.lr_offline,
                self.config.cql_alpha_offline,
            )

    def refit_due(self, count: int, day: int) -> bool:
        return (
            count >= self.config.batch_size
            and day - self.refit_day >= self.config.retrain_period_days
        )

    def learn(self, rows: list, day: int) -> None:
        """Accumulate source update credits, keeping the policy fixed between refits."""
        self.store(rows)
        self.owed += self.config.updates_per_epoch * len(rows)
        if not self.refit_due(len(self.buffer), day):
            return
        self.refit_day = day
        for _ in range(self.owed):
            self.update(
                self._draw(), self.config.lr_online, self.config.cql_alpha_online
            )
        self.owed = 0

    def update(self, rows: list, rate: float, alpha: float) -> dict:
        """Source Double DQN target, elapsed-time discount and safe-set CQL gradient."""
        rows = [validate_transition(row, self.width) for row in rows]
        state = np.asarray([row["state"] for row in rows])
        next_state = np.asarray([row["next_state"] for row in rows])
        mask = np.asarray([row["safe_mask"] for row in rows])
        next_mask = np.asarray([row["next_safe_mask"] for row in rows])
        action = np.asarray([row["action"] for row in rows], dtype=int)
        reward = np.asarray(
            [
                self.config.weight_engagement * row["objectives"]["engagement"]
                + self.config.weight_miss * row["objectives"]["miss"]
                for row in rows
            ]
        )
        bootstrap = np.asarray([row["bootstrap"] for row in rows])
        discount = np.exp(
            -np.log(2.0)
            * np.asarray([row["elapsed_min"] for row in rows])
            / self.config.discount_half_life_min
        )
        # Terminal rows need no next feasible action; their bootstrap is zero.
        ahead = np.where(next_mask, self.online.values(next_state), -np.inf)
        best = np.argmax(ahead, axis=1)
        scored = self.target.values(next_state)[np.arange(len(rows)), best]
        target = reward + bootstrap * discount * scored
        predicted, hidden = self.online.forward(state)
        taken = np.zeros_like(predicted)
        taken[np.arange(len(rows)), action] = 1.0
        residual = predicted[np.arange(len(rows)), action] - target
        upstream = 2.0 * (predicted - target[:, None]) * taken / len(rows)
        if alpha:
            shifted = np.where(mask, predicted, -np.inf)
            shifted -= shifted.max(axis=1, keepdims=True)
            weights = np.exp(shifted) * mask
            upstream += (
                alpha
                * (weights / weights.sum(axis=1, keepdims=True) - taken)
                / len(rows)
            )
        gradients = self.online.backward(state, hidden, upstream)
        if not all(np.isfinite(gradient).all() for gradient in gradients):
            raise ValueError("Nonfinite policy gradient")
        self.online.apply(gradients, rate)
        if self.online.updates % self.config.target_copy_interval == 0:
            self.online.copy_into(self.target)
        return {"td_error": float(np.abs(residual).mean()), "targets": target.tolist()}

    def checkpoint(self) -> dict:
        """Replay is stored in the transition ledger, independently of this checkpoint."""
        return {
            "format_version": 2,
            "base_version": self.base_version,
            "config_sha256": self.config.digest,
            "online": [p.tolist() for p in self.online.parameters()],
            "target": [p.tolist() for p in self.target.parameters()],
            "moment": [p.tolist() for p in self.online.moment],
            "velocity": [p.tolist() for p in self.online.velocity],
            "updates": self.online.updates,
            "rng": self.rng.bit_generator.state,
            "refit_day": self.refit_day,
            "owed": self.owed,
            "replay_count": self.replay_count,
            "replay_write": self.write,
            "offline_source": self.offline_source,
        }

    def _require_replay(self):
        if len(self.buffer) != min(self.replay_count, self.config.replay_capacity):
            raise ValueError("Restore the personal replay ledger before continuation")

    def portable_checkpoint(self) -> dict:
        """Include retained personal replay in oldest-to-newest insertion order."""
        self._require_replay()
        return {
            "checkpoint": self.checkpoint(),
            "replay": self.buffer[self.write :] + self.buffer[: self.write],
        }

    def restore_portable(self, state: dict) -> None:
        self.restore(state["checkpoint"], replay=state["replay"])

    def restore(self, checkpoint: dict, *, replay: list | None = None) -> None:
        if (
            checkpoint.get("format_version") not in (1, 2)
            or checkpoint.get("base_version") != self.base_version
            or checkpoint.get("config_sha256") != self.config.digest
        ):
            raise ValueError("Incompatible policy checkpoint")
        parameters = []
        for key, destination in (
            ("online", self.online.parameters()),
            ("target", self.target.parameters()),
            ("moment", self.online.moment),
            ("velocity", self.online.velocity),
        ):
            arrays = checkpoint[key]
            if len(arrays) != len(destination):
                raise ValueError("Invalid checkpoint arrays")
            for target, source in zip(destination, arrays):
                value = np.asarray(source, dtype=float)
                if value.shape != target.shape or not np.isfinite(value).all():
                    raise ValueError("Invalid checkpoint dimensions")
                if key == "velocity" and (value < 0).any():
                    raise ValueError("Invalid Adam variance")
                parameters.append((target, value))
        for key in ("updates", "refit_day", "owed"):
            if type(checkpoint.get(key)) is not int or checkpoint[key] < 0:
                raise ValueError("Invalid checkpoint counter")
        write = checkpoint.get("replay_write", 0)
        capacity = self.config.replay_capacity
        if type(write) is not int or not 0 <= write < capacity:
            raise ValueError("Invalid replay position")
        if checkpoint.get("format_version") == 1 and "replay_count" not in checkpoint:
            # Legacy live checkpoints store all applied and outstanding update credits.
            count, remainder = divmod(
                checkpoint["updates"] + checkpoint["owed"],
                self.config.updates_per_epoch,
            )
            if remainder:
                raise ValueError(
                    "Legacy checkpoint requires the migrated replay ledger"
                )
        else:
            count = checkpoint.get("replay_count")
        if (
            type(count) is not int
            or count < 0
            or write != max(0, count - capacity) % capacity
        ):
            raise ValueError("Checkpoint and replay count disagree")
        if checkpoint.get(
            "offline_source", self.offline_source
        ) != self.offline_source or checkpoint.get("participant_offline"):
            raise ValueError("Incompatible offline replay source")
        buffer = [] if replay is None else self._replay_ring(replay, count, write)
        # Validate everything before replacing the current training state.
        rng = np.random.default_rng(0)
        rng.bit_generator.state = checkpoint["rng"]
        for destination, value in parameters:
            destination[...] = value
        self.online.updates = checkpoint["updates"]
        self.refit_day = checkpoint["refit_day"]
        self.owed = checkpoint["owed"]
        self.rng = rng
        self.write = write
        self.replay_count = count
        self.buffer = buffer

    def _replay_ring(self, rows, count, write):
        if len(rows) > self.config.replay_capacity:
            raise ValueError("Replay exceeds configured capacity")
        if len(rows) != min(count, self.config.replay_capacity):
            raise ValueError("Checkpoint and replay ledger disagree")
        rows = [validate_transition(row, self.width) for row in rows]
        return rows[-write:] + rows[:-write] if write else rows

    def restore_replay(self, rows: list) -> None:
        """Rebuild the ring from the ledger's oldest-to-newest retained rows."""
        self.buffer = self._replay_ring(rows, self.replay_count, self.write)

    @property
    def revision(self) -> str:
        digest = hashlib.sha256(self.config.digest.encode())
        for parameter in self.online.parameters():
            digest.update(parameter.tobytes())
        return f"{self.online.updates}-{digest.hexdigest()[:12]}"
