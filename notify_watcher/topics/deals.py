"""Topic: price-drop watcher for a list of product pages (e.g. Soundcore).

For each product in watchlist.json["products"] we fetch its page and read the
price out of the embedded schema.org JSON-LD (`<script type="application/ld+json">`,
Product.offers.price). That is store-agnostic: any shop that publishes standard
Product structured data works without per-site scraping rules, including
soundcore.com and most major retailers.

We push when a product is first seen, whenever its price drops, and whenever a
configured `target_price` is reached. A price increase updates the stored value
silently (so the next drop is measured from the new baseline) without nagging
you. No API key is required, so this topic works out of the box.

watchlist.json shape for this topic:

    "products": [
      {"name": "Soundcore Liberty 4 NC", "url": "https://...", "target_price": 79.99}
    ]

`name` and `target_price` are optional; `url` is required. An optional `group`
ties multiple listings of the SAME product together (e.g. Costco + Amazon for
one backpack): every source is tracked independently — a drop at EITHER pushes
an alert — and each alert quotes the other sources' last known price so the
two can be compared at a glance.

A note on Amazon: product pages carry no schema.org Product JSON-LD, so when
the JSON-LD scan comes up empty on an amazon.* URL we fall back to reading the
buy-box price (`span.a-offscreen` inside the core-price block). Amazon also
intermittently blocks data-center IPs (like GitHub Actions runners); a blocked
fetch is logged and retried naturally on the next run.
"""
from __future__ import annotations

import json
import logging
import os
import re

import requests
from bs4 import BeautifulSoup

from .. import changes, events, health, watchlist

log = logging.getLogger(__name__)

TOPIC = "deals"
STATE_KEY = "product_prices"  # { "<url>": <last_price_float> }
# Some stores 403 a bare requests User-Agent, so present as a normal browser:
# beyond the UA, send the header set a real Chrome navigation carries (Accept,
# client hints, Sec-Fetch-*). This clears *basic* Cloudflare/Akamai bot checks;
# it cannot defeat IP-reputation/TLS-fingerprint blocks (see the Costco note
# below) — for those, set DEALS_PROXY (see _proxies). Accept-Encoding is left at
# "gzip, deflate" on purpose: advertising "br" without the brotli package
# installed would hand us undecodable bytes.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}


def _proxies() -> dict | None:
    """Optional egress proxy for stores that block the runner's data-center IP.

    Costco and a few others sit behind Akamai/Cloudflare bot protection that
    403s by IP reputation no matter what headers we send, so the only real fix
    is a different source IP. Rather than bake in a free public proxy — dead
    half the time, and a man-in-the-middle on every page we read — we honor an
    OPERATOR-supplied one: set ``DEALS_PROXY`` (or ``DEALS_PROXY_URL``) to a
    trusted rotating/residential endpoint and every fetch routes through it;
    leave it unset and fetches go direct, exactly as before. (requests also
    honors the standard ``HTTPS_PROXY`` env on its own.)
    """
    url = os.environ.get("DEALS_PROXY") or os.environ.get("DEALS_PROXY_URL")
    return {"http": url, "https": url} if url else None

