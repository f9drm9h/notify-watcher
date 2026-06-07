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

import logging

from . import digest, ntfy, scoring

log = logging.getLogger(__name__)

Article = tuple[str, str, str, str]  # (article_id, headline, link, source)


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
) -> None:
    """Score and route one title's fresh articles. Mutates `bucket`/`state`.

    `bucket` is the topic's per-title seen-id map; `bucket[title]` is this
    title's seen-id list (None until the first run, which seeds silently).
    """
    seen = bucket.get(title)
    if seen is None:
        # Baseline-only first run: remember ids without alerting, so a newly
        # added title never blasts a backlog. Mirrors the collectors' seeding.
        bucket[title] = [a[0] for a in articles if a[0]][:cap]
        log.info("seeded news baseline for %r (no alerts on first run)", title)
        return

    tiers = scoring_cfg.get("source_tiers", {})
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = digested = dropped = 0

    for aid, headline, link, source in articles:
        if not aid or aid in seen_set:
            continue
        seen_set.add(aid)
        fresh.append(aid)  # recorded regardless of tier so it's never re-scored

        _score, tier = scoring.score(
            headline, _source_weight_key(source, tiers), [], scoring_cfg
        )
        if tier in ("breakthrough", "high"):
            ntfy.push(
                title=f"{live_title_prefix}: {title}",
                message=headline,
                click_url=link or None,
                tags=live_tag,
                priority="high",
            )
            pushed += 1
        elif tier == "moderate":
            # Group the daily digest by game/film title, not publisher.
            digest.add(state, {"title": headline, "url": link, "source": title}, digest_cfg)
            digested += 1
        else:
            dropped += 1

    if pushed or digested or dropped:
        log.info("news %r: %d live, %d digest, %d dropped", title, pushed, digested, dropped)

    # Newest-first, capped, so state.json plateaus.
    bucket[title] = (fresh + seen)[:cap]
