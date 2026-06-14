"""Lightweight mute/snooze state for Discord UI actions.

This module is intentionally separate from state.json. The Discord button
router can call these helpers without knowing anything about watcher internals:

    mute_topic("movies")
    snooze_topic("games", hours=1)
    if not is_topic_active("movies"):
        ...

State lives in mutes.json at the repo root:

    {
      "movies": {
        "status": "muted",
        "expires_at": null,
        "updated_at": "2026-06-14T12:00:00+00:00"
      },
      "games": {
        "status": "snoozed",
        "expires_at": "2026-06-14T13:00:00+00:00",
        "updated_at": "2026-06-14T12:00:00+00:00"
      }
    }
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
MUTES_PATH = ROOT / "mutes.json"

STATUS_MUTED = "muted"
STATUS_SNOOZED = "snoozed"


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def _parse_ts(raw: object) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _normalize_topic(topic: str) -> str:
    value = str(topic or "").strip().lower()
    if not value:
        raise ValueError("topic must be a non-empty string")
    return value


def _target(path: Path | None = None) -> Path:
    return path if path is not None else MUTES_PATH


def load_mutes(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load mutes.json, returning an empty map when the file is missing."""
    target = _target(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{target} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"{target} must contain a JSON object")
    return {str(topic): entry for topic, entry in raw.items()
            if isinstance(entry, dict)}


def save_mutes(data: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    """Persist mutes.json in a stable, human-readable format."""
    _target(path).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def mute_topic(topic: str) -> dict[str, Any]:
    """Permanently mute a topic until another command removes/overwrites it."""
    topic = _normalize_topic(topic)
    data = load_mutes()
    entry = {
        "status": STATUS_MUTED,
        "expires_at": None,
        "updated_at": _iso(_now_utc()),
    }
    data[topic] = entry
    save_mutes(data)
    return entry


def snooze_topic(topic: str, hours: float = 1) -> dict[str, Any]:
    """Mute a topic temporarily for `hours` hours from now."""
    topic = _normalize_topic(topic)
    try:
        hours_value = float(hours)
    except (TypeError, ValueError) as exc:
        raise ValueError("hours must be a positive number") from exc
    if hours_value <= 0:
        raise ValueError("hours must be greater than zero")

    now = _now_utc()
    entry = {
        "status": STATUS_SNOOZED,
        "expires_at": _iso(now + dt.timedelta(hours=hours_value)),
        "updated_at": _iso(now),
    }
    data = load_mutes()
    data[topic] = entry
    save_mutes(data)
    return entry


def is_topic_active(topic: str) -> bool:
    """Return False when a topic is permanently muted or currently snoozed.

    Expired snoozes are removed from mutes.json as a side effect. Malformed
    entries fail open: they are cleaned up and the topic is treated as active.
    A corrupt mutes.json also fails open here, but write operations still raise
    so the file is not overwritten silently.
    """
    topic = _normalize_topic(topic)
    try:
        data = load_mutes()
    except RuntimeError as exc:
        log.warning("mute_manager: %s; treating %s as active", exc, topic)
        return True

    entry = data.get(topic)
    if not isinstance(entry, dict):
        return True

    status = entry.get("status")
    if status == STATUS_MUTED:
        return False

    if status == STATUS_SNOOZED:
        expires_at = _parse_ts(entry.get("expires_at"))
        if expires_at and expires_at > _now_utc():
            return False
        data.pop(topic, None)
        save_mutes(data)
        return True

    data.pop(topic, None)
    save_mutes(data)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print(f"Using {MUTES_PATH}")
    print("Initial movies active?", is_topic_active("movies"))

    print("Snoozing movies for 1 hour...")
    snooze_topic("movies", hours=1)
    print("Movies active after snooze?", is_topic_active("movies"))

    print("Muting games permanently...")
    mute_topic("games")
    print("Games active after mute?", is_topic_active("games"))

    print("Done. Inspect mutes.json to see the persisted state.")
