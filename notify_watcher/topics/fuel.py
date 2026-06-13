"""Topic: DR weekly fuel prices (MICM official notice, no key).

The Dominican Republic sets official fuel prices once a week. That duty used to
sit with the CNPE (Comisión Nacional de Política Energética); since Law 37-17 it
belongs to the MICM (Ministerio de Industria, Comercio y MiPymes), which posts an
"Aviso Semanal de Precios de Combustibles" PDF every Friday for the week starting
Saturday — cnpe.gob.do itself no longer resolves. We scrape the avisos listing
page for the newest PDF (`monitors.json` -> `fuel`), pull the consumer fuels'
official RD$/gal prices out of its table, and diff against last week via
``changes.diff``: any move of ``push_pct`` (default 5%) or more pushes live,
smaller moves (and flat weeks) land one calm line in the daily digest, and the
first run seeds silently. Dedup is URL + PDF content hash: MICM has reused a
notice URL before, so a same-URL notice is parsed again unless its stored hash
also matches. Daily-only (NOTIFY_DAILY) — prices change once a week, so polling
every cycle buys nothing.

Price extraction leans on the table's arithmetic rather than column positions
(pypdf text columns are unreliable): every row is importation parity + taxes +
margins = official price, so the official price is always the LARGEST number in
its row. The week-over-week "variación" column is small and the parenthesized
negatives lose their sign under ``float`` coercion anyway, so the heuristic holds
for every fuel including GLP (whose row appends a post-adjustment final price,
also the maximum).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import html as _html
import io
import logging
import os
import re

import requests

from .. import changes, config, events, health

log = logging.getLogger(__name__)

TOPIC = "fuel"
STATE_KEY = "fuel_prices"          # {fuel name: official RD$/gal} from the last notice
LAST_PDF_KEY = "fuel_last_pdf"     # URL of the last notice we reported
LAST_PDF_HASH_KEY = "fuel_last_pdf_hash"
LAST_PRICES_SEEN_AT_KEY = "fuel_last_prices_seen_at"
DEFAULT_PAGE = ("https://micm.gob.do/direcciones/combustibles/"
                "avisos-semanales-de-precios/avisos-semanales-de-precios-de-combustibles/")
DEFAULT_PUSH_PCT = 5.0
# The MICM WAF 403s plain bot user agents; a desktop UA gets the public page.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-DO,es;q=0.9,en;q=0.8",
}

# Consumer fuels we track, in display order. The negative lookahead keeps
# "Gasoil Regular" from also matching the EGP power-generation rows, and the
# GLP pattern tolerates whatever the PDF extractor does to the accented "ó".
FUELS: list[tuple[str, re.Pattern]] = [
    ("Gasolina Premium", re.compile(r"Gasolina\s+Premium", re.I)),
    ("Gasolina Regular", re.compile(r"Gasolina\s+Regular", re.I)),
    ("Gasoil Regular", re.compile(r"Gasoil\s+Regular(?!\s+EGP)", re.I)),
    ("Gasoil Óptimo", re.compile(r"Gasoil\s+[ÓO]ptimo", re.I)),
    ("Kerosene", re.compile(r"Kerosene", re.I)),
    ("GLP", re.compile(r"Gas\s+Licuado\s+de\s+Petr.leo", re.I)),
]

_PDF_LINK = re.compile(r'href="([^"]+/wp-content/uploads/[^"]*AVISO[^"]*\.pdf[^"]*)"', re.I)
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}")


def _find_pdf(html: str) -> str | None:
    """Newest weekly-notice PDF URL on the listing page (the page lists newest
    first), or None when the markup changed and no notice link is found."""
    m = _PDF_LINK.search(html)
    return _html.unescape(m.group(1)) if m else None


def _parse_prices(text: str) -> dict[str, float]:
    """Pure: extract {fuel: official RD$/gal} from the notice's text.

    Scans line by line; the first line matching a fuel's pattern that carries at
    least three numeric cells is its table row (the prose preamble mentions fuel
    names too, but never with numbers). The official price is the row's maximum
    (see module docstring). Fuels missing from a notice are simply absent.
    """
    prices: dict[str, float] = {}
    for line in text.splitlines():
        nums = [float(n.replace(",", "")) for n in _NUMBER.findall(line)]
        if len(nums) < 3:
            continue
        for name, pat in FUELS:
            if name not in prices and pat.search(line):
                prices[name] = max(nums)
    return prices


def _hash_pdf(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stamp_prices_seen(state: dict) -> None:
    state[LAST_PRICES_SEEN_AT_KEY] = _dt.datetime.now(_dt.timezone.utc).isoformat()


def _evaluate(prev: dict, cur: dict, push_pct: float) -> tuple[str, str, changes.Change | None]:
    """Pure. Returns (action, body, biggest_change) for a new weekly notice.

    One line per tracked fuel ("Gasolina Premium: RD$339.80 (-4.70, -1.4%)").
    ``action`` is "push" when any fuel moved by ``push_pct`` percent or more,
    else "digest" (small moves and flat weeks both digest, so a quiet week is
    never ambiguous with a broken scrape). ``biggest_change`` is the largest
    mover's ``changes.Change`` (None on an all-flat week) for the event log.
    """
    lines: list[str] = []
    biggest: changes.Change | None = None
    biggest_pct = 0.0
    for name, _ in FUELS:
        if name not in cur:
            continue
        price = cur[name]
        ch = changes.diff(prev[name], price, label=name,
                          fmt=lambda p: f"RD${p:.2f}") if name in prev else None
        if ch:
            delta = ch.metadata.get("abs_delta", 0.0)
            pct = ch.metadata.get("pct_delta")
            move = f"{delta:+.2f}" + (f", {pct:+.1f}%" if pct is not None else "")
            lines.append(f"{name}: RD${price:.2f} ({move})")
            if pct is not None and abs(pct) > biggest_pct:
                biggest_pct, biggest = abs(pct), ch
        else:
            lines.append(f"{name}: RD${price:.2f} (sin cambio)")
    action = "push" if biggest_pct >= push_pct else "digest"
    return action, "\n".join(lines), biggest


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state  # weekly notices; the daily run is plenty

    cfg = config.section("fuel")
    page = cfg.get("page", DEFAULT_PAGE)
    push_pct = float(cfg.get("push_pct", DEFAULT_PUSH_PCT))

    try:
        resp = requests.get(page, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        pdf_url = _find_pdf(resp.text)
    except Exception as exc:  # noqa: BLE001 - a fetch failure is non-fatal
        log.error("fuel: listing fetch failed: %s", exc)
        health.source_failed(state, TOPIC, f"listing fetch failed: {exc}")
        return state

    if not pdf_url:
        log.warning("fuel: no weekly notice PDF found on %s", page)
        health.source_failed(state, TOPIC, "no weekly notice PDF found (layout change?)")
        return state
    try:
        pdf = requests.get(pdf_url, headers=HEADERS, timeout=60)
        pdf.raise_for_status()
        pdf_hash = _hash_pdf(pdf.content)
    except Exception as exc:  # noqa: BLE001
        log.error("fuel: notice PDF fetch failed: %s", exc)
        health.source_failed(state, TOPIC, f"notice PDF fetch failed: {exc}")
        return state

    known_url = pdf_url == state.get(LAST_PDF_KEY)
    if known_url and pdf_hash == state.get(LAST_PDF_HASH_KEY):
        log.info("fuel: no new notice (still %s, hash match)",
                 pdf_url.rsplit("/", 1)[-1])
        # The listing and PDF are alive and match the content we already parsed.
        # Count the cached price rows as current source data for watchdog health.
        cached_count = len(state.get(STATE_KEY) or {}) or 1
        _stamp_prices_seen(state)
        health.source_ok(state, TOPIC, data_count=cached_count, message="no new notice")
        return state

    try:
        from pypdf import PdfReader
    except ImportError:
        log.error("fuel: pypdf is not installed; skipping fuel prices")
        health.source_failed(state, TOPIC, "pypdf is not installed")
        return state

    try:
        reader = PdfReader(io.BytesIO(pdf.content))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:  # noqa: BLE001
        log.error("fuel: notice PDF fetch/parse failed: %s", exc)
        health.source_failed(state, TOPIC, f"notice PDF fetch/parse failed: {exc}")
        return state

    prices = _parse_prices(text)
    if not prices:
        # Don't record the URL: leaving the dedup key untouched retries this
        # notice tomorrow instead of silently skipping a week.
        log.error("fuel: no prices parsed from %s (layout change?)", pdf_url)
        health.source_failed(state, TOPIC, "no prices parsed from notice PDF (layout change?)")
        return state
    health.source_ok(state, TOPIC, data_count=len(prices))
    _stamp_prices_seen(state)

    prev = state.get(STATE_KEY) or {}
    if not prev:
        log.info("fuel: first run, seeded %d prices silently", len(prices))
    elif known_url and prices == prev:
        log.info("fuel: same URL parsed successfully; prices unchanged")
    else:
        action, body, biggest = _evaluate(prev, prices, push_pct)
        big = ("high" if action == "push" else "low")
        state = events.emit(
            state,
            title="Combustibles: precios de la semana",
            body=body,
            change=biggest,
            topic="fuel",
            severity=big,
            source="MICM",
            click_url=pdf_url,
            tags="fuelpump",
            metadata={"preserve_detail": True},
            legacy_priority="high" if action == "push" else None,
            legacy_action=action,
        )
        log.info("fuel: new weekly notice -> %s (%s)", action, pdf_url.rsplit("/", 1)[-1])

    state[STATE_KEY] = prices
    state[LAST_PDF_KEY] = pdf_url
    state[LAST_PDF_HASH_KEY] = pdf_hash
    return state
