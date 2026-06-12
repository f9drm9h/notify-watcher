"""Topic: nearby earthquake alerts (USGS, free, no key).

USGS publishes near-real-time seismicity as GeoJSON. We read the M2.5+ past-day
feed, compute each quake's great-circle distance from home (monitors.json ->
location), and route by magnitude AND proximity: a strong, close quake pushes
live; a smaller nearby one goes to the daily digest; everything else is dropped.
Hispaniola is seismically active, so this is a protective local alert rather than
a global firehose.

Dedup is by USGS event id (seen-list; the first run seeds silently), so a quake
is alerted once even though the feed carries it for a day and may revise its
magnitude. Only quakes we actually act on are remembered, so a distant quake that
later edges into range can still alert.
"""
from __future__ import annotations

import logging
import math

import requests

from .. import config, events, health, ids

log = logging.getLogger(__name__)

TOPIC = "quakes"
STATE_KEY = "quake_seen_ids"
CAP = 300
DEFAULT_URL = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
)
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _classify(mag, dist_km: float, cfg: dict) -> str | None:
    """Return 'live' | 'digest' | None for a quake of `mag` at `dist_km` away."""
    if mag is None:
        return None
    mag = float(mag)
    if dist_km <= float(cfg.get("live_radius_km", 600)) and mag >= float(cfg.get("live_min_mag", 4.5)):
        return "live"
    if dist_km <= float(cfg.get("digest_radius_km", 300)) and mag >= float(cfg.get("digest_min_mag", 3.0)):
        return "digest"
    return None


def _tsunami_risk(mag, depth_km: float, dist_km: float, cfg: dict) -> bool:
    """A large, shallow, undersea-ish quake within tsunami range warrants a
    "check advisories" heads-up - tsunamis travel far, so the radius is wide and
    independent of the normal nearby-quake tiering."""
    if mag is None:
        return False
    return (float(mag) >= float(cfg.get("tsunami_min_mag", 7.0))
            and depth_km <= float(cfg.get("tsunami_max_depth_km", 70))
            and dist_km <= float(cfg.get("tsunami_radius_km", 1500)))


def _evaluate(features: list, home: tuple[float, float], cfg: dict) -> list[tuple]:
    """Pure: map USGS features to [(id, tier, mag, dist_km, place, url, tsunami)]
    worth acting on (a normal tier, or a tsunami-risk quake beyond that range)."""
    lat0, lon0 = home
    out: list[tuple] = []
    for f in features:
        try:
            fid = f.get("id")
            props = f.get("properties") or {}
            coords = (f.get("geometry") or {}).get("coordinates") or []
            if not fid or len(coords) < 2:
                continue
            mag = props.get("mag")
            lon, lat = float(coords[0]), float(coords[1])
            depth = float(coords[2]) if len(coords) > 2 and coords[2] is not None else 10.0
            dist = _haversine_km(lat0, lon0, lat, lon)
            tier = _classify(mag, dist, cfg)
            tsunami = _tsunami_risk(mag, depth, dist, cfg)
            if tier or tsunami:
                out.append((fid, tier, float(mag), dist,
                            props.get("place") or "", props.get("url") or "", tsunami))
        except (TypeError, ValueError):
            continue
    return out


def run(state: dict) -> dict:
    loc = config.section("location")
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        log.info("no location configured; skipping quakes")
        return state

    cfg = config.section("quakes")
    url = cfg.get("url") or DEFAULT_URL
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        features = resp.json().get("features") or []
    except Exception as exc:  # noqa: BLE001 - a fetch/parse failure is non-fatal
        log.error("USGS fetch failed: %s", exc)
        health.source_failed(state, TOPIC, f"USGS fetch failed: {exc}")
        return state
    log.info("USGS: %d quake(s) in feed", len(features))
    # Whole feed, pre-filter: last_data tracks the SOURCE producing data.
    health.source_ok(state, TOPIC, data_count=len(features))

    evaluated = _evaluate(features, (float(lat), float(lon)), cfg)

    seen = state.get(STATE_KEY)
    if seen is None:
        # Baseline-only first run: remember only the quakes we'd act on right now,
        # NOT every event in the feed. A quake currently too small/far to alert is
        # deliberately left unseen, so if a later feed revises its magnitude up (or
        # it edges into range) it can still alert. This matches the steady-state
        # loop below, which only ever records acted-on (evaluated) ids.
        state[STATE_KEY] = [ids.short(e[0]) for e in evaluated][:CAP]
        log.info("seeded %s baseline with %d acted-on id(s) (no alerts on first run)",
                 STATE_KEY, len(state[STATE_KEY]))
        return state

    seen = ids.normalize_seen(seen)
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = digested = tsunamis = 0

    for fid, tier, mag, dist, place, q_url, tsunami in evaluated:
        h = ids.short(fid)
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)
        msg = f"M{mag:.1f} - {place} (~{dist:.0f} km away)"
        if tsunami:
            # Supersedes the normal quake push: a big shallow quake in range.
            state = events.emit(
                state,
                title="Possible tsunami risk",
                body=(f"{msg}. Large shallow quake - check official advisories "
                      f"at tsunami.gov and move to high ground if instructed."),
                topic="quakes",
                severity="critical",
                source="Earthquakes",
                click_url="https://www.tsunami.gov/",
                tags="warning",
                legacy_priority="urgent",
                legacy_action="push",
            )
            tsunamis += 1
        elif tier == "live":
            state = events.emit(
                state,
                title="Earthquake nearby",
                body=msg,
                topic="quakes",
                severity="critical" if mag >= 6 else "high",
                source="Earthquakes",
                click_url=q_url or None,
                tags="ocean",
                legacy_priority="urgent" if mag >= 6 else "high",
                legacy_action="push",
            )
            pushed += 1
        else:
            state = events.emit(
                state,
                title=msg,
                topic="quakes",
                severity="moderate",
                source="Earthquakes",
                click_url=q_url,
                score=max(1, round(mag)),
                legacy_action="digest",
            )
            digested += 1

    if pushed or digested or tsunamis:
        log.info("quakes: %d live, %d digest, %d tsunami", pushed, digested, tsunamis)

    state[STATE_KEY] = (fresh + seen)[:CAP]
    return state