# --- Multi-retailer coverage notes -----------------------------------------
# This topic is store-agnostic: any URL added to watchlist.json["products"]
# whose page exposes a schema.org Product (JSON-LD) price is tracked with no
# per-site code. Two retailers were evaluated for the Soundcore Liberty 5 Pro
# line and intentionally NOT added, because neither offers a free, static
# (no-JavaScript) price this fetcher can read:
#
#   * Best Buy (search/product pages): actively blocks non-browser/data-center
#     clients — requests are connection-reset or time out, and the GitHub
#     Actions runner's data-center IP is blocked harder still. Prices are
#     JavaScript-rendered, so there is no static JSON-LD/meta/microdata price.
#     Best Buy's Developer API is free but needs a registered API key (a new
#     secret wired into the workflow) and a different query-by-SKU code path,
#     so it's out of scope for this URL/JSON-LD pattern. Skipped.
#   * soundcore.com/liberty-5-pro-series: returns 200 but its only JSON-LD node
#     is a Corporation (no Product/ProductGroup, no og:price/product:price meta,
#     no microdata). It is a collection landing page linking ~199 products, not
#     a single trackable item. The individual Liberty 5 Pro / 5 Pro Max product
#     pages already in watchlist.json carry proper Product JSON-LD and cover
#     these products, so the series page is redundant. Skipped.
#
# Add new retailers by dropping a product-page URL into watchlist.json; if a
# store publishes standard Product structured data it works automatically.
#
#   * Costco (p/... product pages): behind Akamai bot protection — returns 403
#     to any non-browser client (verified 2026-06-11 with plain requests, full
#     Chrome header sets, and curl alike), and a GitHub Actions data-center IP
#     fares no better. The full browser header set HEADERS now sends clears
#     *basic* bot checks but NOT Akamai's IP-reputation block, which keys off the
#     source IP regardless of headers. The only real fix is a different egress
#     IP: set the DEALS_PROXY env (see _proxies) to a trusted rotating/residential
#     proxy and the fetch routes through it. (A free public proxy is deliberately
#     NOT baked in — they're unreliable and a man-in-the-middle risk.) There is
#     also no static JSON-LD to read even on a 200. Until a proxy is configured,
#     the Highland Tactical Foxtrot entry keeps the Costco URL as its primary
#     source so it resumes the moment Costco is reachable; each run logs the 403
#     as a bot wall and the Amazon listing (same `group`) carries the tracking.


def _iter_jsonld(soup: BeautifulSoup):
    """Yield every parsed JSON-LD object on the page (objects and list items)."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            yield from (d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            yield data
            # Some sites wrap nodes in an @graph array.
            graph = data.get("@graph")
            if isinstance(graph, list):
                yield from (d for d in graph if isinstance(d, dict))


def _price_from_offers(offers) -> tuple[float, str] | None:
    """Pull (price, currency) out of a schema.org offers value, or None."""
    if isinstance(offers, list):
        for off in offers:
            got = _price_from_offers(off)
            if got:
                return got
        return None
    if not isinstance(offers, dict):
        return None
    # AggregateOffer uses lowPrice; a plain Offer uses price.
    raw = offers.get("price") or offers.get("lowPrice")
    if raw is None:
        return None
    try:
        price = float(str(raw).replace(",", ""))
    except ValueError:
        return None
    currency = str(offers.get("priceCurrency") or "").strip()
    return price, currency


def _node_prices(node: dict):
    """Yield (price, currency) for a Product/ProductGroup JSON-LD node.

    Handles a plain Product (`offers`) and a Shopify-style ProductGroup whose
    real prices live on each `hasVariant` Product — so colour/size variants are
    all considered and the caller can pick the lowest.
    """
    got = _price_from_offers(node.get("offers"))
    if got:
        yield got
    variants = node.get("hasVariant")
    if isinstance(variants, list):
        for var in variants:
            if isinstance(var, dict):
                got = _price_from_offers(var.get("offers"))
                if got:
                    yield got


def _is_product_node(node: dict) -> bool:
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return bool({"Product", "ProductGroup"} & set(types)) or "offers" in node


def _extract_price(html: str) -> tuple[float, str] | None:
    """Return the lowest (price, currency) across all Product JSON-LD on a page."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[float, str]] = []
    for node in _iter_jsonld(soup):
        if _is_product_node(node):
            candidates.extend(_node_prices(node))
    return min(candidates, key=lambda c: c[0]) if candidates else None


# Amazon renders the buy-box price as e.g. "$59.99" or "DOP3,459.57" (currency
# symbol or ISO code, then a comma-grouped number).
_AMAZON_PRICE_RE = re.compile(r"^\s*(US\$|[A-Z]{2,3}|\$|€|£)?\s*(\d[\d,]*(?:\.\d+)?)\s*$")
_CURRENCY_SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP"}
# The struck-through list price and per-unit prices also use a-offscreen spans,
# so scope to the buy-box containers (in order of preference) instead of taking
# the first match on the page.
_AMAZON_PRICE_SELECTORS = (
    "#corePrice_feature_div span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div span.a-offscreen",
    "span.priceToPay span.a-offscreen",
    "#apex_desktop span.a-offscreen",
)


