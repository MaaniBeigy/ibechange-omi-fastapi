"""Inference and schedule constraints for exported notification policies."""

import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from app.rl.learner import TrainingConfig
from app.rl.replay import read_replay

CLOCK_FIELDS = (
    "sin_minute",
    "cos_minute",
    "sin_weekday",
    "cos_weekday",
    *(f"day_block_{index}" for index in range(8)),
)
BUDGET_FIELDS = (
    "day_budget_left",
    "week_budget_left",
    "day_remaining",
    "week_remaining",
)
HISTORY_FIELDS = (
    "since_last_log",
    "engagement_ema",
    "recent_open_rate",
    "recent_delay",
)
WEARABLE_FIELDS = (
    "wearable_present",
    "steps",
    "activity_score",
    "sleep_efficiency",
)
MISSION_FIELDS = (
    "mission_ahead",
    "sin_mission",
    "cos_mission",
    "mission_today",
)
QUEUE_FIELDS = tuple(
    f"queue_{position}_{kind}"
    for position in range(1, 5)
    for kind in ("recommendation", "reminder")
)
MIN_RECOMMENDATION_CLASS_PER_DAY = 1
MAX_RECOMMENDATION_CLASS_PER_DAY = 2
MIN_REMINDERS_PER_DAY = 1
MAX_REMINDERS_PER_DAY = 2


def state_fields(wearer: bool) -> tuple[str, ...]:
    """Return the input fields in the order used by the trained network."""
    context = WEARABLE_FIELDS if wearer else ()
    return (
        CLOCK_FIELDS
        + BUDGET_FIELDS
        + HISTORY_FIELDS
        + context
        + MISSION_FIELDS
        + QUEUE_FIELDS
    )


def policy_kind(content_type: str) -> str:
    """Map stored content types to the two policy item classes."""
    if content_type in {"recommendation", "resource"}:
        return "recommendation"
    if content_type == "reminder":
        return "reminder"
    raise ValueError(f"Unsupported content type: {content_type}")


