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

`name` and `target_price` are optional; `url` is required. A note on Amazon:
Amazon frequently blocks data-center IPs (like GitHub Actions runners) and does
not always expose a clean JSON-LD price, so prefer the manufacturer/retailer
product page (soundcore.com, Best Buy, etc.) for reliable readings.
"""
from __future__ import annotations

import json
import logging

import requests
from bs4 import BeautifulSoup

from .. import ntfy, watchlist

log = logging.getLogger(__name__)

STATE_KEY = "product_prices"  # { "<url>": <last_price_float> }
# Some stores 403 a bare requests User-Agent; present as a normal browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


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
    """watchlist products + auto-discovered ones (state["auto_products"]),
    de-duplicated by URL with the watchlist entry winning (it may carry a
    target_price). The series-discovery topic populates auto_products."""
    merged: dict[str, dict] = {}
    for product in watchlist.entries("products") + list(state.get("auto_products", [])):
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

    for product in products:
        url = str(product.get("url") or "").strip()
        if not url:
            log.warning("product entry missing url: %r", product)
            continue
        name = str(product.get("name") or url).strip()
        target = product.get("target_price")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            found = _extract_price(resp.text)
            if found is None:
                log.warning("no JSON-LD price found for %r (%s)", name, url)
                continue
            price, currency = found
            log.info("product %r -> %s", name, _fmt(price, currency))

            previous = bucket.get(url)
            bucket[url] = price  # always store the latest reading

            if previous is None:
                ntfy.push(
                    title=f"Now tracking: {name}",
                    message=f"Current price: {_fmt(price, currency)}",
                    click_url=url,
                    tags="shopping",
                )
                continue

            meets_target = target is not None and price <= float(target)
            dropped = price < previous
            if dropped:
                line = f"Price dropped: {_fmt(previous, currency)} -> {_fmt(price, currency)}"
                if meets_target:
                    line += f" (at or below your target {_fmt(float(target), currency)})"
                ntfy.push(
                    title=f"Deal: {name}",
                    message=line,
                    click_url=url,
                    tags="moneybag",
                )
            # A price rise just updates the baseline above; no notification.
        except Exception as exc:  # noqa: BLE001 - isolate each product
            log.error("product %r check failed: %s", name, exc)

    return state
