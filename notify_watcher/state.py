"""Load and save the dedup state file.

state.json lives at the repo root and is committed back by the GitHub
Actions workflow after every run. Each topic owns one key inside it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"


def load() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
