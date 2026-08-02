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

ROWS ARE FOUND BY LABEL, NOT BY LINE (August 2026 reliability audit). The
original parser scanned line by line and took the maximum of any line naming a
fuel. That silently produced garbage for the whole life of this topic: pypdf
extracts the real notice as ONE unbroken line per page — the entire price table
arrives with no separators at all ("…Gasolina Premium191.0471.8530.57…") — so
every fuel pattern matched that same line and every fuel got the maximum of the
WHOLE TABLE. That maximum is the "Cilindros de 100 Libras" LPG-cylinder price,
which is why state.json carried six identical RD$3,380.07 prices instead of six
different real ones. Nothing caught it: the fetch succeeded, six rows "parsed",
health said ok, and because the bogus value was identical for every fuel the
week-over-week diff was always "sin cambio" — a topic incapable of ever
alerting, reporting itself healthy every single day.

So a row is now the text BETWEEN one row label and the next, wherever the line
breaks happen to fall. ``_ROW_LABEL`` deliberately includes rows we do not
track (the EGP variants, Avtur, Fuel Oil, the Cilindros block) precisely
because their only job is to terminate the row before them.

The parse is then VALIDATED before it is trusted (``_validate``), because the
failure above was not a crash — it was a confident wrong answer, and no amount
of fetch-level health checking can see one of those. Prices outside a plausible
RD$/gal band are dropped, and an all-identical result is rejected outright as
the signature of a collapsed table. A rejected parse reports
``health.source_failed``, so a future layout change surfaces through the
watchdog as an outage instead of quietly digesting nonsense for a month.
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

# Every label that starts a row of the price table, tracked or not. This is the
# row DELIMITER set: a fuel's numbers are the text from the end of its own label
# to the start of the next label, so untracked rows must appear here or the row
# before them would swallow their numbers (Kerosene would run on into Fuel Oil,
# GLP into the Cilindros cylinder prices — the exact value that poisoned this
# topic). Order is irrelevant; the scan uses positions.
_ROW_LABEL = re.compile(
    r"Gasolina\s+Premium"
    r"|Gasolina\s+Regular"
    r"|Gasoil\s+Regular"          # also terminates at its own EGP-C/EGP-T rows
    r"|Gasoil\s+[ÓO]ptimo"
    r"|Avtur"
    r"|Kerosene"
    r"|Fuel\s+Oil"
    r"|Gas\s+Licuado\s+de\s+Petr.leo"
    r"|Cilindros\s+de",
    re.I,
)

# Plausible consumer price band in RD$/gal. Real notices sit around RD$130-350;
# this is deliberately wide so ordinary inflation never trips it, and narrow
# enough to reject the cylinder prices (RD$1,690 / RD$3,380) and the stray
# totals that a collapsed table hands back.
_MIN_PRICE = 20.0
_MAX_PRICE = 1000.0


def _find_pdf(html: str) -> str | None:
    """Newest weekly-notice PDF URL on the listing page (the page lists newest
    first), or None when the markup changed and no notice link is found."""
    m = _PDF_LINK.search(html)
    return _html.unescape(m.group(1)) if m else None


# How far past a row label to look when deciding WHICH fuel the row is. Long
# enough to see the " EGP-C"/" EGP-T" qualifier that distinguishes the
# power-generation rows from the consumer ones, short enough never to reach the
# next row.
_QUALIFIER_WINDOW = 20


def _rows(text: str) -> list[tuple[str, str]]:
    """Pure: (label, body) for every price-table row, split by label position.

    A row runs from the end of its own label to the start of the next label
    anywhere in the document, so this works whether pypdf emits one line per row
    (older notices, and the test fixture) or collapses the entire table onto a
    single line (what the live notice actually does).

    ``label`` carries the matched label plus a short lookahead into the row, so
    a fuel pattern's negative lookahead ("Gasoil Regular" but not "Gasoil
    Regular EGP-C") can still see the qualifier that follows it. Without that
    window the EGP rows would be indistinguishable from the consumer row and
    only their document order would keep them apart.
    """
    marks = list(_ROW_LABEL.finditer(text))
    rows: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        rows.append((text[m.start(): m.end() + _QUALIFIER_WINDOW], text[m.end(): end]))
    return rows


