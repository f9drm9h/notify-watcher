"""Lightweight audit trail for items the router intentionally drops.

The main ``state.json`` is already busy with durable watcher state. This module
keeps short-term diagnostics in ``audit.json`` instead: the last few dropped
items per topic, with the exact routing reason, so an admin can ask
``explain <topic>`` without bloating the workflow state.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

AUDIT_PATH = Path(__file__).resolve().parent.parent / "audit.json"
MAX_PER_TOPIC = 5


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _target(path: Optional[Path] = None) -> Path:
    return Path(path) if path is not None else AUDIT_PATH


def _clean_entry(entry: object) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    title = str(entry.get("title") or "").strip()
    reason = str(entry.get("reason") or "").strip()
    if not title or not reason:
        return None
    cleaned = {
        "ts": str(entry.get("ts") or ""),
        "title": title,
        "reason": reason,
    }
    if entry.get("source"):
        cleaned["source"] = str(entry["source"])
    if entry.get("score") is not None:
        try:
            cleaned["score"] = int(entry["score"])
        except (TypeError, ValueError):
            pass
    return cleaned


def load(path: Optional[Path] = None) -> dict[str, list[dict]]:
    """Read audit.json, tolerating missing or malformed files as empty."""
    target = _target(path)
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("audit: could not read %s: %s", target, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    data: dict[str, list[dict]] = {}
    for topic, entries in raw.items():
        if not isinstance(entries, list):
            continue
        cleaned = [_clean_entry(entry) for entry in entries]
        kept = [entry for entry in cleaned if entry][-MAX_PER_TOPIC:]
        if kept:
            data[str(topic)] = kept
    return data


def save(data: dict[str, list[dict]], path: Optional[Path] = None) -> None:
    """Persist audit data atomically enough for the small GitHub Actions file."""
    target = _target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)


def record(
    topic: str,
    title: str,
    reason: str,
    *,
    source: str = "",
    score: Optional[int] = None,
    path: Optional[Path] = None,
    now: Optional[_dt.datetime] = None,
) -> None:
    """Append one dropped item, retaining only the latest MAX_PER_TOPIC."""
    topic = str(topic or "unknown")
    entry = {
        "ts": (now or _utcnow()).isoformat(),
        "title": str(title or "(untitled)"),
        "reason": str(reason or "dropped by routing"),
    }
    if source:
        entry["source"] = str(source)
    if score is not None:
        entry["score"] = int(score)

    data = load(path)
    items = data.setdefault(topic, [])
    items.append(entry)
    data[topic] = items[-MAX_PER_TOPIC:]
    save(data, path)


def recent(topic: str, path: Optional[Path] = None) -> list[dict]:
    """Return newest retained drops for one topic in chronological order."""
    return list(load(path).get(str(topic or "unknown"), []))[-MAX_PER_TOPIC:]
