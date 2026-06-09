"""Topic: rough-seas heads-up for the nearest coast (Open-Meteo Marine, no key).

Open-Meteo's marine API gives the day's maximum wave height for a coastal point
without a key. On the daily run we push a heads-up only when today's max is at/
above the configured threshold - a useful "rough today" signal for beach or
fishing plans - at most once per day. (Open-Meteo's free tier has no tidal
predictions, so this is a sea-state report, not a tide table.)
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

import requests

from .. import config, events

log = logging.getLogger(__name__)

STATE_KEY = "marine_last_sent"  # YYYY-MM-DD guard so it fires once per day
API = "https://marine-api.open-meteo.com/v1/marine"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state

    cfg = config.section("marine")
    lat, lon = cfg.get("latitude"), cfg.get("longitude")
    if lat is None or lon is None:
        log.info("no marine coordinates configured; skipping marine")
        return state

    today = _dt.date.today().isoformat()
    if state.get(STATE_KEY) == today:
        return state

    try:
        resp = requests.get(
            API,
            params={"latitude": lat, "longitude": lon,
                    "daily": "wave_height_max", "timezone": "auto", "forecast_days": 1},
            headers=HEADERS, timeout=30,
        )
        resp.raise_for_status()
        values = (resp.json().get("daily") or {}).get("wave_height_max") or []
    except Exception as exc:  # noqa: BLE001 - non-fatal
        log.error("marine fetch failed: %s", exc)
        return state

    if not values or values[0] is None:
        return state
    wave = float(values[0])
    rough = float(cfg.get("rough_wave_m", 2.0))
    log.info("marine: wave_height_max %.2f m (threshold %.2f)", wave, rough)
    if wave < rough:
        return state

    events.emit(
        state,
        title="Rough seas today",
        body=f"Waves up to {wave:.1f} m off the coast today. Take care at the beach.",
        topic="marine",
        severity="moderate",
        source="Marine",
        tags="ocean",
        legacy_priority="default",
        legacy_action="push",
    )
    state[STATE_KEY] = today
    return state
