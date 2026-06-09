"""Shared importance scoring + routing for per-title news topics (games, movies).

The *collection* side — the Google News query, the token-subset relevance
filter, per-title dedup, silent first-run seeding — stays in each topic, which
already owns a per-title "seen ids" bucket. This module owns the *decision*:
given the per-title pool of fresh, already-relevance-filtered articles, score
each one deterministically (scoring.score) and route it by tier:

    breakthrough / high -> live push (priority "high")
    moderate            -> daily digest buffer (digest.add)
    minor               -> dropped

Crucially, EVERY evaluated id is recorded as seen — pushed, digested, and
dropped alike — so a dropped or digested article is never re-scored (and never
re-digested) on the next run. This mirrors monitor.run_source's seen-list
semantics; the older "store only what we pushed" behaviour would have re-routed
dropped articles every run once scoring was added.

An article is a 4-tuple ``(article_id, headline, link, source)`` where `source`
is the publisher label from the feed (used for provenance weighting). Scoring
config is a topic-specific section of monitors.json (e.g. "games_scoring") so
each topic tunes its own keywords/weights without code changes.
"""
from __future__ import annotations

import calendar
import logging
import time

from . import events, ids, scoring

log = logging.getLogger(__name__)

Article = tuple[str, str, str, str]  # (article_id, headline, link, source)

# Shared freshness window for the Google News-based topics (monitors.json ->
# news.max_age_days). Google News search results routinely resurface months- or
# years-old articles under brand-new URLs; since dedup is id/URL-based, those
# resurfaced items look fresh and alert ("Claude Pro launches", 2023). Age-gating
# at collection time is what actually fixes that.
DEFAULT_MAX_AGE_DAYS = 14


def is_recent(entry, max_age_days: float, now: float | None = None) -> bool:
    """True if a feed entry was published within ``max_age_days`` (or is undated).

    Reads feedparser's ``published_parsed``/``updated_parsed`` struct_time (UTC).
    Undated or unparseable entries pass — the id dedup still guards them — and
    ``max_age_days <= 0`` disables the gate, so a config typo can only ever let
    more through, never silently filter everything out. ``now`` is an epoch
    override for tests.
    """
    try:
        if not max_age_days or float(max_age_days) <= 0:
            return True
    except (TypeError, ValueError):
        return True
    st = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not st:
        return True
    try:
        published = calendar.timegm(st)
    except (TypeError, ValueError, OverflowError):
        return True
    now_ts = time.time() if now is None else now
    return (now_ts - published) <= float(max_age_days) * 86400


def _source_weight_key(source: str, tiers: dict) -> str:
    """Map a publisher label to a source-weight key via config substring lists.

    `tiers` is {weight_key: [name_fragment, ...]}; the first key with a fragment
    found (case-insensitive substring) in the source wins, else "unknown". This
    is what lets an official channel ("PlayStation Blog") or a credible outlet
    ("Bloomberg") outscore an anonymous blog on an otherwise identical headline.
    """
    s = (source or "").lower()
    for key, names in tiers.items():
        if isinstance(names, list) and any(str(n).lower() in s for n in names):
            return key
    return "unknown"


def route(
    state: dict,
    *,
    bucket: dict,
    title: str,
    articles: list[Article],
    scoring_cfg: dict,
    digest_cfg: dict,
    cap: int,
    live_tag: str,
    live_title_prefix: str,
    topic: str = "",
) -> None:
    """Score and route one title's fresh articles. Mutates `bucket`/`state`.

    `bucket` is the topic's per-title seen-id map; `bucket[title]` is this
    title's seen-id list (None until the first run, which seeds silently).

    Routing goes through events.emit (the Personal Priority Engine funnel): the
    within-domain tier becomes the Event severity and emit decides push vs.
    digest vs. drop. With no `priority` section the engine is OFF and emit
    reproduces the legacy routing exactly. `topic` is the engine's cross-topic
    rule key (e.g. "games", "movies"); it is ignored while the engine is off.
    """
    seen = bucket.get(title)
    if seen is None:
        # Baseline-only first run: remember ids without alerting, so a newly
        # added title never blasts a backlog. Mirrors the collectors' seeding.
        bucket[title] = [ids.short(a[0]) for a in articles if a[0]][:cap]
        log.info("seeded news baseline for %r (no alerts on first run)", title)
        return

    # Seen-lists store short hashes, not raw article ids; normalize_seen migrates
    # any legacy raw ids the first time we see them so dedup stays exact.
    seen = ids.normalize_seen(seen)
    tiers = scoring_cfg.get("source_tiers", {})
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = digested = dropped = 0

    for aid, headline, link, source in articles:
        if not aid:
            continue
        h = ids.short(aid)
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)  # recorded regardless of tier so it's never re-scored

        score, tier = scoring.score(
            headline, _source_weight_key(source, tiers), [], scoring_cfg
        )
        # The Event's source is the game/film TITLE (the 4th-tuple publisher is
        # only a scoring weight). That title drives both the legacy push label
        # "<prefix>: <title>" (via the title_prefix hint) and the digest grouping,
        # matching the pre-emit behavior byte-for-byte while the engine is off.
        if tier in ("breakthrough", "high"):
            events.emit(
                state,
                title=headline,
                topic=topic,
                severity="critical" if tier == "breakthrough" else "high",
                source=title,
                click_url=link or None,
                tags=live_tag,
                metadata={"title_prefix": live_title_prefix},
                legacy_priority="high",
                legacy_action="push",
                score=score,
                digest_cfg=digest_cfg,
            )
            pushed += 1
        elif tier == "moderate":
            # Group the daily digest by game/film title, not publisher; the score
            # lets the flush rank items across all sources.
            events.emit(
                state,
                title=headline,
                topic=topic,
                severity="moderate",
                source=title,
                click_url=link or None,
                metadata={"title_prefix": live_title_prefix},
                legacy_action="digest",
                score=score,
                digest_cfg=digest_cfg,
            )
            digested += 1
        else:
            dropped += 1

    if pushed or digested or dropped:
        log.info("news %r: %d live, %d digest, %d dropped", title, pushed, digested, dropped)

    # Newest-first, capped, so state.json plateaus.
    bucket[title] = (fresh + seen)[:cap]
