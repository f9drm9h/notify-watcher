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
marketing page). Matching anchors each slug to the flagship model shape
(Liberty N Pro / Pro Max) and accepts only standalone earbud descriptors after
it, so promotional bundles that append a second product or giveaway
(…-pro-max-anker-nano-cargador-usb-c…) and accessories (…-pro-charging-case)
are rejected by construction rather than chased with an ever-growing denylist.
"""
from __future__ import annotations

import logging
from html import unescape
import re
import time
from urllib.parse import urlparse

import requests

from .. import control, events
from . import deals

log = logging.getLogger(__name__)

SITEMAP_URL = "https://www.soundcore.com/server-sitemap-index-products.xml"
FALLBACK_SITEMAP_URL = "https://www.soundcore.com/server-sitemap-index-pages.xml"
PRODUCT_BASE = "https://www.soundcore.com/products/"
SEEN_KEY = "soundcore_pro_seen"   # list[str] of flagship slugs already known
AUTO_KEY = "auto_products"        # list[dict]; also consumed by deals.run

# The product sitemap is generated on demand and can timeout or 500 for
# data-center IPs (GitHub Actions) even when product pages serve fine. Retry the
# product sitemap briefly, then fall back to the pages sitemap, which currently
# carries model-code product/launch URLs such as
# /a3954-liberty-4-pro-tws-earbuds-pre-launch.
SITEMAP_HEADERS = {
    **deals.HEADERS,
    "Accept": "application/xml,text/xml,text/html;q=0.9,*/*;q=0.8",
}
_RETRIES = 2
_BACKOFF = 2  # seconds, multiplied by attempt number


def _fetch_url(url: str) -> str:
    resp = requests.get(url, headers=SITEMAP_HEADERS, timeout=20)
    resp.raise_for_status()
    text = resp.text or ""
    if "<loc" not in text:
        raise RuntimeError(f"{url} did not contain sitemap <loc> entries")
    return text


def _fetch_sitemap() -> str:
    last: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            return _fetch_url(SITEMAP_URL)
        except (requests.RequestException, RuntimeError) as exc:
            last = exc
            log.warning("product sitemap fetch attempt %d/%d failed: %s",
                        attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                time.sleep(_BACKOFF * attempt)

    try:
        log.warning("product sitemap unavailable; falling back to pages sitemap")
        return _fetch_url(FALLBACK_SITEMAP_URL)
    except (requests.RequestException, RuntimeError) as exc:
        raise RuntimeError(
            f"Soundcore sitemaps unreachable; product={last}; fallback={exc}"
        ) from exc

# A *standalone* flagship slug is the model name and nothing else:
#   liberty-4-pro-earbuds   liberty-5-pro-max-tws   soundcore-liberty-6-pro-anc
# i.e. "Liberty <N> Pro" (optionally "Pro Max"), an optional brand prefix, then
# ONLY product-type descriptors from the closed set below. Promotional bundles
# and accessories tack on a second product, brand, or part whose tokens aren't
# descriptors — e.g. "liberty-5-pro-max-anker-nano-cargador-usb-c-..." (a
# giveaway charger that 404s as a product page) or "liberty-4-pro-charging-case".
# Anchoring the whole slug to the model shape rejects those by construction
# instead of denylisting each new giveaway one token at a time.
_MODEL = re.compile(
    r"^(?:soundcore-)?(?:[a-z]?\d{3,4}[a-z0-9]*-)?liberty-\d+-pro(-max)?"
)
_DESCRIPTORS = frozenset({
    "earbuds", "tws", "anc", "wireless", "ai", "clear",
})
_TRAILING_PAGE_TOKENS = re.compile(r"-(?:pre-launch(?:-boa)?|boa)$")
_LOCALE_PREFIXES = {
    "ae-en", "ca", "cl", "de", "es", "eu", "fr", "pl", "sg", "uk",
}


def _slug(url: str) -> str:
    """Normalize product and launch-page URLs into a comparable slug."""
    path = urlparse(unescape(url)).path.strip("/")
    if "/products/" in path:
        slug = path.rsplit("/products/", 1)[-1]
    elif "/collections/" in path:
        slug = path.rsplit("/collections/", 1)[-1]
    else:
        parts = [p for p in path.split("/") if p]
        if parts and parts[0].lower() in _LOCALE_PREFIXES:
            parts = parts[1:]
        slug = parts[-1] if parts else path
    return _TRAILING_PAGE_TOKENS.sub("", slug.split("?", 1)[0].strip("/").lower())


def _is_flagship_pro(slug: str) -> bool:
    m = _MODEL.match(slug)
    if not m:
        return False
    has_max = m.group(1) is not None
    extras = [tok for tok in slug[m.end():].split("-") if tok]
    # Bare "liberty-N-pro" (no Max, no descriptor) is a collection/landing slug,
    # not a product page — don't track it.
    if not has_max and not extras:
        return False
    # Any token after the model that isn't a known descriptor means this is a
    # bundle / accessory / variant, not the standalone earbuds.
    return all(tok in _DESCRIPTORS for tok in extras)


def _current_slugs(xml: str) -> set[str]:
    """Distinct flagship-Pro slugs in the sitemap (locale variants collapse)."""
    return {
        s
        for loc in re.findall(r"<loc>(.*?)</loc>", xml)
        if _is_flagship_pro(s := _slug(loc))
    }


def _product_url(slug: str) -> str:
    return PRODUCT_BASE + slug


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
    try:
        current = _current_slugs(_fetch_sitemap())
    except Exception as exc:  # noqa: BLE001 - topic must degrade, not crash run
        log.warning("soundcore_pro skipped: could not fetch/parse sitemap: %s", exc)
        return state
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
        url = _product_url(slug)
        try:
            name, body = _describe(url, slug)
            # Offer registry (docs/design/05): the discovery is auto-tracked
            # (applied=True), and the push's [Not interested] button can undo
            # that with one tap. A previously-ignored product returns None —
            # the user already said no, so skip both the alert and tracking.
            oid = control.register_offer(
                state, "product", name, {"name": name, "url": url},
                applied=True)
            if oid is None:
                seen.add(slug)
                continue
            ignore = control.make_action("Not interested", f"IGNORE:{oid}")
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
                metadata={"actions": [ignore]} if ignore else None,
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
