"""Topic: movie release dates for a personal watchlist, via TMDb.

For each title in watchlist.json["movies"] we resolve it to a TMDb movie and
track its release date. We push when a title is first seen and whenever its
date moves (the common case worth knowing about: a delay or a newly-set date
on a previously-TBA film).

Requires a free TMDb v3 API key in the TMDB_API_KEY env var / GitHub secret.
If the key is unset the topic no-ops quietly so the rest of the run is
unaffected.
"""
from __future__ import annotations

import logging
import os

import requests

from .. import ntfy, watchlist

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
MOVIE_PAGE = "https://www.themoviedb.org/movie/"
STATE_KEY = "movie_release_dates"  # { "<tmdb_id>": "YYYY-MM-DD" | "TBA" }
TBA = "TBA"


def _search(title: str, api_key: str) -> dict | None:
    """Return the best-match TMDb movie object for a title, or None."""
    resp = requests.get(
        SEARCH_URL,
        params={"api_key": api_key, "query": title, "include_adult": "false"},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    # TMDb sorts by popularity/relevance, so the first hit is the intended film.
    return results[0] if results else None


def run(state: dict) -> dict:
    api_key = os.environ.get("TMDB_API_KEY", "").strip()
    if not api_key:
        log.info("TMDB_API_KEY not set; skipping movie watcher")
        return state

    wanted = watchlist.titles("movies")
    if not wanted:
        log.info("no movies in watchlist; nothing to do")
        return state

    bucket: dict = state.setdefault(STATE_KEY, {})

    for title in wanted:
        try:
            movie = _search(title, api_key)
            if movie is None:
                log.warning("no TMDb match for movie %r", title)
                continue

            mid = str(movie.get("id"))
            name = movie.get("title") or title
            year = (movie.get("release_date") or "")[:4]
            current = movie.get("release_date") or TBA
            log.info("movie %r -> %s (%s) release %s", title, name, year or "?", current)

            previous = bucket.get(mid)
            if previous == current:
                continue

            if previous is None:
                body = f"Now tracking {name}. Release date: {current}"
            else:
                body = f"{name} release date changed: {previous} -> {current}"
            ntfy.push(
                title=f"Movie: {name}",
                message=body,
                click_url=MOVIE_PAGE + mid,
                tags="clapper",
            )
            bucket[mid] = current
        except Exception as exc:  # noqa: BLE001 - isolate each title
            log.error("movie %r check failed: %s", title, exc)

    return state
