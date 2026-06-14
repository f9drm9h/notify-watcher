"""Cross-topic Personal Priority Engine — a pure, deterministic event scorer.

Like ``scoring.py`` (the *within-domain* importance scorer for collector items),
this module is intentionally dumb and pure: no network, no LLM, no state. A
decision is a function only of (the normalized Event's fields, the monitors.json
``priority`` section), so the same event always routes the same way and a
re-run never changes a routing decision.

Where ``scoring.py`` answers "is this FDA/energy item notable *within its
domain*", this engine answers the *cross-topic* question "relative to everything
the user subscribes to, should this interrupt them now, wait for the daily
digest, or be dropped". A topic's Event carries a ``severity`` (its own sense of
importance) and the engine maps that — plus topic/source/keyword rules — onto a
single global priority score.

Resolution (mirrors scoring.py's additive, group-once style):

    base   = the FIRST matching rule's score, else cfg["default"]
    score  = base + each matching boost group's `add` (each group counts once)
    score  = an override's `set` value, if a matching override clamps it
    action = "push"   if score >= threshold       (ntfy priority from bands)
             "digest" if score >= digest_floor     (buffered for the daily flush)
             "drop"   otherwise

Rules are tried IN LIST ORDER and the first match wins, so the config author
controls precedence by putting more specific rules first (this matches the
``_comment`` documented in monitors.json).

THE BACKWARD-COMPAT KEYSTONE: an absent or empty ``priority`` section makes
``decide`` return ``None`` ("engine OFF"), and ``events.emit`` then falls back
to legacy routing — i.e. every topic keeps its exact pre-engine behavior until
the section is authored. As elsewhere in the project, a missing/partial/
malformed config never raises; each field is read defensively with a default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# Locked defaults (see design): used when the priority section exists but omits
# a given knob, so a partial config still routes sensibly instead of crashing.
_DEFAULT_THRESHOLD = 60
_DEFAULT_DIGEST_FLOOR = 25
_DEFAULT_BASE = 30
# Fallback ntfy band table if cfg omits urgency_bands: score -> ntfy priority name.
_DEFAULT_BANDS = {"90": "urgent", "70": "high"}
_HIGH_PRIORITY_FLOOR = 70

# Event fields a rule/override may constrain. All are matched EXACTLY (the
# controlled vocab of topic/severity, and the source label); fuzzy/substring
# matching lives in the boost groups, not here.
_MATCH_KEYS = ("topic", "severity", "source")

_ANTHROPIC_MODEL_TERMS = ("claude", "opus", "sonnet", "haiku", "model")
_ANTHROPIC_RELEASE_TERMS = (
    "introducing", "announce", "announcing", "release", "released", "launch",
    "available", "new",
)


@dataclass(frozen=True)
class Decision:
    """What the engine decided for one event.

    ``ntfy_priority`` is the banded ntfy priority name for a ``push`` action and
    ``None`` for ``digest``/``drop``. ``score`` is the final global priority,
    which the digest path stores so the daily digest ranks/evicts by cross-topic
    priority for free.
    """
    action: str  # "push" | "digest" | "drop"
    score: int
    ntfy_priority: Optional[str]
    reason: str = ""


def _int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _contains_any(haystack: str, terms: Iterable[str]) -> bool:
    return any(t in haystack for t in terms)


def _rule_matches(rule: dict, event) -> bool:
    """True if every constraint the rule names equals the event's field.

    A rule that names none of _MATCH_KEYS matches everything (a catch-all); a
    rule naming several requires all of them to match (logical AND).
    """
    for key in _MATCH_KEYS:
        if key in rule and rule[key] != getattr(event, key):
            return False
    return True


def _is_anthropic_model_release(event) -> bool:
    """True for official Anthropic model-release announcements.

    The Anthropic topic already filters to source="Anthropic"; this core-layer
    guard exists because those posts are time-sensitive even when the topic emits
    moderate severity. Raising their score into the high ntfy band lets them ring
    through quiet hours without teaching the topic scraper about delivery policy.
    """
    if getattr(event, "topic", "") != "anthropic_news":
        return False
    if "anthropic" not in str(getattr(event, "source", "")).lower():
        return False
    text = f"{getattr(event, 'title', '')}\n{getattr(event, 'body', '')}".lower()
    return (
        _contains_any(text, _ANTHROPIC_MODEL_TERMS)
        and _contains_any(text, _ANTHROPIC_RELEASE_TERMS)
    )


def _ntfy_priority(score: int, bands: object) -> str:
    """Map a score to an ntfy priority via the highest band whose key it meets."""
    if not isinstance(bands, dict):
        bands = _DEFAULT_BANDS
    best_name = "default"
    best_key = -1
    for key, name in bands.items():
        ik = _int(key, default=-1)
        if ik < 0:
            continue
        if score >= ik and ik > best_key:
            best_key = ik
            best_name = str(name)
    return best_name


def decide(event, cfg: dict) -> Optional[Decision]:
    """Route one Event. Returns None when the engine is OFF (legacy fallback).

    Never raises on a partial/malformed config: every field is read with a
    default, matching the fail-soft contract used across the project.
    """
    if not isinstance(cfg, dict) or not cfg:
        return None  # engine OFF -> caller uses legacy routing

    threshold = _int(cfg.get("threshold"), _DEFAULT_THRESHOLD)
    floor = _int(cfg.get("digest_floor"), _DEFAULT_DIGEST_FLOOR)
    base_default = _int(cfg.get("default"), _DEFAULT_BASE)

    # 1) Base score: first matching rule wins (author-ordered precedence).
    score = base_default
    for rule in cfg.get("rules") or []:
        if isinstance(rule, dict) and _rule_matches(rule, event):
            score = _int(rule.get("score"), base_default)
            break

    # 2) Boosts: each group adds at most once (parallels scoring.py).
    text = f"{event.title}\n{event.body}".lower()
    for boost in cfg.get("keyword_boosts") or []:
        if isinstance(boost, dict) and _contains_any(
            text, (str(t).lower() for t in boost.get("terms", []))
        ):
            score += _int(boost.get("add"), 0)

    src = (event.source or "").lower()
    for boost in cfg.get("source_boosts") or []:
        if not isinstance(boost, dict):
            continue
        want = str(boost.get("source", "")).lower()
        if want and want in src:  # forgiving substring match on the source label
            score += _int(boost.get("add"), 0)

    for boost in cfg.get("severity_boosts") or []:
        if isinstance(boost, dict) and boost.get("severity") == event.severity:
            score += _int(boost.get("add"), 0)

    if _is_anthropic_model_release(event):
        score = max(score, _HIGH_PRIORITY_FLOOR)

    # 3) Overrides: a matching user-defined rule clamps the final score.
    for override in cfg.get("overrides") or []:
        if isinstance(override, dict) and "set" in override and _rule_matches(override, event):
            score = _int(override.get("set"), score)
            break

    # 4) Band the result.
    if score >= threshold:
        return Decision("push", score, _ntfy_priority(score, cfg.get("urgency_bands")))
    if score >= floor:
        return Decision("digest", score, None)
    return Decision("drop", score, None, f"score {score} below digest floor {floor}")
