"""Topic: auto-discover new Soundcore Liberty *Pro* flagship earbuds.

The deals topic only price-watches a fixed list of URLs, so it can't notice a
product that doesn't exist yet. This topic closes that gap for the Liberty Pro
line: it reads Soundcore's product sitemap each run, keeps the flagship Pro
earbuds (Liberty N Pro / Pro Max), and alerts when a brand-new one appears —
e.g. a future "Liberty 6 Pro". Each discovery is also appended to
state["auto_products"], which the deals topic then price-tracks automatically.

First run seeds silently: it records the current catalog (Liberty 4 Pro, 5 Pro,
5 Pro Max) as the baseline WITHOUT alerting, so you only ever get pinged about
genuinely new releases going forward — which is what "all products moving
forward" means. The 5 Pro / 5 Pro Max you already track explicitly in
watchlist.json are unaffected; deals de-dupes by URL.

Source: https://www.soundcore.com/server-sitemap-index-products.xml (the
authoritative list of every product URL — far more stable than scraping a
marketing page). Matching is slug-based with an accessory/refurb/locale denylist
verified to isolate exactly the flagship Pro earbuds.
"""
from __future__ import annotations

import logging
import re
import time

import requests

from .. import events
from . import deals

log = logging.getLogger(__name__)

# The sitemap is generated on demand and occasionally 500s for data-center IPs
# (the GitHub Actions runner) even though product pages serve fine. Retry a few
# times with backoff before giving up, so a transient blip doesn't skip a run.
SITEMAP_HEADERS = {**deals.HEADERS, "Accept": "application/xml,text/xml,*/*"}
_RETRIES = 4
_BACKOFF = 3  # seconds, multiplied by attempt number


def _fetch_sitemap() -> str:
    last: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(SITEMAP_URL, headers=SITEMAP_HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last = exc
            log.warning("sitemap fetch attempt %d/%d failed: %s", attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                time.sleep(_BACKOFF * attempt)
    raise RuntimeError(f"sitemap unreachable after {_RETRIES} attempts: {last}")

SITEMAP_URL = "https://www.soundcore.com/server-sitemap-index-products.xml"
PRODUCT_BASE = "https://www.soundcore.com/products/"
SEEN_KEY = "soundcore_pro_seen"   # list[str] of flagship slugs already known
AUTO_KEY = "auto_products"        # list[dict]; also consumed by deals.run

# A flagship Pro earbud slug contains "liberty" + "pro" and one of these
# product markers...
REQUIRE_ANY = ("earbuds", "-anc", "tws", "pro-max", "pro-anc")
# ...and none of these accessory / variant / refurb / localized tokens, which
# otherwise pollute the match (verified against the live sitemap).
DENY = (
    "replacement", "refurbished", "renewed", "tips", "-case", "charging",
    "ladecase", "power-bank", "without", "left-and-right", "ear-fins", "fins",
    "komfort", "ersatz", "ohr", "stopsel", "laptop", "prime", "semi-in-ear",
    "capsule", "eartips",
)


def _slug(url: str) -> str:
    return url.rsplit("/products/", 1)[-1].split("?")[0].strip("/").lower()


def _is_flagship_pro(slug: str) -> bool:
    if "liberty" not in slug or "pro" not in slug:
        return False
    if not any(k in slug for k in REQUIRE_ANY):
        return False
    return not any(d in slug for d in DENY)


def _current_slugs(xml: str) -> set[str]:
    """Distinct flagship-Pro slugs in the sitemap (locale variants collapse)."""
    return {
        s
        for loc in re.findall(r"<loc>(.*?)</loc>", xml)
        if _is_flagship_pro(s := _slug(loc))
    }


def _pretty(slug: str) -> str:
    """Fallback display name if the page can't be fetched: strip a leading
    model code and the trailing descriptor, title-case the rest."""
    s = re.sub(r"^(soundcore-)?[a-z]?\d{3,4}[a-z0-9]*-", "", slug)
    s = re.split(r"-(?:tws|anc|earbuds|ai|clear|wireless)", s, maxsplit=1)[0]
    return "Soundcore " + s.replace("-", " ").title()


def _describe(url: str, slug: str) -> tuple[str, str]:
    """Return (name, body) for a discovered product, enriching with the live
    page's name + price when reachable, else a slug-derived fallback."""
    name = _pretty(slug)
    body = "Just appeared in Soundcore's catalog."
    try:
        resp = requests.get(url, headers=deals.HEADERS, timeout=30)
        resp.raise_for_status()
        page_name = deals.extract_name(resp.text)
        if page_name:
            name = "Soundcore " + page_name
        price = deals._extract_price(resp.text)
        if price:
            body = f"Just released - current price {deals._fmt(*price)}. Now price-tracking it."
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        log.warning("could not enrich %s: %s", slug, exc)
    return name, body


def run(state: dict) -> dict:
    current = _current_slugs(_fetch_sitemap())
    log.info("flagship Liberty Pro products in catalog: %d", len(current))
    # Guard against an empty/garbled sitemap response (e.g. a WAF HTML page that
    # still returned 200) wiping nothing but also seeding nothing useful.
    if not current:
        log.warning("no flagship Pro slugs parsed from sitemap; skipping this run")
        return state

    seen_list = state.get(SEEN_KEY)
    # First run: seed the baseline silently so we only alert on future releases.
    if not seen_list:
        log.info("seeding Liberty Pro baseline (no alerts on first run)")
        state[SEEN_KEY] = sorted(current)
        return state

    seen = set(seen_list)
    new = sorted(current - seen)
    if not new:
        log.info("no new Liberty Pro products")
        return state

    auto: list = state.setdefault(AUTO_KEY, [])
    tracked_urls = {str(p.get("url", "")) for p in auto if isinstance(p, dict)}
    for slug in new:
        url = PRODUCT_BASE + slug
        try:
            name, body = _describe(url, slug)
            log.info("new Liberty Pro discovered: %s (%s)", name, slug)
            events.emit(
                state,
                title=f"New Soundcore Liberty Pro: {name}",
                body=body,
                topic="soundcore_pro",
                severity="moderate",
                source=name,
                click_url=url,
                tags="rocket",
                legacy_action="push",
            )
            if url not in tracked_urls:
                auto.append({"name": name, "url": url})
                tracked_urls.add(url)
            seen.add(slug)  # mark seen only after a successful alert
        except Exception as exc:  # noqa: BLE001 - isolate each product
            log.error("failed to announce %r: %s", slug, exc)

    # Persist exactly what we successfully announced (plus the prior baseline),
    # so a product whose alert failed is retried next run rather than lost.
    state[SEEN_KEY] = sorted(seen)
    return state
