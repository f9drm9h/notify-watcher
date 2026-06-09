"""Topic: weekend "beach day" index — one 0-10 score instead of three raw feeds.

The app already watches UV, wave height, and tropical weather separately, but
none of them answers the actual Saturday-morning question: *is today a good
beach day?* This topic asks Open-Meteo for the day's beach-relevant numbers at
the configured coast (default Boca Chica, like marine) — max wave height from
the marine API; rain probability, max UV, and max temperature from the forecast
API — and folds them into a single 0-10 score with a one-line verdict:

    Beach day: 8/10 — great day for the beach
    Waves 0.7 m | Rain 15% | UV 9 | 31 C
    UV is extreme at midday - go early and reapply sunscreen.

Scoring is pure and deterministic (see _score): waves and rain probability
subtract the most, a cool day a little; extreme UV subtracts one and adds a
caution line. Each input degrades independently — if one API fails the score is
computed from what's known and the body says "unknown" for the rest; only when
NOTHING is available does it skip (unstamped, so it retries on the same day's
next run). Fires on the days in monitors.json -> beach_day.weekdays (default
Saturday), on the daily run, once per day. No key.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

import requests

from .. import config, events

log = logging.getLogger(__name__)

STATE_KEY = "beach_day_last_sent"
MARINE_API = "https://marine-api.open-meteo.com/v1/marine"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}

# date.weekday() numbering: Monday=0 ... Saturday=5, Sunday=6.
DEFAULT_WEEKDAYS = [5]
DEFAULT_LAT = 18.45   # Boca Chica, same default coast as marine
DEFAULT_LON = -69.6


def _today() -> _dt.date:
    return _dt.date.today()


def _first_daily(payload: dict, field: str) -> float | None:
    values = (payload.get("daily") or {}).get(field) or []
    if values and values[0] is not None:
        try:
            return float(values[0])
        except (TypeError, ValueError):
            return None
    return None


def _fetch_marine(lat, lon) -> float | None:
    resp = requests.get(
        MARINE_API,
        params={"latitude": lat, "longitude": lon,
                "daily": "wave_height_max", "timezone": "auto", "forecast_days": 1},
        headers=HEADERS, timeout=30,
    )
    resp.raise_for_status()
    return _first_daily(resp.json(), "wave_height_max")


def _fetch_forecast(lat, lon) -> dict:
    resp = requests.get(
        FORECAST_API,
        params={"latitude": lat, "longitude": lon,
                "daily": "precipitation_probability_max,uv_index_max,temperature_2m_max",
                "timezone": "auto", "forecast_days": 1},
        headers=HEADERS, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "precip": _first_daily(data, "precipitation_probability_max"),
        "uv": _first_daily(data, "uv_index_max"),
        "temp": _first_daily(data, "temperature_2m_max"),
    }


def _score(wave_m, precip_pct, uv, temp_c) -> tuple[int, list[str]]:
    """Pure. (0-10 score, caution notes) from the day's numbers; None = unknown.

    Unknown inputs neither add nor subtract — the score reflects what we know
    and the body marks the gap, so one API outage never zeroes a sunny day.
    """
    score = 10.0
    notes: list[str] = []
    if wave_m is not None:
        if wave_m >= 2.0:
            score -= 4
            notes.append(f"Rough seas ({wave_m:.1f} m waves) - swimming not advisable.")
        elif wave_m >= 1.5:
            score -= 2
            notes.append(f"Choppy water ({wave_m:.1f} m waves).")
        elif wave_m >= 1.0:
            score -= 1
    if precip_pct is not None:
        if precip_pct >= 70:
            score -= 4
            notes.append(f"High rain chance ({precip_pct:.0f}%).")
        elif precip_pct >= 40:
            score -= 2
        elif precip_pct >= 20:
            score -= 1
    if temp_c is not None and temp_c < 26:
        score -= 1  # a cool day by DR standards
    if uv is not None and uv >= 11:
        score -= 1
        notes.append("UV is extreme at midday - go early and reapply sunscreen.")
    return max(0, min(10, round(score))), notes


def _verdict(score: int) -> str:
    if score >= 8:
        return "great day for the beach"
    if score >= 6:
        return "good, with a caveat or two"
    if score >= 4:
        return "iffy - check the notes"
    return "better to skip it today"


def _fmt(value, template: str, unknown: str) -> str:
    return template.format(value) if value is not None else unknown


def _compose(wave_m, precip_pct, uv, temp_c) -> tuple[str, str]:
    """Pure. (title, body) for the day's beach index."""
    score, notes = _score(wave_m, precip_pct, uv, temp_c)
    title = f"Beach day: {score}/10 - {_verdict(score)}"
    facts = " | ".join([
        _fmt(wave_m, "Waves {:.1f} m", "Waves unknown"),
        _fmt(precip_pct, "Rain {:.0f}%", "Rain unknown"),
        _fmt(uv, "UV {:.0f}", "UV unknown"),
        _fmt(temp_c, "{:.0f} C", "Temp unknown"),
    ])
    body = "\n".join([facts] + notes)
    return title, body


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state
    cfg = config.section("beach_day")
    weekdays = cfg.get("weekdays", DEFAULT_WEEKDAYS)
    today = _today()
    if not isinstance(weekdays, list) or today.weekday() not in weekdays:
        return state
    if state.get(STATE_KEY) == today.isoformat():
        return state

    lat = cfg.get("latitude", DEFAULT_LAT)
    lon = cfg.get("longitude", DEFAULT_LON)

    wave = precip = uv = temp = None
    try:
        wave = _fetch_marine(lat, lon)
    except Exception as exc:  # noqa: BLE001 - degrade, don't die
        log.warning("beach_day: marine fetch failed: %s", exc)
    try:
        fc = _fetch_forecast(lat, lon)
        precip, uv, temp = fc["precip"], fc["uv"], fc["temp"]
    except Exception as exc:  # noqa: BLE001 - degrade, don't die
        log.warning("beach_day: forecast fetch failed: %s", exc)

    if wave is None and precip is None and uv is None and temp is None:
        log.warning("beach_day: no data at all; will retry on the next run today")
        return state

    title, body = _compose(wave, precip, uv, temp)
    state = events.emit(
        state,
        title=title,
        body=body,
        topic="beach_day",
        severity="low",
        source="Beach day",
        tags="beach_umbrella",
        legacy_priority="low",
        legacy_action="push",
    )
    state[STATE_KEY] = today.isoformat()
    log.info("beach_day: sent (%s)", title)
    return state
