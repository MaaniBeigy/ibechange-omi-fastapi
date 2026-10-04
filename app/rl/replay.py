"""Portable, pickle-free behavior replay for optional offline/online mixing."""

from pathlib import Path

import numpy as np

from app.rl.learner import validate_transition


def write_replay(path: Path, rows: list, width: int) -> None:
    rows = [validate_transition(row, width) for row in rows]
    arrays = {
        key: np.asarray([row[key] for row in rows])
        for key in (
            "state",
            "next_state",
            "action",
            "safe_mask",
            "next_safe_mask",
            "elapsed_min",
            "bootstrap",
        )
    }
    for key in ("engagement", "miss"):
        arrays[key] = np.asarray([row["objectives"][key] for row in rows])
    np.savez_compressed(path, **arrays)


def read_replay(path: Path, width: int) -> list:
    with np.load(path, allow_pickle=False) as archive:
        arrays = {key: archive[key] for key in archive.files}
    keys = (
        "state",
        "next_state",
        "action",
        "safe_mask",
        "next_safe_mask",
        "elapsed_min",
        "bootstrap",
        "engagement",
        "miss",
    )
    if set(arrays) != set(keys):
        raise ValueError("Invalid offline replay fields")
    count = len(arrays["action"])
    if not count or any(len(arrays[key]) != count for key in keys):
        raise ValueError("Invalid offline replay lengths")
    rows = []
    for index in range(count):
        row = {
            key: arrays[key][index].tolist()
            for key in keys
            if key not in ("engagement", "miss")
        }
        row["objectives"] = {
            key: float(arrays[key][index]) for key in ("engagement", "miss")
        }
        rows.append(validate_transition(row, width))
    return rows
