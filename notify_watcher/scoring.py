"""Deterministic importance scorer for domain-monitor items.

This is intentionally dumb and pure: no network, no LLM, no state. An item's
tier is a function only of (headline text, source weight, watch keywords) plus
the static weights in monitors.json, so the same item always scores the same
way and re-running a feed never changes a routing decision. Keeping importance
out of an LLM is what preserves deterministic dedup and keeps the project free.

The score is:

    score = source_weight
          + sum(bonus.weight  for each bonus group whose terms appear)
          + watch_match.weight if any domain keyword appears
          - sum(penalty.weight for each penalty group whose terms appear)

and the tier is the highest threshold the score meets:

    >= breakthrough  -> "breakthrough"   (live push, urgent)
    >= high          -> "high"           (live push, high)
    >= moderate      -> "moderate"       (daily digest)
    else             -> "minor"          (dropped)

Each group contributes its weight at most once, so a headline stuffed with
action words can't runaway-score; provenance (the source weight) stays the
dominant signal.
"""
from __future__ import annotations

from typing import Iterable

# Order matters: most-important first, so score_tier returns the top match.
_TIER_ORDER = ("breakthrough", "high", "moderate")


def _contains_any(haystack: str, terms: Iterable[str]) -> bool:
    return any(t in haystack for t in terms)


def score(title: str, source_weight_key: str, keywords: Iterable[str],
          cfg: dict) -> tuple[int, str]:
    """Return (score, tier) for one item. Never raises on a partial config."""
    text = (title or "").lower()
    weights = cfg.get("source_weights", {})
    total = int(weights.get(source_weight_key, 0))

    for name, group in (cfg.get("signal_bonuses") or {}).items():
        if not isinstance(group, dict):
            continue
        weight = int(group.get("weight", 0))
        if name == "watch_match":
            # The watch bonus fires when any domain keyword is in the headline.
            if _contains_any(text, (k.lower() for k in keywords)):
                total += weight
        elif _contains_any(text, (t.lower() for t in group.get("terms", []))):
            total += weight

    for group in (cfg.get("noise_penalties") or {}).values():
        if isinstance(group, dict) and _contains_any(
            text, (t.lower() for t in group.get("terms", []))
        ):
            total += int(group.get("weight", 0))  # weight is already negative

    thresholds = cfg.get("thresholds", {})
    for tier in _TIER_ORDER:
        if tier in thresholds and total >= int(thresholds[tier]):
            return total, tier
    return total, "minor"
