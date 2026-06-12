"""Topic: flush the daily digest of moderate-importance monitor items.

Runs only on the daily cron (NOTIFY_DAILY set). Collectors accumulate moderate
items into state["digest_buffer"] throughout the day; this topic drains that
buffer into a single grouped notification and clears it. Registered AFTER the
collectors in main.py so items found on the same daily run are included.

The digest opens with a one-line morning weather summary ("Today: 31 °C,
rain 20%, UV 9") fetched from Open-Meteo for the configured location (free, no
key, same API the uv/beach_day topics use). The line is best-effort: any
failure — no location, network error, missing fields — just omits it, so the
digest is never blocked on weather.

All the real work (idempotent per-day guard, grouping, buffer clearing) lives
in notify_watcher.digest; this topic is just the scheduled entry point.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

import requests

from .. import config, control, digest

log = logging.getLogger(__name__)

API = "https://api.open-meteo.com/v1/forecast"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}


def _first_daily(payload: dict, field: str) -> float | None:
    values = (payload.get("daily") or {}).get(field) or []
    if values and values[0] is not None:
        try:
            return float(values[0])
        except (TypeError, ValueError):
            return None
    return None


def _weather_line(state: dict) -> str | None:
    """One-line weather summary for the top of the digest, or None.

    Current temperature plus today's max rain probability and max UV from one
    Open-Meteo forecast call. Degrades field by field (a missing value is just
    left out of the line) and returns None when there is nothing to show or on
    any error, so the digest always goes out regardless.
    """
    loc = config.section("location")
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        return None

    try:
        resp = requests.get(
            API,
            params={"latitude": lat, "longitude": lon,
                    "current": "temperature_2m",
                    "daily": "precipitation_probability_max,uv_index_max",
                    "timezone": "auto", "forecast_days": 1},
            headers=HEADERS, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - weather is decoration, never fatal
        log.warning("digest weather line skipped: %s", exc)
        return None

    temp = (data.get("current") or {}).get("temperature_2m")
    try:
        temp = float(temp) if temp is not None else None
    except (TypeError, ValueError):
        temp = None
    precip = _first_daily(data, "precipitation_probability_max")
    uv = _first_daily(data, "uv_index_max")

    parts = []
    if temp is not None:
        parts.append(f"{temp:.0f} °C")
    if precip is not None:
        parts.append(f"rain {precip:.0f}%")
    if uv is not None:
        parts.append(f"UV {uv:.0f}")
    if not parts:
        return None
    return "Today: " + ", ".join(parts)


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # only the daily cron run flushes the digest
    # Fetch the weather line only when flush will actually send (buffer has
    # items and today's digest isn't stamped yet), so a duplicate or empty
    # daily run costs no API call.
    header = None
    if (state.get(digest.BUFFER_KEY)
            and state.get(digest.LAST_SENT_KEY) != _dt.date.today().isoformat()):
        header = _weather_line(state)
    # Reply buttons: a fixed pair of 24h mutes (movies/games, the two
    # chattiest topics). A mute defers the topic's live pushes into the next
    # digest and drops its digest chatter; critical alerts still ring (see
    # events._apply_mute). make_action returns None when the control channel
    # is off, so the flush push is then byte-identical to before.
    actions = [a for a in (
        control.make_action("Mute movies 24h", "MUTE:movies:24"),
        control.make_action("Mute games 24h", "MUTE:games:24"),
        _follow_action(state),
    ) if a]
    digest.flush(state, config.section("digest"), header=header,
                 actions=actions or None)
    return state


def _follow_action(state: dict):
    """[Follow <topic> 3d] for the topic of the digest's top-scored item.

    The positive mirror of the mute buttons (docs/design/05): while followed,
    that topic's digest-bound items push live. Gated by digest.follow_button
    (default on); returns None when disabled, when no buffered item carries a
    topic (pre-migration entries), or when the control channel is off.
    """
    if not config.section("digest").get("follow_button", True):
        return None
    items = [it for it in (state.get(digest.BUFFER_KEY) or [])
             if isinstance(it, dict) and it.get("topic")]
    if not items:
        return None
    hot = max(items, key=lambda it: it.get("score", 0))["topic"]
    return control.make_action(f"Follow {hot} 3d", f"FOLLOW:{hot}:72")
