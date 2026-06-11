"""Topic: weekly grocery deals — La Sirena, Nacional, and Bravo.

Each chain exposes its weekly promotions very differently, so there is one
small collector per store feeding a shared dedup/severity pipeline:

* **La Sirena** (sirena.do, VTEX) — the homepage "Especiales del día" banners
  point at a VTEX product cluster (id 144 as of 2026-06). The public
  intelligent-search API returns that cluster as JSON: product name, link, and
  a priceRange whose listPrice vs sellingPrice gives the discount with no HTML
  scraping at all. Paged; `max_pages` bounds the requests per run.
* **Nacional** (supermercadosnacional.com, Magento) — the /ofertas landing is
  static HTML; each `li.product-item` carries the name, the link, and
  `data-price-amount` values under `special-price` / `old-price`. Only items
  showing a real cut (both prices) are taken, so the catalog filler around the
  offer carousels never alerts.
* **Bravo** (superbravo.com.do, WordPress) — the site publishes NO product or
  price data anywhere: /ofertas/ is an empty shell and the weekly specials are
  image flyers (verified 2026-06-11; their offers effectively live on social
  media). The only machine-readable signal is the "PROMOS <year>" nav menu, so
  Bravo's collector watches it and digests a heads-up when a new campaign page
  appears (e.g. "DE VACAS CON PAPÁ"), linking to it.

Routing leans on the priority engine: a discount of `big_discount_pct` or more
(default 30%) is a significant deal — severity high, which scores above the
push threshold; `mid_discount_pct`..big is moderate and anything else low,
both of which buffer into the daily digest. Dedup is per (store, product,
price), so the same offer never repeats but a *deeper* cut on a known product
alerts again. Each store seeds its currently published offers silently the
first time it is successfully collected, and a store that fails to fetch is
skipped without touching its baseline.

Daily-only (NOTIFY_DAILY): weekly offer pools do not change run-to-run.
"""
from __future__ import annotations

import logging
import os
import re

import requests
from bs4 import BeautifulSoup

from .. import config, events, ids

log = logging.getLogger(__name__)

STATE_KEY = "grocery_seen"  # { "<store>": [key, ...] } — per-store baselines
CAP = 800  # per store
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-DO,es;q=0.9,en;q=0.8",
}

SIRENA_BASE = "https://www.sirena.do"
SIRENA_API = (SIRENA_BASE + "/api/io/_v/api/intelligent-search/product_search"
              "/productClusterIds/{cluster}?count=50&page={page}")
NACIONAL_URL = "https://supermercadosnacional.com/ofertas"
BRAVO_URL = "https://superbravo.com.do/"

DEFAULT_BIG_PCT = 30.0  # >= this % off: significant deal -> severity high (push)
DEFAULT_MID_PCT = 15.0  # >= this % off: severity moderate; below: low (digest)
DEFAULT_SIRENA_CLUSTER = "144"  # "Especiales del día"
DEFAULT_MAX_PAGES = 4  # x50 products; the deepest cuts are merchandised early

# Bravo nav: the "PROMOS <year>" submenu's campaign pages.
_BRAVO_PROMO_RE = re.compile(r"promos\s*20\d{2}", re.IGNORECASE)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def _pct_off(price: float, list_price: float | None) -> float:
    """Pure: percent discount, 0.0 when there is no (sane) list price."""
    if not list_price or list_price <= 0 or price >= list_price:
        return 0.0
    return (1.0 - price / list_price) * 100.0


def _severity(pct: float, big: float, mid: float) -> str:
    """Pure: discount % -> severity; the priority rules turn high into a push."""
    if pct >= big:
        return "high"
    if pct >= mid:
        return "moderate"
    return "low"


def _deal_key(store: str, url: str, price: float) -> str:
    """Pure: dedup key — one alert per (store, product, price). A further
    price cut changes the key, so a deepening deal alerts again."""
    return ids.short(f"grocery|{store}|{url}|{price:.2f}")


def _fmt_dop(price: float) -> str:
    return f"DOP {price:,.2f}"


