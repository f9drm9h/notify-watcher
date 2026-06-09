"""Topic: imminent rocket launches (Launch Library 2, free, no key).

The Space Devs' Launch Library 2 lists upcoming orbital launches with their net
(scheduled) time, provider, and webcast links. Each run we alert once per launch
that is within imminent_hours, skipping routine launches whose name matches
skip_keywords (Starlink by default) so the feed doesn't become a SpaceX firehose.
"""
from __future__ import annotations

import datetime as _dt
import logging

import requests

from .. import config, events, ids

log = logging.getLogger(__name__)

STATE_KEY = "launch_seen_ids"
CAP = 200
API = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}


def _parse_net(net: str):
    """Parse an ISO net time to an aware UTC datetime, or None."""
    if not net:
        return None
    try:
        return _dt.datetime.fromisoformat(net.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _select(results: list[dict], now, cfg: dict) -> list[tuple]:
    """Pure: launches within imminent_hours and not skipped.

    Returns [(id, name, hours_until, webcast_url)].
    """
    imminent = float(cfg.get("imminent_hours", 24))
    skip = [s.lower() for s in (cfg.get("skip_keywords") or [])]
    out: list[tuple] = []
    for r in results:
        name = r.get("name") or ""
        if any(s in name.lower() for s in skip):
            continue
        net = _parse_net(r.get("net"))
        if net is None:
            continue
        hours = (net - now).total_seconds() / 3600.0
        if 0 <= hours <= imminent:
            url = ((r.get("vidURLs") or [{}])[0].get("url")
                   if r.get("vidURLs") else None) or r.get("url")
            out.append((str(r.get("id")), name, hours, url))
    return out


def run(state: dict) -> dict:
    cfg = config.section("launches")
    try:
        resp = requests.get(API, params={"limit": 20, "mode": "detailed"},
                            headers=HEADERS, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except Exception as exc:  # noqa: BLE001 - non-fatal (LL2 rate-limits)
        log.error("launches fetch failed: %s", exc)
        return state

    now = _dt.datetime.now(_dt.timezone.utc)
    selected = _select(results, now, cfg)

    seen = state.get(STATE_KEY)
    if seen is None:
        # Seed silently with the currently-imminent launches so we don't blast
        # a backlog on first run.
        state[STATE_KEY] = [ids.short(s[0]) for s in selected][:CAP]
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 STATE_KEY, len(state[STATE_KEY]))
        return state

    seen = ids.normalize_seen(seen)
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = 0
    for lid, name, hours, url in selected:
        h = ids.short(lid)
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)
        when = "soon" if hours < 1 else f"in ~{hours:.0f}h"
        state = events.emit(
            state,
            title="Rocket launch",
            body=f"{name} launches {when}.",
            topic="launches",
            severity="moderate",
            source="Launches",
            click_url=url or None,
            tags="rocket",
            legacy_priority="default",
            legacy_action="push",
        )
        pushed += 1

    if pushed:
        log.info("launches: %d alert(s)", pushed)
    state[STATE_KEY] = (fresh + seen)[:CAP]
    return state
