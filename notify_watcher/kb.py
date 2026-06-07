"""Curated knowledge-base channels: load a vetted fact list and pick today's.

A KB is a JSON array of objects (data/*.json), each with a text field plus an
optional "src" — plain text, no secrets, editable on github.com. This module is
the shared engine behind every "one curated item per day" feature (the daily
health tip and the learning push), so adding a channel is a JSON file, not code.

Selection is a deterministic day-of-year rotation: a given calendar day always
yields the same entry, so a re-run on the Actions runner never drifts, and
entries spread evenly across the year (no repeats within a year once a KB has
>= 366 entries). LLMs never invent KB content — an optional reword (see
notify_watcher.summarize) only rephrases an already-vetted entry, and any
failure falls back to the verbatim text.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# data/ sits next to the package (repo root), beside watchlist.json.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load(path: Path, field: str = "text") -> list[dict]:
    """Load a KB file, keeping only objects that have the text `field`.

    A missing file or malformed JSON yields an empty list (logged), so a bad KB
    never crashes the daily run — the caller treats empty as "nothing to send".
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("could not load KB %s: %s", getattr(path, "name", path), exc)
        return []
    if not isinstance(data, list):
        log.error("KB %s is not a JSON array", getattr(path, "name", path))
        return []
    return [e for e in data if isinstance(e, dict) and e.get(field)]


def day_of_year(day: _dt.date | None = None) -> int:
    return (day or _dt.date.today()).timetuple().tm_yday


def pick(items: list, offset: int = 0, day: _dt.date | None = None):
    """Deterministically pick one item by day-of-year rotation, or None if empty.

    `offset` staggers parallel rotations (e.g. choosing a channel and then an
    item within it) so they don't advance in lockstep.
    """
    if not items:
        return None
    return items[(day_of_year(day) + offset) % len(items)]
