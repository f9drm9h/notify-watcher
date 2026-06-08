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

from .. import config, ntfy

log = logging.getLogger(__name__)

STATE_KEY = "fx_last_zone"
API_URL = "https://open.er-api.com/v6/latest/{base}"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}


def _zone(rate: float, low: float, high: float) -> str:
    if rate < low:
        return "below"
    if rate > high:
        return "above"
    return "within"


def _evaluate(rate, cfg: dict, prev_zone: str | None) -> tuple[bool, str, str]:
    """Pure. Returns (alert?, zone, message). No alert on the first observation."""
    if rate is None:
        return False, prev_zone or "", ""
    low, high = float(cfg.get("low", 0)), float(cfg.get("high", 10 ** 9))
    base, quote = cfg.get("base", "USD"), cfg.get("quote", "DOP")
    zone = _zone(float(rate), low, high)
    if prev_zone is None or zone == prev_zone:
        return False, zone, ""
    if zone == "above":
        msg = f"{base}/{quote} rose to {rate:.2f}, above {high:.2f}."
    elif zone == "below":
        msg = f"{base}/{quote} fell to {rate:.2f}, below {low:.2f}."
    else:  # back within the band
        msg = f"{base}/{quote} is back in range at {rate:.2f} ({low:.2f}-{high:.2f})."
    return True, zone, msg


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

    alert, zone, msg = _evaluate(rate, cfg, state.get(STATE_KEY))
    log.info("FX: %s/%s = %.4f -> zone %s; alert=%s", base, quote, rate, zone, alert)

    if alert:
        ntfy.push(
            title=f"{base}/{quote} rate",
            message=msg,
            tags="moneybag",
            priority="default",
        )
    # Always record the latest zone so the next transition is detected (and the
    # first run seeds without alerting).
    state[STATE_KEY] = zone
    return state
