"""Shared collector engine for scored domain monitors (FDA, energy, ...).

A collector topic's only job is to fetch a source and normalize it into a list
of items; this module does the rest, identically for every domain:

  1. First run (state key absent) -> silently seed the current item ids as the
     baseline and return, so adding a source never blasts a backlog of alerts.
     This mirrors the seeding in games.py / soundcore_pro.py.
  2. Otherwise, for each unseen item: score it deterministically (scoring.py),
     then route by tier:
        breakthrough -> live push, priority "urgent"
        high         -> live push, priority "high"
        moderate     -> daily digest buffer (digest.add)
        minor        -> dropped
     Every processed id is recorded as seen (even dropped ones) so it is not
     re-evaluated, then the seen list is capped newest-first to bound state.

An item is a dict: {"id", "title", "url", "source", "weight"}. `source` is a
human label shown in alerts/digest; `weight` is the provenance key for scoring
(falling back to `default_weight_key` when an item omits it), which lets one
collector mix sources of different authority under a single state key. Each
item is isolated in try/except so one bad entry never blocks the rest, matching
the per-topic resilience used throughout the project.
"""
from __future__ import annotations

import logging

from . import digest, ntfy, scoring

log = logging.getLogger(__name__)

_TIER_PRIORITY = {"breakthrough": "urgent", "high": "high"}
_TIER_TAG = {"breakthrough": "rotating_light", "high": "zap"}


def run_source(
    state: dict,
    *,
    state_key: str,
    items: list[dict],
    default_weight_key: str,
    keywords: list[str],
    scoring_cfg: dict,
    digest_cfg: dict,
    cap: int,
    live_title_prefix: str,
) -> dict:
    """Dedup, score, and route a collector's items. Returns updated state."""
    seen = state.get(state_key)
    if seen is None:
        # Baseline-only first run: remember ids without alerting.
        state[state_key] = [i["id"] for i in items if i.get("id")][:cap]
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 state_key, len(state[state_key]))
        return state

    seen_set = set(seen)
    fresh: list[str] = []
    pushed = digested = 0

    for item in items:
        try:
            iid = item.get("id")
            if not iid or iid in seen_set:
                continue
            seen_set.add(iid)
            fresh.append(iid)

            sc, tier = scoring.score(
                item.get("title", ""),
                item.get("weight", default_weight_key),
                keywords,
                scoring_cfg,
            )
            if tier == "minor":
                continue
            if tier in _TIER_PRIORITY:
                ntfy.push(
                    title=f"{live_title_prefix}: {item.get('source', '')}".strip(": "),
                    message=item.get("title", ""),
                    click_url=item.get("url") or None,
                    tags=_TIER_TAG[tier],
                    priority=_TIER_PRIORITY[tier],
                )
                pushed += 1
            else:  # moderate
                digest.add(state, {**item, "tier": tier, "score": sc}, digest_cfg)
                digested += 1
        except Exception as exc:  # noqa: BLE001 - isolate each item
            log.error("monitor item failed (%s): %s", state_key, exc)

    if pushed or digested:
        log.info("%s: %d live push(es), %d digested", state_key, pushed, digested)

    # Newest-first, capped, so state.json plateaus.
    state[state_key] = (fresh + seen)[:cap]
    return state
