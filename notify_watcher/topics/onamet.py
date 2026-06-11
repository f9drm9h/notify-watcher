"""Topic: ONAMET/INDOMET severe-weather alerts for the Dominican Republic.

The Dominican meteorological office (ONAMET, renamed INDOMET in 2024 —
onamet.gob.do now redirects there) publishes its official watches ("alertas")
and warnings ("avisos") as a Common Alerting Protocol feed on the WMO Alert
Hub: an RSS index on S3 whose items each link to a structured CAP XML with
event, severity, onset/expiry, and the affected provinces. That feed is the
source here — it is the same data the website renders, but bot-wall-free and
machine-readable, so it survives the website's redesigns.

Every new alert pushes live immediately (this topic never buffers to the
digest): an AVISO — the office's highest level — rides the critical/urgent
band, an ALERTA the high band, anything else the default band.

The feed re-posts the SAME alert under a fresh guid every few minutes while
the forecaster revises it, so guid dedup alone would ring the phone all
afternoon. Dedup is therefore two-layered: a guid seen-list stops re-reading
old items, and a content key (normalized headline + description) held until
the alert's CAP expiry suppresses re-issuances of an alert we already pushed.
When the same text is issued again AFTER its predecessor expired, that is a
genuinely new alert and it pushes again. The first run seeds both silently.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import xml.etree.ElementTree as ET

import feedparser
import requests

from .. import config, events, ids

log = logging.getLogger(__name__)

STATE_SEEN = "onamet_seen_ids"
STATE_ACTIVE = "onamet_active"  # content key -> expiry ISO timestamp
SEEN_CAP = 300
DEFAULT_URL = "https://cap-sources.s3.amazonaws.com/do-indomet-es/rss.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notify-watcher/1.0)"}
# How many CAP detail files to fetch per run; new alerts arrive a handful at a
# time, so this caps a worst-case backlog without ever missing a fresh one.
MAX_DETAIL_FETCHES = 8
# Suppression window for an alert whose CAP detail (and thus real expiry)
# could not be read. INDOMET alerts typically run 24-48h.
DEFAULT_ACTIVE_HOURS = 48

_CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}


def _content_key(title: str, description: str) -> str:
    """Stable key for one alert's *content*, immune to re-issuance timestamps."""
    text = f"{title} {description}".lower()
    return ids.short(re.sub(r"\s+", " ", text).strip())


def _severity(headline: str, cap_severity: str = "") -> str:
    """Map an alert to our severity vocabulary.

    DR levels: AVISO (warning, the highest) -> critical; ALERTA (watch) ->
    high. The CAP severity field backstops headlines that name neither.
    """
    text = (headline or "").lower()
    cap_sev = (cap_severity or "").lower()
    if "aviso" in text or cap_sev == "extreme":
        return "critical"
    if "alerta" in text or cap_sev == "severe":
        return "high"
    return "moderate"


def _parse_cap(xml_text: str) -> dict:
    """Pure: extract the fields we use from one CAP 1.2 alert document.

    Returns {} on any parse failure — the RSS item alone is enough to alert,
    the CAP detail only enriches it.
    """
    try:
        root = ET.fromstring(xml_text)
        info = root.find("cap:info", _CAP_NS)
        if info is None:
            return {}

        def _text(tag: str) -> str:
            el = info.find(f"cap:{tag}", _CAP_NS)
            return (el.text or "").strip() if el is not None and el.text else ""

        areas = [
            (a.findtext("cap:areaDesc", "", _CAP_NS) or "").strip()
            for a in info.findall("cap:area", _CAP_NS)
        ]
        return {
            "event": _text("event"),
            "severity": _text("severity"),
            "urgency": _text("urgency"),
            "onset": _text("onset"),
            "expires": _text("expires"),
            "headline": _text("headline"),
            "description": _text("description"),
            "areas": [a for a in areas if a],
        }
    except ET.ParseError:
        return {}


def _fmt_areas(areas: list[str], limit: int = 6) -> str:
    """Pure: 'Santiago, Azua, ... (+3 more)' — provinces line for the push body."""
    shown = areas[:limit]
    extra = len(areas) - len(shown)
    line = ", ".join(shown)
    return f"{line} (+{extra} more)" if extra > 0 else line