def _extract_amazon_price(html: str) -> tuple[float, str] | None:
    """Return (price, currency) from an Amazon buy box, or None.

    Amazon product pages publish no schema.org Product JSON-LD, so the generic
    extractor finds nothing there; this reads the rendered price instead. A
    CAPTCHA/blocked page simply has no buy box and yields None, which run()
    already logs as "no price found".
    """
    soup = BeautifulSoup(html, "html.parser")
    for selector in _AMAZON_PRICE_SELECTORS:
        node = soup.select_one(selector)
        if node is None:
            continue
        m = _AMAZON_PRICE_RE.match(node.get_text())
        if not m:
            continue
        symbol, number = m.group(1) or "", m.group(2)
        currency = _CURRENCY_SYMBOLS.get(symbol, symbol)
        return float(number.replace(",", "")), currency
    return None


def _amazon_no_price_diagnosis(html: str) -> str:
    """One log-friendly line saying WHY the Amazon fallback found nothing.

    Distinguishes "Amazon walled this client off" (CAPTCHA / sorry page —
    expected occasionally from a data-center IP, self-heals on a later run)
    from "the buy box exists but our selectors no longer match it" (markup
    change worth a code fix). Mirrors how the JSON-LD tests pin shapes: make
    a silent upstream change diagnosable from the runner log alone.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    low = html[:4000].lower()
    if "captcha" in low or "robot check" in low or "/errors/validatecaptcha" in low:
        return f"bot wall (CAPTCHA page, title={title!r})"
    markers = ", ".join(
        sel for sel in ("#corePrice_feature_div", "#corePriceDisplay_desktop_feature_div",
                        "span.priceToPay", "#apex_desktop", "span.a-offscreen")
        if soup.select_one(sel) is not None
    )
    return (f"page served but no buy-box price (title={title!r}, "
            f"{len(html)} bytes, markers present: {markers or 'none'})")


def _group_note(products: list[dict], bucket: dict, product: dict) -> str:
    """One clause comparing a grouped product's other sources, or "".

    For a product listed at several stores (same `group` value), quote each
    sibling's last stored price so a drop alert from one store reads against
    the other: "Costco: 39.99". Prices are quoted bare because the bucket only
    stores the number, not the currency it was read in.
    """
    group = str(product.get("group") or "").strip()
    if not group:
        return ""
    url = str(product.get("url") or "").strip()
    notes = [
        f"{sibling.get('name') or sibling_url}: {bucket[sibling_url]:.2f}"
        for sibling in products
        if (sibling_url := str(sibling.get("url") or "").strip()) != url
        and str(sibling.get("group") or "").strip() == group
        and bucket.get(sibling_url) is not None
    ]
    return f" | Compare {', '.join(notes)}" if notes else ""


def extract_name(html: str) -> str | None:
    """Return the product name from the first Product/ProductGroup JSON-LD.

    Soundcore names look like "Liberty 5 Pro Max | Earbuds with ..."; we keep
    the part before the marketing "| ..." tail. Used by the series-discovery
    topic to label a newly found product.
    """
    soup = BeautifulSoup(html, "html.parser")
    for node in _iter_jsonld(soup):
        name = node.get("name")
        if _is_product_node(node) and isinstance(name, str) and name.strip():
            return name.split("|")[0].strip()
    return None


def _fmt(price: float, currency: str) -> str:
    return f"{currency} {price:.2f}".strip() if currency else f"{price:.2f}"


def _merge_products(state: dict) -> list[dict]:
    """watchlist products + auto-discovered ones (state["auto_products"]) +
    reply-button ADDs (state["tracked_products"], see docs/design/05),
    de-duplicated by URL with the watchlist entry winning (it may carry a
    target_price). The series-discovery topic populates auto_products."""
    merged: dict[str, dict] = {}
    for product in (watchlist.entries("products")
                    + list(state.get("auto_products", []))
                    + list(state.get("tracked_products", []))):
        if not isinstance(product, dict):
            continue
        url = str(product.get("url") or "").strip()
        if url and url not in merged:
            merged[url] = product
    return list(merged.values())


def run(state: dict) -> dict:
    products = _merge_products(state)
    if not products:
        log.info("no products to track; nothing to do")
        return state

    bucket: dict = state.setdefault(STATE_KEY, {})
    prices_seen = 0
    last_check_error = ""

    for product in products:
        url = str(product.get("url") or "").strip()
        if not url:
            log.warning("product entry missing url: %r", product)
            continue
        name = str(product.get("name") or url).strip()
        target = product.get("target_price")
        try:
            resp = requests.get(url, headers=HEADERS, proxies=_proxies(),
                                timeout=30)
            if resp.status_code in (403, 429):
                # Akamai/Cloudflare IP block (typical for Costco from a CI IP).
                # Report it as a bot wall, not a generic crash, and move on: a
                # grouped sibling source (e.g. Amazon) still carries the price,
                # and DEALS_PROXY can route around it when configured.
                log.warning("%r: bot-walled (HTTP %d) at %s; set DEALS_PROXY to "
                            "fetch via a trusted proxy", name, resp.status_code, url)
                last_check_error = f"{name}: HTTP {resp.status_code} (bot wall)"
                continue
            resp.raise_for_status()
            found = _extract_price(resp.text)
            if found is None and "amazon." in url:
                found = _extract_amazon_price(resp.text)
                if found is None:
                    log.warning("no price for %r: %s (%s)", name,
                                _amazon_no_price_diagnosis(resp.text), url)
                    last_check_error = f"{name}: no price parsed"
                    continue
            if found is None:
                log.warning("no price found for %r (%s)", name, url)
                last_check_error = f"{name}: no price parsed"
                continue
            price, currency = found
            prices_seen += 1
            log.info("product %r -> %s", name, _fmt(price, currency))

            previous = bucket.get(url)
            bucket[url] = price  # always store the latest reading

            if previous is None:
                events.emit(
                    state,
                    title=f"Now tracking: {name}",
                    body=f"Current price: {_fmt(price, currency)}"
                         f"{_group_note(products, bucket, product)}",
                    topic="deals",
                    severity="low",
                    source=name,
                    click_url=url,
                    tags="shopping",
                    legacy_action="push",
                )
                continue

            meets_target = target is not None and price <= float(target)
            dropped = price < previous
            if dropped:
                # "how it moved" — the absolute + percent drop — via the shared
                # framework, with currency-aware value rendering.
                ch = changes.diff(previous, price, label=name,
                                  fmt=lambda p: _fmt(p, currency))
                line = f"Price dropped: {ch.summary}"
                if meets_target:
                    line += f" (at or below your target {_fmt(float(target), currency)})"
                line += _group_note(products, bucket, product)
                events.emit(
                    state,
                    title=f"Deal: {name}",
                    body=line,
                    change=ch,
                    topic="deals",
                    severity="moderate",
                    source=name,
                    click_url=url,
                    tags="moneybag",
                    legacy_action="push",
                )
            # A price rise just updates the baseline above; no notification.
        except Exception as exc:  # noqa: BLE001 - isolate each product
            log.error("product %r check failed: %s", name, exc)
            last_check_error = f"{name}: {exc}"

    # Health contract: ok while at least one product yielded a price;
    # source_failed when every check failed (network down, every page
    # bot-walled, or a layout change broke every parse).
    if prices_seen:
        health.source_ok(state, TOPIC, data_count=prices_seen)
    else:
        health.source_failed(
            state, TOPIC,
            f"no prices from {len(products)} product(s); last: {last_check_error}")

    return state
