"""Load and save the dedup state file.

state.json lives at the repo root and is committed back by the GitHub
Actions workflow after every run. Each topic owns one key inside it.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"


class CorruptStateError(RuntimeError):
    """Raised when state.json cannot be decoded safely."""


def _backup_corrupt_state(path: Path) -> Path:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.corrupt-{stamp}.{suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    return backup


def load() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        backup = _backup_corrupt_state(STATE_PATH)
        message = (
            f"state.json is corrupt ({exc}); preserved a copy at {backup}. "
            "Refusing to continue so state.save() cannot overwrite the last "
            "recoverable state."
        )
        log.error(message)
        raise CorruptStateError(message) from exc


def save(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