def _is_expired(expires_iso: str, now: _dt.datetime) -> bool:
    """Pure: True when the stored expiry has passed. Unparseable -> expired,
    so a corrupt entry can suppress at most until it is pruned, never forever."""
    try:
        expires = _dt.datetime.fromisoformat(expires_iso)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_dt.timezone.utc)
        return expires <= now
    except (ValueError, TypeError):
        return True


def _prune_active(active: dict, now: _dt.datetime) -> dict:
    """Pure: drop content keys whose suppression window has passed."""
    return {k: v for k, v in active.items() if not _is_expired(v, now)}


def _default_expiry(now: _dt.datetime) -> str:
    return (now + _dt.timedelta(hours=DEFAULT_ACTIVE_HOURS)).isoformat()


def run(state: dict) -> dict:
    cfg = config.section("onamet")
    url = cfg.get("url") or DEFAULT_URL
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001 - a fetch/parse failure is non-fatal
        log.error("INDOMET CAP feed fetch failed: %s", exc)
        return state

    entries = feed.entries
    log.info("INDOMET: %d alert item(s) in feed", len(entries))

    now = _dt.datetime.now(_dt.timezone.utc)
    items = []  # (guid, title, description, link)
    for e in entries:
        guid = getattr(e, "id", "") or getattr(e, "link", "") or ""
        title = getattr(e, "title", "") or ""
        if not guid or not title:
            continue
        items.append((guid, title, getattr(e, "summary", "") or "",
                      getattr(e, "link", "") or ""))

    seen = state.get(STATE_SEEN)
    if seen is None:
        # Silent first run: remember every current item AND mark each distinct
        # alert's content as active so a revision minutes after deploy stays quiet.
        state[STATE_SEEN] = [ids.short(guid) for guid, *_ in items][:SEEN_CAP]
        state[STATE_ACTIVE] = {
            _content_key(title, desc): _default_expiry(now)
            for _, title, desc, _ in items
        }
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 STATE_SEEN, len(state[STATE_SEEN]))
        return state

    seen = ids.normalize_seen(seen)
    seen_set = set(seen)
    active = _prune_active(dict(state.get(STATE_ACTIVE) or {}), now)
    fresh: list[str] = []
    pushed = suppressed = fetched = 0

    for guid, title, desc, link in items:
        h = ids.short(guid)
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)

        key = _content_key(title, desc)
        if key in active:
            # A re-issuance/revision of an alert we already pushed and which
            # has not expired yet — stay quiet.
            suppressed += 1
            continue

        # Best-effort enrichment from the linked CAP XML (severity, provinces,
        # real expiry). The push goes out either way.
        cap: dict = {}
        if link.endswith(".xml") and fetched < MAX_DETAIL_FETCHES:
            fetched += 1
            try:
                detail = requests.get(link, headers=HEADERS, timeout=30)
                detail.raise_for_status()
                cap = _parse_cap(detail.text)
            except Exception as exc:  # noqa: BLE001 - enrichment only; never block the alert
                log.warning("CAP detail fetch failed for %s: %s", link, exc)

        severity = _severity(title, cap.get("severity", ""))
        body = (cap.get("description") or desc or title)[:400]
        areas = cap.get("areas") or []
        if areas:
            body = f"{body}\n\nProvincias: {_fmt_areas(areas)}"
        if cap.get("event"):
            body = f"{cap['event'].capitalize()}. {body}"

        state = events.emit(
            state,
            title=f"INDOMET: {title[:150]}",
            body=body,
            topic="onamet",
            severity=severity,
            source="ONAMET",
            click_url=link or None,
            tags="warning" if severity in ("critical", "high") else "cloud",
            legacy_priority={"critical": "urgent", "high": "high"}.get(severity),
            legacy_action="push",
        )
        pushed += 1
        active[key] = cap.get("expires") or _default_expiry(now)

    if pushed or suppressed:
        log.info("onamet: %d pushed, %d revision(s) suppressed", pushed, suppressed)

    state[STATE_SEEN] = (fresh + seen)[:SEEN_CAP]
    state[STATE_ACTIVE] = active
    return state
