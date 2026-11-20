"""Run a repeatable synthetic learning check without a database or broker."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np

from app.rl.learner import ConstrainedConservativeDoubleDQN
from app.services.rl_policy import (
    PolicyArtifact,
    QueueItem,
    decide_epoch,
    load_policy_artifacts,
)


def parameter_digest(model) -> str:
    return hashlib.sha256(b"".join(p.tobytes() for p in model.parameters())).hexdigest()


def run_check(days: int = 14, seed: int = 11, mode: str = "online") -> tuple:
    """Exercise actual policy and gradient updates with explicitly artificial responses."""
    if days < 1 or mode not in {"online", "frozen"}:
        raise ValueError("Use positive days and online or frozen mode")
    artifact = load_policy_artifacts()["nonwearer"]
    learner = ConstrainedConservativeDoubleDQN(artifact, seed=seed)
    before = parameter_digest(learner.online)
    rng = np.random.default_rng(seed)
    config = artifact.metadata
    grid = int(config["grid_min"])
    window = float(config["response_window_min"])
    history, pending, daily = [], [], []
    last_send, sent_week, ema, violations = -10080, 0, 0.0, 0

    def release(now, day):
        nonlocal pending, ema
        ready = [p for p in pending if p["ready"] <= now and p["row"] is not None]
        for entry in ready:
            if entry["send"]:
                history.append((entry["opened"], entry["delay"]))
                ema = 0.85 * ema + 0.15 * entry["row"]["objectives"]["engagement"]
        if mode == "online":
            learner.learn([p["row"] for p in ready], day)
        pending = [
            p for p in pending if not (p["ready"] <= now and p["row"] is not None)
        ]

    for day in range(days):
        if day % 7 == 0:
            sent_week = 0
        items = (
            QueueItem("recommendation", 1319),
            QueueItem("reminder", 12 * 60),
            QueueItem("resource", 1319),
            QueueItem("reminder", 18 * 60),
        )
        sent_day = opened_day = in_window = decisions = 0
        reward_day, previous = 0.0, None
        release(day * 1440 + 420, day)
        for minute in range(420, 1320, grid):
            now = day * 1440 + minute
            release(now, day)
            metadata = dict(config)
            if mode == "online":
                metadata.update(learner_revision=learner.revision, learning_day=day)
            policy = PolicyArtifact(
                metadata, learner.online.weights, learner.online.biases
            )
            recent = history[-int(config["history_window"]) :]
            decision = decide_epoch(
                policy,
                items,
                minute,
                day % 7,
                sent_day,
                sent_week,
                now - last_send,
                ema,
                [h[0] for h in recent],
                [h[1] for h in recent if h[0]],
                next(
                    (item.deadline_minute for item in items if item.kind == "reminder"),
                    None,
                ),
                floor_day=4,
                floor_week=0,
                exploration_seed=int(rng.integers(2**63)),
            )
            if previous is not None:
                previous["row"] = dict(
                    state=previous["state"],
                    action=previous["action"],
                    safe_mask=previous["mask"],
                    next_state=decision.state.tolist(),
                    next_safe_mask=list(decision.safe_mask),
                    elapsed_min=grid,
                    bootstrap=1,
                    objectives=previous["objectives"],
                )
            decisions += 1
            action = decision.action
            violations += int(not decision.safe_mask[action])
            opened, delay, engagement = False, 0.0, 0.0
            if action:
                opportunity = 540 <= minute < 660 or 1020 <= minute < 1140
                # These probabilities are a software fixture, independent of pilot calibration.
                opened = bool(rng.random() < (0.6 if opportunity else 0.1))
                delay = float(rng.uniform(1, 20)) if opened else 0.0
                engagement = (
                    (1 - config["rating_weight"])
                    * np.exp(-config["delay_decay"] * delay)
                    if opened
                    else 0.0
                )
                violations += int(
                    now - last_send < config["blackout_min"]
                    or minute > items[0].deadline_minute
                    or sent_day >= config["cap_per_day"]
                    or sent_week >= config["cap_per_week"]
                )
                last_send = now
                sent_day += 1
                sent_week += 1
                opened_day += int(opened)
                in_window += int(opportunity)
                items = items[1:]
            objectives = {
                "engagement": float(engagement),
                "miss": -int(action and not opened),
            }
            reward_day += (
                config["weight_engagement"] * engagement
                + config["weight_miss"] * objectives["miss"]
            )
            previous = dict(
                state=decision.state.tolist(),
                action=action,
                mask=list(decision.safe_mask),
                objectives=objectives,
                ready=now + (window if action else grid),
                opened=opened,
                delay=delay,
                send=action,
                row=None,
            )
            pending.append(previous)
            if not items:
                previous["row"] = dict(
                    state=previous["state"],
                    action=action,
                    safe_mask=previous["mask"],
                    next_state=[0.0] * learner.width,
                    next_safe_mask=[False, False],
                    elapsed_min=grid,
                    bootstrap=0,
                    objectives=objectives,
                )
                break
        if items:
            raise RuntimeError("Synthetic daily queue did not complete")
        release((day + 1) * 1440, day)
        daily.append(
            dict(
                day=day + 1,
                mode=mode,
                decisions=decisions,
                sends=sent_day,
                opens=opened_day,
                in_window=in_window,
                fitting_reward=float(reward_day),
                gradient_updates=learner.online.updates,
                replay_size=len(learner.buffer),
                policy_revision=learner.revision,
            )
        )
    summary = dict(
        purpose="Software learning check with artificial response probabilities; no pilot calibration",
        mode=mode,
        days=days,
        seed=seed,
        artifact_version=artifact.version,
        gradient_updates=learner.online.updates,
        constraint_violations=violations,
        weights_changed=parameter_digest(learner.online) != before,
        initial_parameter_sha256=before,
        final_parameter_sha256=parameter_digest(learner.online),
        pending_transitions=len(pending),
        sends=sum(row["sends"] for row in daily),
    )
    return daily, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--mode", choices=("online", "frozen"), default="online")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    daily, summary = run_check(args.days, args.seed, args.mode)
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "daily.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(daily[0]))
        writer.writeheader()
        writer.writerows(daily)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