def build_observation(
    minute: int,
    weekday: int,
    sent_day: int,
    sent_week: int,
    cap_day: int,
    cap_week: int,
    since_last: float,
    engagement_ema: float,
    recent_opens: list[bool],
    recent_delays: list[float],
    mission_minute: int | None,
    items: tuple["QueueItem", ...],
    wearable: dict[str, float] | None = None,
) -> np.ndarray:
    """Build the network state from measurements known at decision time."""
    if not 0 <= minute < 1440 or not 0 <= weekday < 7:
        raise ValueError("Invalid policy decision time")
    day_angle = 2 * math.pi * minute / 1440
    week_angle = 2 * math.pi * weekday / 7
    block = [float(minute // 180 == index) for index in range(8)]
    clock = [
        math.sin(day_angle),
        math.cos(day_angle),
        math.sin(week_angle),
        math.cos(week_angle),
        *block,
    ]
    budget = [
        max(min((cap_day - sent_day) / cap_day, 1), 0) if cap_day else 0,
        max(min((cap_week - sent_week) / cap_week, 1), 0) if cap_week else 0,
        1 - minute / 1440,
        1 - (weekday * 1440 + minute) / 10080,
    ]
    history = [
        math.log1p(min(max(since_last, 0), 10080)) / math.log1p(10080),
        max(min(engagement_ema, 1), 0),
        sum(recent_opens) / len(recent_opens) if recent_opens else 0,
        (sum(recent_delays) / len(recent_delays) if recent_delays else 60) / 60,
    ]
    context = (
        [float(wearable.get(field, 0)) for field in WEARABLE_FIELDS]
        if wearable is not None
        else []
    )
    if mission_minute is None:
        mission = [0, 0, 0, 0]
    else:
        angle = 2 * math.pi * mission_minute / 1440
        mission = [
            (mission_minute - minute) / 1440,
            math.sin(angle),
            math.cos(angle),
            1,
        ]
    queue = []
    for position in range(4):
        kind = items[position].kind if position < len(items) else None
        queue.extend((float(kind == "recommendation"), float(kind == "reminder")))
    return np.asarray(clock + budget + history + context + mission + queue, dtype=float)


class InfeasibleQueueError(ValueError):
    """Report that no valid completion exists for the supplied queue."""


@dataclass(frozen=True)
class QueueItem:
    """Record one ordered item and its last allowed minute of the day."""

    content_type: str
    deadline_minute: int

    @property
    def kind(self) -> str:
        """Return the class represented in the policy state."""
        return policy_kind(self.content_type)


def _awake(minute: int, quiet_start: int, quiet_end: int) -> bool:
    """Report whether the minute lies outside configured quiet hours."""
    hour = min(minute // 60, 23)
    if quiet_start > quiet_end:
        return quiet_end <= hour < quiet_start
    return hour < quiet_start or hour >= quiet_end


def _can_finish(
    items: tuple[QueueItem, ...],
    minute: int,
    sent_day: int,
    sent_week: int,
    spacing: int,
    grid: int,
    quiet_start: int,
    quiet_end: int,
    floor_day: int,
    floor_week: int,
    cap_day: int,
    cap_week: int,
    weekday: int,
) -> bool:
    """Check whether earliest allowed placement completes the ordered queue."""
    for item in items:
        while minute < 1440 and not _awake(minute, quiet_start, quiet_end):
            minute += grid
        if minute >= 1440 or minute > item.deadline_minute:
            return False
        if sent_day >= cap_day or sent_week >= cap_week:
            return False
        sent_day += 1
        sent_week += 1
        minute += spacing
    if sent_day < floor_day:
        return False
    days_left = 6 - weekday
    return sent_week + days_left * floor_day >= floor_week


def safe_actions(
    items: tuple[QueueItem, ...],
    minute: int,
    since_last: float,
    sent_day: int,
    sent_week: int,
    grid: int,
    blackout: int,
    quiet_start: int,
    quiet_end: int,
    floor_day: int,
    floor_week: int,
    cap_day: int,
    cap_week: int,
    weekday: int,
) -> tuple[bool, bool]:
    """Return safe skip and send actions for the head of the queue."""
    if not items:
        return True, False
    spacing = grid * max(math.ceil(blackout / grid), 1)
    can_send = (
        since_last >= blackout
        and _awake(minute, quiet_start, quiet_end)
        and minute <= items[0].deadline_minute
        and sent_day < cap_day
        and sent_week < cap_week
    )
    common = (
        spacing,
        grid,
        quiet_start,
        quiet_end,
        floor_day,
        floor_week,
        cap_day,
        cap_week,
        weekday,
    )
    remaining_blackout = max(blackout - since_last - grid, 0)
    earliest_skip = minute + grid + grid * math.ceil(remaining_blackout / grid)
    skip = _can_finish(items, earliest_skip, sent_day, sent_week, *common)
    send = can_send and _can_finish(
        items[1:], minute + spacing, sent_day + 1, sent_week + 1, *common
    )
    if not skip and not send:
        raise InfeasibleQueueError("No safe action completes the supplied queue")
    return skip, send


class PolicyArtifact:
    """Evaluate a versioned constrained conservative Double DQN weight file."""

    def __init__(
        self,
        metadata: dict,
        weights: list[np.ndarray],
        biases: list[np.ndarray],
        offline=None,
    ):
        self.metadata = metadata
        self.weights = weights
        self.biases = biases
        self.offline = offline or []

    @property
    def version(self) -> str:
        """Return the policy name and training-sample identifier."""
        base = (
            f"con_cddqn-{self.metadata['device']}-"
            f"{self.metadata['sample_sha256'][:12]}"
        )
        if "model_sha256" in self.metadata:
            base += f"-model-{self.metadata['model_sha256'][:12]}"
        configuration = {
            key: value
            for key, value in self.metadata.items()
            if key
            not in (
                "learner_revision",
                "source_decisions",
                "source_run",
                "runtime_training_defaults",
            )
        }
        config_hash = hashlib.sha256(
            json.dumps(configuration, sort_keys=True).encode()
        ).hexdigest()
        base += f"-config-{config_hash[:12]}"
        revision = self.metadata.get("learner_revision")
        return f"{base}-online-{revision}" if revision else base

    @classmethod
    def load(cls, stem: Path) -> "PolicyArtifact":
        """Load and validate exported metadata and network arrays."""
        metadata = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
        if metadata.get("format_version") != 1 or metadata.get("policy") != "con_cddqn":
            raise ValueError("Unsupported policy artifact format")
        if metadata.get("device") not in {"nonwearer", "wearer"}:
            raise ValueError("Unsupported policy device")
        sample_hash = metadata.get("sample_sha256", "")
        if (
            not isinstance(sample_hash, str)
            or len(sample_hash) != 64
            or any(char not in "0123456789abcdef" for char in sample_hash)
        ):
            raise ValueError("Invalid policy training sample checksum")
        expected = state_fields(metadata.get("device") == "wearer")
        if tuple(metadata.get("fields", ())) != expected:
            raise ValueError("Policy state fields do not match the runtime")
        TrainingConfig.from_metadata(metadata)
        grid = metadata.get("grid_min")
        if not isinstance(grid, int) or grid <= 0 or 1440 % grid:
            raise ValueError("Policy grid must divide one day")
        for key in (
            "cap_per_day",
            "cap_per_week",
            "history_window",
            "response_window_min",
        ):
            value = metadata.get(key)
            if (
                not isinstance(value, (int, float))
                or not np.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"Invalid policy setting: {key}")
        for key in (
            "blackout_min",
            "floor_per_day",
            "floor_per_week",
            "delay_decay",
            "send_q_margin",
        ):
            value = metadata.get(key)
            if (
                not isinstance(value, (int, float))
                or not np.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Invalid policy setting: {key}")
        for key in ("quiet_hour_start", "quiet_hour_end"):
            value = metadata.get(key)
            if not isinstance(value, int) or not 0 <= value < 24:
                raise ValueError(f"Invalid quiet hour: {key}")
        if not 0 <= metadata.get("rating_weight", -1) <= 1:
            raise ValueError("Invalid rating weight")
        model_path = stem.with_suffix(".npz")
        checksum = hashlib.sha256(model_path.read_bytes()).hexdigest()
        if metadata.get("model_sha256", checksum) != checksum:
            raise ValueError("Policy weight checksum mismatch")
        metadata["model_sha256"] = checksum
        with np.load(model_path, allow_pickle=False) as archive:
            layers = sorted(
                int(key.split("_")[1])
                for key in archive.files
                if key.startswith("weight_")
            )
            if layers != list(range(len(layers))) or not layers:
                raise ValueError("Invalid policy layers")
            weights = [archive[f"weight_{index}"].copy() for index in layers]
            biases = [archive[f"bias_{index}"].copy() for index in layers]
        width = len(expected)
        for weight, bias in zip(weights, biases):
            if (
                bias.ndim != 1
                or weight.shape != (width, bias.size)
                or not np.isfinite(weight).all()
            ):
                raise ValueError("Invalid policy network dimensions")
            if not np.isfinite(bias).all():
                raise ValueError("Invalid policy network bias")
            width = len(bias)
        if width != 2:
            raise ValueError("Policy network must return two action values")
        if metadata.get("hidden_layers", len(weights) - 1) != len(weights) - 1 or any(
            bias.size != metadata.get("hidden_units", bias.size) for bias in biases[:-1]
        ):
            raise ValueError("Policy architecture does not match metadata")
        offline = []
        if "offline_replay_sha256" in metadata:
            replay_path = stem.with_suffix(".replay.npz")
            if (
                hashlib.sha256(replay_path.read_bytes()).hexdigest()
                != metadata["offline_replay_sha256"]
            ):
                raise ValueError("Offline replay checksum mismatch")
            offline = read_replay(replay_path, len(expected))
        return cls(metadata, weights, biases, offline)

    def action_values(self, observation: np.ndarray) -> np.ndarray:
        """Return skip and send action values for one state vector."""
        output = np.asarray(observation, dtype=float)
        if (
            output.shape != (len(self.metadata["fields"]),)
            or not np.isfinite(output).all()
        ):
            raise ValueError("Invalid policy observation")
        for index, (weight, bias) in enumerate(zip(self.weights, self.biases)):
            output = output @ weight + bias
            if index < len(self.weights) - 1:
                output = np.maximum(output, 0)
        if not np.isfinite(output).all():
            raise ValueError("Nonfinite policy action values")
        return output

    def decide(self, observation: np.ndarray, mask: tuple[bool, bool]) -> int:
        """Choose a feasible action using the trained send margin."""
        if mask == (True, False):
            return 0
        if mask == (False, True):
            return 1
        if mask != (True, True):
            raise InfeasibleQueueError("No feasible policy action")
        values = self.action_values(observation)
        return int(values[1] - values[0] > self.metadata["send_q_margin"])


@lru_cache(maxsize=1)
def load_policy_artifacts() -> dict[str, PolicyArtifact]:
    """Load both deployed policies and check their network dimensions."""
    directory = Path(__file__).resolve().parents[1] / "policy_artifacts"
    policies = {
        device: PolicyArtifact.load(directory / f"con_cddqn_{device}")
        for device in ("nonwearer", "wearer")
    }
    constraint_keys = (
        "grid_min",
        "blackout_min",
        "quiet_hour_start",
        "quiet_hour_end",
        "cap_per_day",
        "cap_per_week",
    )
    if any(
        policies["nonwearer"].metadata[key] != policies["wearer"].metadata[key]
        for key in constraint_keys
    ):
        raise ValueError("Policy artifacts have different delivery constraints")
    return policies


@dataclass(frozen=True)
class EpochDecision:
    """Record one safe policy decision and the state that produced it."""

    action: int
    state: np.ndarray
    safe_mask: tuple[bool, bool]
    policy_version: str


@dataclass(frozen=True)
class ScheduledDecision:
    """Keep the selected queue position and local minute for one decision."""

    occurrence_index: int
    minute: int
    decision: EpochDecision


def decide_epoch(
    policy: PolicyArtifact,
    items: tuple[QueueItem, ...],
    minute: int,
    weekday: int,
    sent_day: int,
    sent_week: int,
    since_last: float,
    engagement_ema: float,
    recent_opens: list[bool],
    recent_delays: list[float],
    mission_minute: int | None,
    wearable: dict[str, float] | None = None,
    floor_day: int | None = None,
    floor_week: int | None = None,
) -> EpochDecision:
    """Choose a safe action using current measurements and the full queue."""
    config = policy.metadata
    if (wearable is None) != (config["device"] == "nonwearer"):
        raise ValueError("Wearable state does not match the policy artifact")
    mask = safe_actions(
        items,
        minute,
        since_last,
        sent_day,
        sent_week,
        int(config["grid_min"]),
        int(config["blackout_min"]),
        int(config["quiet_hour_start"]),
        int(config["quiet_hour_end"]),
        int(config["floor_per_day"] if floor_day is None else floor_day),
        int(config["floor_per_week"] if floor_week is None else floor_week),
        int(config["cap_per_day"]),
        int(config["cap_per_week"]),
        weekday,
    )
    state = build_observation(
        minute,
        weekday,
        sent_day,
        sent_week,
        int(config["cap_per_day"]),
        int(config["cap_per_week"]),
        since_last,
        engagement_ema,
        recent_opens,
        recent_delays,
        mission_minute,
        items,
        wearable,
    )
    return EpochDecision(policy.decide(state, mask), state, mask, policy.version)


def plan_daily_queue(
    policy: PolicyArtifact,
    items: tuple[QueueItem, ...],
    start_minute: int,
    weekday: int,
    sent_day: int,
    sent_week: int,
    since_last: float,
    engagement_ema: float,
    recent_opens: list[bool],
    recent_delays: list[float],
    mission_minute: int | None,
    wearable: dict[str, float] | None = None,
    sent_recommendation_class: int = 0,
    sent_reminders: int = 0,
) -> list[ScheduledDecision]:
    """Run decision epochs until the ordered daily queue is complete."""
    if not items:
        return []
    recommendation_count = sum(item.kind == "recommendation" for item in items)
    reminder_count = len(items) - recommendation_count
    if (
        sent_recommendation_class + recommendation_count
        > MAX_RECOMMENDATION_CLASS_PER_DAY
        or sent_reminders + reminder_count > MAX_REMINDERS_PER_DAY
    ):
        raise InfeasibleQueueError("Daily content class limit would be exceeded")
    grid = int(policy.metadata["grid_min"])
    if grid <= 0 or 1440 % grid:
        raise ValueError("Policy grid must divide one day")
    if not 0 <= start_minute < 1440:
        raise ValueError("Daily queue start is outside the day")
    minute = math.ceil(start_minute / grid) * grid
    remaining = items
    decisions = []
    while remaining and minute < 1440:
        decision = decide_epoch(
            policy,
            remaining,
            minute,
            weekday,
            sent_day,
            sent_week,
            since_last,
            engagement_ema,
            recent_opens,
            recent_delays,
            mission_minute,
            wearable,
        )
        decisions.append(
            ScheduledDecision(len(items) - len(remaining), minute, decision)
        )
        if decision.action == 1:
            remaining = remaining[1:]
            sent_day += 1
            sent_week += 1
            since_last = 0
        minute += grid
        since_last += grid
    if remaining:
        raise InfeasibleQueueError("Daily queue could not finish before midnight")
    return decisions
