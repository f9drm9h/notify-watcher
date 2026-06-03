"""Topic: video game release dates for a personal watchlist, via RAWG.

For each title in watchlist.json["games"] we resolve it to a RAWG game and
track its release date. We push when a title is first seen and whenever its
date moves (e.g. a delay, or a TBA game getting a real date).

Requires a free RAWG API key in the RAWG_API_KEY env var / GitHub secret.
If the key is unset the topic no-ops quietly so the rest of the run is
unaffected.
"""
from __future__ import annotations

import logging
import os

import requests

from .. import ntfy, watchlist

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.rawg.io/api/games"
GAME_PAGE = "https://rawg.io/games/"
STATE_KEY = "game_release_dates"  # { "<rawg_id>": "YYYY-MM-DD" | "TBA" }
TBA = "TBA"


def _search(title: str, api_key: str) -> dict | None:
    """Return the best-match RAWG game object for a title, or None."""
    resp = requests.get(
        SEARCH_URL,
        params={
            "key": api_key,
            "search": title,
            "search_precise": "true",
            "page_size": 5,
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    return results[0] if results else None


def _release(game: dict) -> str:
    """RAWG gives `released` (YYYY-MM-DD or null) and a `tba` flag."""
    if game.get("released"):
        return game["released"]
    return TBA


def run(state: dict) -> dict:
    api_key = os.environ.get("RAWG_API_KEY", "").strip()
    if not api_key:
        log.info("RAWG_API_KEY not set; skipping game watcher")
        return state

    wanted = watchlist.titles("games")
    if not wanted:
        log.info("no games in watchlist; nothing to do")
        return state

    bucket: dict = state.setdefault(STATE_KEY, {})

    for title in wanted:
        try:
            game = _search(title, api_key)
            if game is None:
                log.warning("no RAWG match for game %r", title)
                continue

            gid = str(game.get("id"))
            name = game.get("name") or title
            slug = game.get("slug") or ""
            current = _release(game)
            log.info("game %r -> %s release %s", title, name, current)

            previous = bucket.get(gid)
            if previous == current:
                continue

            if previous is None:
                body = f"Now tracking {name}. Release date: {current}"
            else:
                body = f"{name} release date changed: {previous} -> {current}"
            ntfy.push(
                title=f"Game: {name}",
                message=body,
                click_url=GAME_PAGE + slug if slug else None,
                tags="video_game",
            )
            bucket[gid] = current
        except Exception as exc:  # noqa: BLE001 - isolate each title
            log.error("game %r check failed: %s", title, exc)

    return state