def _validate(prices: dict[str, float]) -> tuple[dict[str, float], str]:
    """Pure: drop implausible prices and reject a collapsed parse.

    Returns ``(clean_prices, reason)``; a non-empty ``reason`` means the parse
    must be treated as a SOURCE FAILURE rather than trusted, and is the message
    the watchdog will show. Two rules, both aimed at confident wrong answers
    rather than crashes:

      * out-of-band values are dropped individually — one weird row should not
        cost the other five;
      * three or more fuels sharing one identical price is not a real notice.
        Premium and Regular can coincide; six fuels never do, and that is the
        exact signature of a table that collapsed into a single row.
    """
    clean = {n: p for n, p in prices.items() if _MIN_PRICE <= p <= _MAX_PRICE}
    dropped = sorted(set(prices) - set(clean))
    if not clean:
        return {}, (f"every parsed price was outside RD${_MIN_PRICE:g}-"
                    f"{_MAX_PRICE:g}/gal (layout change?)")
    if len(clean) >= 3 and len(set(clean.values())) == 1:
        value = next(iter(clean.values()))
        return {}, (f"all {len(clean)} fuels parsed to the identical price "
                    f"RD${value:.2f} (collapsed table?)")
    if dropped:
        log.warning("fuel: ignoring implausible price(s) for %s", ", ".join(dropped))
    return clean, ""


def _parse_prices(text: str) -> dict[str, float]:
    """Pure: extract {fuel: official RD$/gal} from the notice's text.

    Walks the table's row spans (see :func:`_rows`); the first row whose label
    matches a tracked fuel and that carries at least three numeric cells is that
    fuel's row, and the official price is the row's maximum (see the module
    docstring). The three-cell floor keeps the prose preamble — which names
    fuels but never tabulates them — from being read as a row. Fuels missing
    from a notice are simply absent.

    Returns the RAW parse; callers must run it through :func:`_validate` before
    trusting it.
    """
    prices: dict[str, float] = {}
    for label, body in _rows(text):
        nums = [float(n.replace(",", "")) for n in _NUMBER.findall(body)]
        if len(nums) < 3:
            continue
        for name, pat in FUELS:
            if name not in prices and pat.search(label):
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

    # Is the price baseline we are about to diff against trustworthy? A baseline
    # banked by an earlier, broken parser is poison: diffing real prices against
    # RD$3,380.07 would push a fabricated -90% crash on the very run that fixes
    # the parser. Re-running the CURRENT validator over stored state is what
    # makes that self-healing — any baseline this build would refuse to accept
    # today is one it refuses to diff against today, whoever wrote it.
    stored = state.get(STATE_KEY) or {}
    _, stored_reason = _validate(stored) if stored else ({}, "")
    if stored_reason:
        log.warning("fuel: discarding unusable stored prices (%s); "
                    "re-seeding silently from this notice", stored_reason)
        stored = {}

    known_url = pdf_url == state.get(LAST_PDF_KEY)
    # Skip the cached-notice shortcut while the baseline is poisoned: the hash
    # still matches, so without this the bad prices would sit there until MICM
    # happens to publish a new notice.
    if known_url and pdf_hash == state.get(LAST_PDF_HASH_KEY) and not stored_reason:
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

    # A parse that SUCCEEDED but produced nonsense is the failure this topic
    # actually suffered, and it is invisible to every fetch-level check. Treat a
    # rejected parse exactly like an unparseable one: report the source as
    # failed so the watchdog raises it, and leave the dedup keys untouched so
    # the notice is retried rather than banked.
    prices, reason = _validate(prices)
    if reason:
        log.error("fuel: rejected implausible parse from %s: %s", pdf_url, reason)
        health.source_failed(state, TOPIC, reason)
        return state
    health.source_ok(state, TOPIC, data_count=len(prices))
    _stamp_prices_seen(state)

    # `stored`, not state[STATE_KEY]: a baseline the validator rejected was
    # emptied above, which turns this run into a silent re-seed.
    prev = stored
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