def _deal_body(deal: dict) -> str:
    """Pure: 'DOP 5,995.00 (was DOP 7,495.00, -20%)' or just the price."""
    if deal["pct"] > 0:
        return (f"{_fmt_dop(deal['price'])} (was {_fmt_dop(deal['list_price'])}, "
                f"-{deal['pct']:.0f}%)")
    return _fmt_dop(deal["price"])


def _parse_sirena(payload: dict, base: str = SIRENA_BASE) -> list[dict]:
    """Pure: one intelligent-search JSON page -> deal dicts.

    priceRange.sellingPrice vs .listPrice (lowPrice of each) carries the
    discount; `link` is site-relative. Products missing a price are skipped.
    """
    deals: list[dict] = []
    for p in payload.get("products") or []:
        name = str(p.get("productName") or "").strip()
        link = str(p.get("link") or "").strip()
        pr = p.get("priceRange") or {}
        price = (pr.get("sellingPrice") or {}).get("lowPrice")
        listp = (pr.get("listPrice") or {}).get("lowPrice")
        if not name or not link or not isinstance(price, (int, float)) or price <= 0:
            continue
        listp = float(listp) if isinstance(listp, (int, float)) else None
        url = link if link.startswith("http") else base + link
        deals.append({"store": "La Sirena", "name": name, "url": url,
                      "price": float(price), "list_price": listp,
                      "pct": _pct_off(float(price), listp)})
    return deals


def _parse_nacional(html: str) -> list[dict]:
    """Pure: the /ofertas page -> deal dicts for items showing a real cut.

    Magento marks each product as li.product-item with the name/link on
    a.product-item-link and machine-readable data-price-amount values under
    the special-price / old-price wrappers. Items without BOTH prices are the
    catalog filler around the offer carousels — skipped, not deals.
    """
    deals: list[dict] = []
    for item in BeautifulSoup(html, "html.parser").select("li.product-item"):
        link = item.select_one("a.product-item-link")
        special = item.select_one("span.special-price [data-price-amount]")
        old = item.select_one("span.old-price [data-price-amount]")
        if link is None or special is None or old is None:
            continue
        name = link.get_text(strip=True)
        url = str(link.get("href") or "").strip()
        try:
            price = float(special["data-price-amount"])
            listp = float(old["data-price-amount"])
        except (ValueError, TypeError, KeyError):
            continue
        if not name or not url or price <= 0:
            continue
        deals.append({"store": "Nacional", "name": name, "url": url,
                      "price": price, "list_price": listp,
                      "pct": _pct_off(price, listp)})
    return deals


def _parse_bravo_promos(html: str) -> list[dict]:
    """Pure: Bravo's homepage nav -> [{name, url}] of promo-campaign pages.

    The campaign links sit under a "PROMOS <year>" menu item; everything under
    such a submenu is a campaign. The site has no price data (see module doc),
    so a new campaign page is the only weekly-offers signal Bravo emits.
    """
    soup = BeautifulSoup(html, "html.parser")
    campaigns: dict[str, dict] = {}
    for menu_item in soup.select("li"):
        top_link = menu_item.find("a", recursive=False)
        if top_link is None or not _BRAVO_PROMO_RE.search(top_link.get_text()):
            continue
        for a in menu_item.select("ul a[href]"):
            name = " ".join(a.get_text().split())
            url = str(a.get("href") or "").strip()
            # The site repeats its nav (desktop + mobile) with different
            # nesting, which can put the "PROMOS …" header itself inside a
            # submenu — the header is never a campaign.
            if _BRAVO_PROMO_RE.search(name):
                continue
            if name and url and url not in campaigns:
                campaigns[url] = {"name": name, "url": url}
    return list(campaigns.values())


# --------------------------------------------------------------------------
# Per-store collectors (network) — None means the store was unreachable
# --------------------------------------------------------------------------

def _collect_sirena(cfg: dict) -> list[dict] | None:
    cluster = str(cfg.get("cluster") or DEFAULT_SIRENA_CLUSTER)
    max_pages = int(cfg.get("max_pages", DEFAULT_MAX_PAGES))
    deals: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(SIRENA_API.format(cluster=cluster, page=page),
                                headers=HEADERS, timeout=40)
            resp.raise_for_status()
            got = _parse_sirena(resp.json())
        except Exception as exc:  # noqa: BLE001 - a fetch failure is non-fatal
            log.error("La Sirena page %d fetch failed: %s", page, exc)
            return deals or None  # keep what we have; None if nothing at all
        if not got:
            break  # past the last page
        deals.extend(got)
    return deals


