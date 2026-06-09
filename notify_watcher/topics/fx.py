"""Topic: USD -> DOP exchange-rate threshold alert (open.er-api, free, no key).

open.er-api.com returns daily reference rates for a base currency without a key.
We watch the configured pair (default USD->DOP) and alert only when the rate
crosses *out of* or *back into* the [low, high] band you set in monitors.json ->
fx. Tracking the zone (below / within / above) and alerting on transitions keeps
this quiet during normal drift and useful for timing remittances or large
purchases. The first run records the current zone silently.
"""
from __future__ import annotations

import logging

import requests

from .. import changes, config, events

log = logging.getLogger(__name__)

STATE_KEY = "fx_last_zone"
RATE_KEY = "fx_last_rate"
API_URL = "https://open.er-api.com/v6/latest/{base}"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}


def _zone(rate: float, low: float, high: float) -> str:
    if rate < low:
        return "below"
    if rate > high:
        return "above"
    return "within"


def _evaluate(rate, cfg: dict, prev_zone: str | None) -> tuple[bool, str, str]:
    """Pure. Returns (alert?, zone, band) where ``band`` is a short clause naming the
    threshold crossed; the *magnitude* of the move is rendered separately in ``run``
    via ``changes.diff``. No alert on the first observation."""
    if rate is None:
        return False, prev_zone or "", ""
    low, high = float(cfg.get("low", 0)), float(cfg.get("high", 10 ** 9))
    zone = _zone(float(rate), low, high)
    if prev_zone is None or zone == prev_zone:
        return False, zone, ""
    if zone == "above":
        band = f"above {high:.2f}"
    elif zone == "below":
        band = f"below {low:.2f}"
    else:  # back within the band
        band = f"back in range ({low:.2f}-{high:.2f})"
    return True, zone, band


def run(state: dict) -> dict:
    cfg = config.section("fx")
    base = cfg.get("base", "USD")
    quote = cfg.get("quote", "DOP")
    try:
        resp = requests.get(API_URL.format(base=base), headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        rate = (data.get("rates") or {}).get(quote)
    except Exception as exc:  # noqa: BLE001 - a fetch/parse failure is non-fatal
        log.error("FX fetch failed: %s", exc)
        return state

    if rate is None:
        log.warning("FX: no rate for %s in response", quote)
        return state

    prev_rate = state.get(RATE_KEY)
    alert, zone, band = _evaluate(rate, cfg, state.get(STATE_KEY))
    log.info("FX: %s/%s = %.4f -> zone %s; alert=%s", base, quote, rate, zone, alert)

    if alert:
        # Render HOW it moved (magnitude) via the shared framework, then append the
        # band context the zone logic produced. The stored prior rate is present on
        # any transition (only the first-ever observation lacks one, and that never
        # alerts), so the fallback is just defensive.
        ch = (changes.diff(prev_rate, rate, label=f"{base}/{quote}",
                           fmt=lambda r: f"{r:.2f}")
              if prev_rate is not None else None)
        body = f"{ch.summary}, now {band}" if ch else f"{base}/{quote} at {rate:.2f}, {band}"
        state = events.emit(
            state,
            title=f"{base}/{quote} rate",
            body=body,
            change=ch,
            topic="fx",
            severity="moderate",
            source="FX",
            tags="moneybag",
            legacy_priority="default",
            legacy_action="push",
        )
    # Always record the latest zone + rate so the next transition is detected and can
    # report its magnitude (and the first run seeds both without alerting).
    state[STATE_KEY] = zone
    state[RATE_KEY] = rate
    return state
