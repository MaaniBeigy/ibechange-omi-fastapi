"""Atomically publish a complete local training checkpoint."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def save_checkpoint(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with NamedTemporaryFile(
            dir=path.parent, delete=False, mode="w", encoding="utf-8"
        ) as handle:
            temp = Path(handle.name)
            json.dump(state, handle, allow_nan=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
