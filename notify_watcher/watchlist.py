"""Load the user-editable watchlist of titles to track.

watchlist.json lives at the repo root and is plain text (no secrets), so you
can edit it right on github.com or locally. Shape:

    {
      "movies": ["Some Movie", "Another Movie"],
      "games":  ["Some Game"]
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


def titles(category: str) -> list[str]:
    """Return the de-duplicated, stripped list of titles for a category."""
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
