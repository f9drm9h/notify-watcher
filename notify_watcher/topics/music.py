"""Topic: music — new releases from followed artists + a daily discovery pick.

Two independent behaviours, both on the free Deezer public API (no key):

1. Releases (every run): for each monitors.json -> music.followed_artists, look
   up the artist and watch their album/single list; push when a release appears
   that we haven't seen. Seeds silently on the first run so a backlog never
   blasts. Useful but sparse (the user's followed artists rarely release).

2. Discovery (daily only): the user wanted "a song I probably haven't heard,"
   derived from their actual library. data/music_seed.json holds the artists
   scanned from their music folder (see tools/scan_music.py). We rotate through
   that seed by day-of-year, ask Deezer for a *related* artist that is neither in
   the seed nor already recommended, and push that artist's top track. So every
   pick is adjacent to their taste yet new to them, and never repeats.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path

import requests

from .. import config, control, events, ids

log = logging.getLogger(__name__)

RELEASE_SEEN_KEY = "music_release_seen"      # short hashes of album ids
DISCOVERY_SEEN_KEY = "music_discovery_seen"  # Deezer artist ids recommended before
CAP = 300
SEED_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "music_seed.json"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}
_API = "https://api.deezer.com"


def _get(path: str, **params) -> dict:
    resp = requests.get(f"{_API}/{path}", params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _artist_id(name: str) -> int | None:
    """Resolve an artist name to its Deezer id, or None."""
    data = _get("search/artist", q=name, limit=1).get("data") or []
    return data[0]["id"] if data else None


# --- 1. Releases -----------------------------------------------------------
def _artists(state: dict) -> list[str]:
    """Configured followed_artists + artists followed from a notification
    ([Follow artist] -> state["follows"]["artists"], docs/design/05),
    de-duplicated case-insensitively with the config entry winning."""
    merged: list[str] = list(config.section("music").get("followed_artists") or [])
    seen = {a.lower() for a in merged if isinstance(a, str)}
    for entry in control.follows(state, "artists"):
        name = str(entry.get("name") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            merged.append(name)
    return merged


def _releases(state: dict) -> dict:
    artists = _artists(state)
    if not artists:
        return state

    seen = state.get(RELEASE_SEEN_KEY)
    first_run = seen is None
    seen = ids.normalize_seen(seen or [])
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = 0

    for name in artists:
        try:
            aid = _artist_id(name)
            if aid is None:
                log.info("music: no Deezer match for %r", name)
                continue
            albums = (_get(f"artist/{aid}/albums", limit=10).get("data") or [])
            for alb in albums:
                h = ids.short(str(alb.get("id")))
                if h in seen_set:
                    continue
                seen_set.add(h)
                fresh.append(h)
                if first_run:
                    continue  # seed silently
                events.emit(
                    state,
                    title=f"New from {name}",
                    body=f"{alb.get('title', '')} ({alb.get('release_date', '')})".strip(),
                    topic="music",
                    severity="moderate",
                    source=name,
                    click_url=alb.get("link") or None,
                    tags="musical_note",
                    legacy_priority="default",
                    legacy_action="push",
                )
                pushed += 1
        except Exception as exc:  # noqa: BLE001 - isolate each artist
            log.error("music release check for %r failed: %s", name, exc)

    if first_run:
        log.info("seeded %s baseline with %d album id(s) (no alerts on first run)",
                 RELEASE_SEEN_KEY, len(fresh))
    elif pushed:
        log.info("music: %d new release(s) pushed", pushed)

    state[RELEASE_SEEN_KEY] = (fresh + seen)[:CAP]
    return state


# --- 2. Discovery ----------------------------------------------------------
def _load_seed() -> list[str]:
    try:
        data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.info("music seed unavailable (%s); skipping discovery", exc)
        return []
    artists = data.get("artists") if isinstance(data, dict) else None
    return [a for a in (artists or []) if isinstance(a, str)]


def _pick_seed(artists: list[str], doy: int) -> str | None:
    """Deterministically rotate through the seed list by day-of-year."""
    return artists[doy % len(artists)] if artists else None


def _pick_recommendation(related: list[dict], seed_set: set[str], seen_ids: set,
                         ignored: dict | None = None) -> dict | None:
    """First related artist that is new to the user (not in their library or
    already recommended). `related` is Deezer artist objects. `ignored` is
    state["ignored"]: an artist the user tapped [Not my thing] on re-rolls to
    the next candidate instead of ever being recommended again."""
    for art in related:
        name = (art.get("name") or "").strip()
        if not name or art.get("id") in seen_ids:
            continue
        if name.lower() in seed_set:
            continue
        if ignored and control.offer_id("artist", {"name": name}) in ignored:
            continue
        return art
    return None


def _discovery(state: dict) -> dict:
    seed_artists = _load_seed()
    if not seed_artists:
        return state

    # Keep an insertion-ordered list (newest last) for a deterministic CAP trim,
    # plus a set built from it for O(1) "already recommended?" lookups.
    seen_list = list(state.get(DISCOVERY_SEEN_KEY) or [])
    seen_ids = set(seen_list)
    seed_set = {a.lower() for a in seed_artists}
    doy = _dt.date.today().timetuple().tm_yday

    # Try a few seeds (today's, then following days) so an exhausted seed's
    # related list doesn't waste the day.
    rec = seed = None
    for offset in range(min(len(seed_artists), 10)):
        seed = _pick_seed(seed_artists, doy + offset)
        try:
            sid = _artist_id(seed)
            if sid is None:
                continue
            related = (_get(f"artist/{sid}/related", limit=20).get("data") or [])
            rec = _pick_recommendation(related, seed_set, seen_ids,
                                       state.get("ignored") or {})
            if rec:
                break
        except Exception as exc:  # noqa: BLE001
            log.error("music discovery via %r failed: %s", seed, exc)

    if not rec:
        log.info("music discovery: no fresh recommendation found this run")
        return state

    try:
        top = (_get(f"artist/{rec['id']}/top", limit=1).get("data") or [])
        track = top[0] if top else None
        title = track.get("title") if track else None
        link = (track.get("link") if track else None) or rec.get("link")
        msg = f"{title} - {rec['name']}" if title else rec["name"]
        # Offer registry (docs/design/05): [Follow artist] opts into release
        # alerts for this pick; [Not my thing] re-rolls future discoveries
        # past them. Buttons evaporate when the control channel is off.
        oid = control.register_offer(state, "artist", rec["name"],
                                     {"name": rec["name"]})
        actions = [a for a in (
            control.make_action("Follow artist", f"ADD:{oid}"),
            control.make_action("Not my thing", f"IGNORE:{oid}"),
        ) if a] if oid else []
        events.emit(
            state,
            title="Music discovery",
            body=f"{msg}\n(because you like {seed})",
            topic="music",
            severity="low",
            source="Music",
            click_url=link or None,
            tags="headphones",
            legacy_priority="low",
            legacy_action="push",
            metadata={"actions": actions} if actions else None,
        )
        # Append (rec is, by construction, not already in seen_ids) and keep the
        # newest CAP entries — deterministic, unlike slicing a set-derived list.
        seen_list.append(rec["id"])
        state[DISCOVERY_SEEN_KEY] = seen_list[-CAP:]
        log.info("music discovery: recommended %r (seed %r)", rec["name"], seed)
    except Exception as exc:  # noqa: BLE001
        log.error("music discovery push failed: %s", exc)

    return state


def run(state: dict) -> dict:
    state = _releases(state)
    if os.environ.get("NOTIFY_DAILY"):
        state = _discovery(state)
    return state
