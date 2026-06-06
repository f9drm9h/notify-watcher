"""Topic: energy / electricity domain monitor (EIA, IEA, NRC, ...).

Reads each RSS source listed in monitors.json -> energy.sources, normalizes
entries to monitor items, and hands them to the shared collector engine, which
dedups by article id, scores deterministically, and routes by tier (live push
for high/breakthrough, daily digest for moderate, dropped for minor). No API
key is required, so this topic works out of the box.

Adding or removing a source is a monitors.json edit, not a code change.
"""
from __future__ import annotations

import logging

import feedparser
import requests

from .. import config, monitor

log = logging.getLogger(__name__)

STATE_KEY = "energy_seen_ids"
CAP = 300
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notify-watcher/1.0)"}


def _fetch(url: str) -> list:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return feedparser.parse(resp.content).entries


def run(state: dict) -> dict:
    cfg = config.section("energy")
    sources = cfg.get("sources") or []
    if not sources:
        log.info("no energy sources configured; nothing to do")
        return state

    keywords = cfg.get("keywords") or []
    scoring_cfg = config.section("scoring")
    digest_cfg = config.section("digest")

    items: list[dict] = []
    for src in sources:
        if not isinstance(src, dict) or not src.get("url"):
            continue
        name = src.get("name", "Energy")
        weight = src.get("weight", "trade")
        try:
            entries = _fetch(src["url"])
        except Exception as exc:  # noqa: BLE001 - one source failing is non-fatal
            log.warning("energy source %r failed: %s", name, exc)
            continue
        for e in entries:
            iid = getattr(e, "id", "") or getattr(e, "link", "")
            if not iid:
                continue
            items.append({
                "id": iid,
                "title": getattr(e, "title", ""),
                "url": getattr(e, "link", ""),
                "source": name,
                "weight": weight,
            })
        log.info("energy source %r: %d entries", name, len(entries))

    # One call across all sources: each item carries its own provenance weight,
    # so the engine seeds the baseline once and scores mixed-authority sources
    # correctly under a single state key.
    return monitor.run_source(
        state,
        state_key=STATE_KEY,
        items=items,
        default_weight_key="trade",
        keywords=keywords,
        scoring_cfg=scoring_cfg,
        digest_cfg=digest_cfg,
        cap=CAP,
        live_title_prefix="Energy",
    )
