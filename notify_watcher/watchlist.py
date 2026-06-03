"""Load the user-editable watchlist of titles to track.

watchlist.json lives at the repo root and is plain text (no secrets), so you
can edit it right on github.com or locally. Shape:

    {
      "movies":   ["Some Movie", "Another Movie"],
      "games":    ["Some Game"],
      "products": [{"name": "Some Product", "url": "https://...", "target_price": 99.99}]
    }

A missing file, missing category, or malformed JSON yields an empty list so a
typo never crashes a scheduled run.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"


def _load(category: str) -> list:
    """Return the raw list for a category, or [] on any missing/bad input."""
    if not WATCHLIST_PATH.exists():
        log.warning("watchlist.json not found at %s", WATCHLIST_PATH)
        return []
    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("watchlist.json is not valid JSON: %s", exc)
        return []
    raw = data.get(category, [])
    if not isinstance(raw, list):
        log.error("watchlist.json[%r] is not a list", category)
        return []
    return raw


def entries(category: str) -> list[dict]:
    """Return the list of dict entries for an object-shaped category.

    Non-dict items are skipped so a malformed entry never crashes a run.
    """
    return [item for item in _load(category) if isinstance(item, dict)]


def titles(category: str) -> list[str]:
    """Return the de-duplicated, stripped list of titles for a category."""
    raw = _load(category)
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        t = item.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out
