"""Load the domain-monitor configuration (monitors.json).

monitors.json lives at the repo root next to watchlist.json. Where
watchlist.json holds the *entities* a user curates (movies, games, products),
monitors.json holds *domain monitoring policy*: which sources to read and how
to score what they return. It is plain text (no secrets), so it can be edited
directly on github.com.

A missing file, missing section, or malformed JSON yields an empty dict so a
typo never crashes a scheduled run — every caller already treats an empty
config as "nothing to do" and no-ops.

That fail-soft is right, but on its own it is also a silent failure, and the
August 2026 reliability audit called it out: an unparseable monitors.json turns
EVERY topic into "nothing configured", so the watcher goes completely quiet
while every run stays green and no topic ever records an error for the watchdog
to find. The config is edited straight on github.com by design, so a stray
comma is a realistic way to lose all notifications indefinitely.

:func:`last_error` is the fix. It reports why the most recent load came back
empty, and main.py pushes that as an alert before the topic loop runs.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "monitors.json"

# Why the last load() returned an empty/degraded config, or None if it was fine.
# Set on every call so it always describes the current state of the file.
_LAST_ERROR: Optional[str] = None


def last_error() -> Optional[str]:
    """Human reason the most recent :func:`load` could not read the config.

    None when the file parsed as a JSON object. Callers use this to alert on a
    config that silently disabled everything, rather than trusting an empty
    dict to mean "nothing to monitor".
    """
    return _LAST_ERROR


def load() -> dict[str, Any]:
    global _LAST_ERROR
    if not CONFIG_PATH.exists():
        log.warning("monitors.json not found at %s", CONFIG_PATH)
        _LAST_ERROR = f"monitors.json not found at {CONFIG_PATH}"
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("monitors.json is not valid JSON: %s", exc)
        _LAST_ERROR = f"monitors.json is not valid JSON: {exc}"
        return {}
    except OSError as exc:
        log.error("monitors.json could not be read: %s", exc)
        _LAST_ERROR = f"monitors.json could not be read: {exc}"
        return {}
    if not isinstance(data, dict):
        log.error("monitors.json is a %s, expected an object", type(data).__name__)
        _LAST_ERROR = (f"monitors.json is a JSON {type(data).__name__}, "
                       "expected an object")
        return {}
    _LAST_ERROR = None
    return data


def section(name: str) -> dict[str, Any]:
    """Return one top-level object section, or {} if missing/not an object."""
    raw = load().get(name, {})
    return raw if isinstance(raw, dict) else {}
