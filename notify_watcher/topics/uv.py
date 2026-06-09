"""Topic: high-UV heads-up for your location (Open-Meteo, free, no key).

Open-Meteo's forecast API gives the day's maximum UV index for any lat/lon
without a key. On the daily run we push a sun-protection nudge only when today's
max is at/above the configured threshold, at most once per day. The DR sits high
year-round, so alert_uv defaults high enough to flag the worst days rather than
ping every single one.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

import requests

from .. import config, events

log = logging.getLogger(__name__)

STATE_KEY = "uv_last_sent"  # YYYY-MM-DD guard so it fires once per day
API = "https://api.open-meteo.com/v1/forecast"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}


def _describe(uv: float) -> str:
    if uv >= 11:
        return "extreme"
    if uv >= 8:
        return "very high"
    if uv >= 6:
        return "high"
    return "moderate"


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state

    loc = config.section("location")
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        log.info("no location configured; skipping uv")
        return state

    cfg = config.section("uv")
    today = _dt.date.today().isoformat()
    if state.get(STATE_KEY) == today:
        return state

    try:
        resp = requests.get(
            API,
            params={"latitude": lat, "longitude": lon,
                    "daily": "uv_index_max", "timezone": "auto", "forecast_days": 1},
            headers=HEADERS, timeout=30,
        )
        resp.raise_for_status()
        values = (resp.json().get("daily") or {}).get("uv_index_max") or []
    except Exception as exc:  # noqa: BLE001 - non-fatal
        log.error("uv fetch failed: %s", exc)
        return state

    if not values or values[0] is None:
        return state
    uv = float(values[0])
    alert_uv = float(cfg.get("alert_uv", 9))
    log.info("uv: max %.1f (threshold %.1f)", uv, alert_uv)
    if uv < alert_uv:
        return state

    events.emit(
        state,
        title=f"High UV today ({_describe(uv)})",
        body=f"UV index will reach {uv:.0f}. Use sunscreen and limit midday sun.",
        topic="uv",
        severity="moderate",
        source="UV",
        tags="sunny",
        legacy_priority="default",
        legacy_action="push",
    )
    state[STATE_KEY] = today
    return state
