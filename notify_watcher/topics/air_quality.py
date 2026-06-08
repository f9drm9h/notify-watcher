"""Topic: local air-quality alerts (Open-Meteo, free, no key).

Open-Meteo's air-quality API returns the current US AQI for any lat/lon without
a key. We map it to the standard US AQI band and alert when the air enters a band
at or above the configured threshold (default: "Unhealthy for sensitive groups"),
with a louder priority for the worse bands. To avoid nagging, we alert at most
once per day per band and only when the band has *worsened* since the last alert
that day — so a steady "Unhealthy" day pings once, and a jump to "Very unhealthy"
pings again, but minor wiggles within a band stay quiet.
"""
from __future__ import annotations

import datetime as _dt
import logging

import requests

from .. import config, ntfy

log = logging.getLogger(__name__)

STATE_KEY = "air_quality_alert"  # {"date": "YYYY-MM-DD", "band": int}
API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}

# US AQI bands: (lower_bound, label, ntfy_priority). Index is the band number.
_BANDS = [
    (0, "Good", "low"),
    (51, "Moderate", "low"),
    (101, "Unhealthy for sensitive groups", "default"),
    (151, "Unhealthy", "high"),
    (201, "Very unhealthy", "high"),
    (301, "Hazardous", "urgent"),
]


def _band(aqi: float) -> tuple[int, str, str]:
    """Return (band_index, label, priority) for a US AQI value."""
    idx = 0
    for i, (lo, _label, _prio) in enumerate(_BANDS):
        if aqi >= lo:
            idx = i
    return idx, _BANDS[idx][1], _BANDS[idx][2]


def _should_alert(aqi, prev: dict, today: str, alert_index: int) -> tuple[bool, int, str, str]:
    """Pure decision. Returns (alert?, band_index, label, priority).

    Alerts only when the band is at/above `alert_index` AND it is worse than any
    band already alerted today (so the first crossing and each worsening fire,
    but repeats within the same day/band do not).
    """
    if aqi is None:
        return False, 0, "", ""
    idx, label, prio = _band(float(aqi))
    if idx < alert_index:
        return False, idx, label, prio
    if prev and prev.get("date") == today and int(prev.get("band", -1)) >= idx:
        return False, idx, label, prio
    return True, idx, label, prio


def run(state: dict) -> dict:
    loc = config.section("location")
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        log.info("no location configured; skipping air quality")
        return state

    cfg = config.section("air_quality")
    try:
        resp = requests.get(
            API_URL,
            params={"latitude": lat, "longitude": lon,
                    "current": "us_aqi,pm2_5,pm10", "timezone": "auto"},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        current = resp.json().get("current") or {}
    except Exception as exc:  # noqa: BLE001 - a fetch/parse failure is non-fatal
        log.error("air quality fetch failed: %s", exc)
        return state

    aqi = current.get("us_aqi")
    today = _dt.date.today().isoformat()
    alert_index = int(cfg.get("alert_band_index", 2))
    alert, idx, label, prio = _should_alert(aqi, state.get(STATE_KEY) or {}, today, alert_index)
    log.info("air quality: US AQI %s -> band %d (%s); alert=%s", aqi, idx, label, alert)

    if alert:
        pm25 = current.get("pm2_5")
        ntfy.push(
            title=f"Air quality: {label}",
            message=f"US AQI {int(aqi)} ({label}). PM2.5 {pm25} ug/m3 in {loc.get('name', 'your area')}.",
            tags="dash",
            priority=prio,
        )
        state[STATE_KEY] = {"date": today, "band": idx}

    return state
