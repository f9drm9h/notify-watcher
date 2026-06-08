"""Topic: visible ISS passes over your location (g7vrd API, free, no key).

The g7vrd satellite-pass API returns upcoming ISS passes for a lat/lon with their
start/end times, peak elevation, and rise/set azimuths. We alert a pass once when
it clears min_elevation_deg AND falls in an evening or pre-dawn window - the times
the station is actually visible (sky dark, satellite still sunlit) - converting
the time to local. A geometric pass at noon isn't useful, so we filter those out.
"""
from __future__ import annotations

import datetime as _dt
import logging

import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # noqa: BLE001 - py<3.9 fallback (not expected on CI)
    ZoneInfo = None  # type: ignore

from .. import config, ids, ntfy

log = logging.getLogger(__name__)

STATE_KEY = "iss_seen_passes"
CAP = 100
API = "https://api.g7vrd.co.uk/v1/satellite-passes/25544/{lat}/{lon}.json"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}
# Local-time windows where a pass can be visible (evening after dusk, pre-dawn).
_EVENING = (18, 24)
_PREDAWN = (4, 6)
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _compass(az) -> str:
    try:
        return _COMPASS[int((float(az) % 360) / 45 + 0.5) % 8]
    except (TypeError, ValueError):
        return "?"


def _parse(ts: str):
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _in_viewing_window(local_hour: int) -> bool:
    return (_EVENING[0] <= local_hour < _EVENING[1]) or (_PREDAWN[0] <= local_hour < _PREDAWN[1])


def _select(passes: list[dict], now, tz, cfg: dict) -> list[tuple]:
    """Pure: visible-window passes above the elevation floor in the next 24h.

    Returns [(key, local_dt, max_el, rise_dir, set_dir)].
    """
    min_el = float(cfg.get("min_elevation_deg", 30))
    out: list[tuple] = []
    for p in passes:
        start = _parse(p.get("start"))
        if start is None:
            continue
        hours = (start - now).total_seconds() / 3600.0
        if not (0 <= hours <= 24):
            continue
        try:
            max_el = float(p.get("max_elevation"))
        except (TypeError, ValueError):
            continue
        if max_el < min_el:
            continue
        local = start.astimezone(tz) if tz else start
        if not _in_viewing_window(local.hour):
            continue
        out.append((p.get("start"), local, max_el,
                    _compass(p.get("aos_azimuth")), _compass(p.get("los_azimuth"))))
    return out


def run(state: dict) -> dict:
    loc = config.section("location")
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        log.info("no location configured; skipping iss")
        return state

    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(loc.get("timezone", "UTC"))
        except Exception:  # noqa: BLE001
            tz = None

    cfg = config.section("iss")
    try:
        resp = requests.get(API.format(lat=lat, lon=lon), headers=HEADERS, timeout=30)
        resp.raise_for_status()
        passes = resp.json().get("passes") or []
    except Exception as exc:  # noqa: BLE001 - non-fatal
        log.error("iss fetch failed: %s", exc)
        return state

    now = _dt.datetime.now(_dt.timezone.utc)
    selected = _select(passes, now, tz, cfg)

    seen = state.get(STATE_KEY)
    if seen is None:
        state[STATE_KEY] = [ids.short(s[0]) for s in selected][:CAP]
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 STATE_KEY, len(state[STATE_KEY]))
        return state

    seen = ids.normalize_seen(seen)
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = 0
    for key, local, max_el, rise_dir, set_dir in selected:
        h = ids.short(key)
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)
        ntfy.push(
            title="ISS pass overhead",
            message=(f"Visible pass at {local.strftime('%I:%M %p').lstrip('0')} - "
                     f"max {max_el:.0f}deg, {rise_dir} to {set_dir}. Look up!"),
            tags="satellite",
            priority="low",
        )
        pushed += 1

    if pushed:
        log.info("iss: %d pass alert(s)", pushed)
    state[STATE_KEY] = (fresh + seen)[:CAP]
    return state
