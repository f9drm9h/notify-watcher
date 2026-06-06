"""Load the domain-monitor configuration (monitors.json).

monitors.json lives at the repo root next to watchlist.json. Where
watchlist.json holds the *entities* a user curates (movies, games, products),
monitors.json holds *domain monitoring policy*: which sources to read and how
to score what they return. It is plain text (no secrets), so it can be edited
directly on github.com.

A missing file, missing section, or malformed JSON yields an empty dict so a
typo never crashes a scheduled run — every caller already treats an empty
config as "nothing to do" and no-ops.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "monitors.json"


def load() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        log.warning("monitors.json not found at %s", CONFIG_PATH)
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("monitors.json is not valid JSON: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def section(name: str) -> dict[str, Any]:
    """Return one top-level object section, or {} if missing/not an object."""
    raw = load().get(name, {})
    return raw if isinstance(raw, dict) else {}