def _collect_nacional(cfg: dict) -> list[dict] | None:
    url = cfg.get("url") or NACIONAL_URL
    try:
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        return _parse_nacional(resp.text)
    except Exception as exc:  # noqa: BLE001 - a fetch failure is non-fatal
        log.error("Nacional fetch failed: %s", exc)
        return None


def _collect_bravo(cfg: dict) -> list[dict] | None:
    url = cfg.get("url") or BRAVO_URL
    try:
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        return _parse_bravo_promos(resp.text)
    except Exception as exc:  # noqa: BLE001 - a fetch failure is non-fatal
        log.error("Bravo fetch failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# Shared pipeline
# --------------------------------------------------------------------------

def _emit_deal(state: dict, deal: dict, big: float, mid: float) -> dict:
    severity = _severity(deal["pct"], big, mid)
    return events.emit(
        state,
        title=f"{deal['store']} deal: {deal['name']}",
        body=_deal_body(deal),
        topic="groceries",
        severity=severity,
        source=deal["store"],
        click_url=deal["url"],
        tags="shopping_cart",
        # Engine off: a significant deal still pushes; the rest digests at a
        # within-domain score so the digest can rank them.
        legacy_action="push" if severity == "high" else "digest",
        score=int(deal["pct"]),
    )


def _emit_bravo_campaign(state: dict, campaign: dict) -> dict:
    return events.emit(
        state,
        title=f"Bravo: new promo — {campaign['name']}",
        body=("Bravo posted a new promotion campaign. Prices aren't published "
              "on their site, so tap to see the campaign page."),
        topic="groceries",
        severity="low",
        source="Bravo",
        click_url=campaign["url"],
        tags="shopping_cart",
        legacy_action="digest",
        score=10,
    )


def _store_run(state: dict, store: str, keyed: list[tuple[str, dict]],
               emit) -> dict:
    """Dedup one store's collected items against its baseline and emit the new
    ones. `keyed` is [(dedup_key, item)]; `emit(state, item) -> state`. A store
    absent from the baseline dict seeds silently (no alerts on its first
    successful collection)."""
    baselines: dict = state.setdefault(STATE_KEY, {})
    seen = baselines.get(store)
    if seen is None:
        baselines[store] = [k for k, _ in keyed][:CAP]
        log.info("groceries: seeded %s baseline with %d item(s) silently",
                 store, len(baselines[store]))
        return state
    seen = ids.normalize_seen(seen)
    seen_set = set(seen)
    fresh: list[str] = []
    for key, item in keyed:
        if key in seen_set:
            continue
        seen_set.add(key)
        fresh.append(key)
        state = emit(state, item)
    if fresh:
        log.info("groceries: %s -> %d new item(s)", store, len(fresh))
    baselines[store] = (fresh + seen)[:CAP]
    return state


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state

    cfg = config.section("groceries")
    big = float(cfg.get("big_discount_pct", DEFAULT_BIG_PCT))
    mid = float(cfg.get("mid_discount_pct", DEFAULT_MID_PCT))

    collected: list[tuple[str, list[tuple[str, dict]], object]] = []

    deals = _collect_sirena(cfg.get("sirena") or {})
    if deals is not None:
        collected.append((
            "La Sirena",
            [(_deal_key(d["store"], d["url"], d["price"]), d) for d in deals],
            lambda s, d: _emit_deal(s, d, big, mid)))

    deals = _collect_nacional(cfg.get("nacional") or {})
    if deals is not None:
        collected.append((
            "Nacional",
            [(_deal_key(d["store"], d["url"], d["price"]), d) for d in deals],
            lambda s, d: _emit_deal(s, d, big, mid)))

    campaigns = _collect_bravo(cfg.get("bravo") or {})
    if campaigns is not None:
        collected.append((
            "Bravo",
            [(ids.short(f"bravo-promo|{c['url']}"), c) for c in campaigns],
            _emit_bravo_campaign))

    for store, keyed, emit in collected:
        state = _store_run(state, store, keyed, emit)
    return state
