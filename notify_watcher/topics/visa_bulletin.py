"""Topic: U.S. State Dept Visa Bulletin, F4 row, "All Other" column.

Logic:
  1. Hit the visa-bulletin index page on travel.state.gov.
  2. Find the newest monthly bulletin link there.
  3. Fetch that monthly bulletin and read the F4 "All Chargeability Areas
     Except Those Listed" cell from BOTH family-sponsored tables:
       - section A, "Final Action Dates"
       - section B, "Dates for Filing"
  4. Compare each to its stored value. If either moved, push and update.

The two checks are independent: a parse failure or unchanged value in one
never blocks a real alert from the other.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .. import events

log = logging.getLogger(__name__)

INDEX_URL = (
    "https://travel.state.gov/content/travel/en/legal/visa-law0/"
    "visa-bulletin.html"
)
USER_AGENT = "notify-watcher/1.0 (+https://github.com/) personal-use"

# Each tracked cell: (state key, human label, heading phrases that must all
# appear in the <p> above the table we want). "FINAL ACTION DATES" vs
# "DATES FOR FILING" disambiguate the two family-sponsored tables.
CHECKS = [
    (
        "visa_f4_final_action",
        "Final Action Dates",
        ("FINAL ACTION DATES", "FAMILY-SPONSORED"),
    ),
    (
        "visa_f4_dates_for_filing",
        "Dates for Filing",
        ("DATES FOR FILING", "FAMILY-SPONSORED"),
    ),
]

# Matches month names inside a bulletin URL so we can pick the newest one.
_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
_BULLETIN_HREF = re.compile(
    r"visa-bulletin-for-(" + "|".join(_MONTHS) + r")-(\d{4})\.html",
    re.IGNORECASE,
)


def _fetch(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def _find_current_bulletin_url(index_html: str) -> str:
    soup = BeautifulSoup(index_html, "html.parser")
    best: Optional[tuple[int, int, str]] = None
    for a in soup.find_all("a", href=True):
        m = _BULLETIN_HREF.search(a["href"])
        if not m:
            continue
        month_num = _MONTHS.index(m.group(1).lower()) + 1
        year = int(m.group(2))
        href = a["href"]
        if href.startswith("/"):
            href = "https://travel.state.gov" + href
        key = (year, month_num)
        if best is None or key > (best[0], best[1]):
            best = (year, month_num, href)
    if best is None:
        raise RuntimeError("Could not find any monthly bulletin link on index page")
    return best[2]


def _norm(s: str) -> str:
    # Replace non-breaking spaces with regular spaces and collapse runs.
    return " ".join(s.replace("\xa0", " ").split())


def _table_after_heading(soup: BeautifulSoup, phrases: tuple[str, ...]):
    """Return the first <table> following a heading whose text contains all
    `phrases` (case-insensitive), or None if no such heading/table exists."""
    for text_node in soup.find_all(string=True):
        compact = _norm(text_node).upper()
        if all(p in compact for p in phrases):
            table = text_node.find_next("table")
            if table is not None:
                return table
    return None


def _f4_all_other(table) -> str:
    """Read the F4 row, second column ("All Other"), from a bulletin table."""
    first_cells: list[str] = []
    for tr in table.find_all("tr"):
        cells = [_norm(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        first_cells.append(cells[0])
        tokens = cells[0].split()
        if tokens and tokens[0].upper() == "F4" and len(cells) >= 2:
            return cells[1]

    log.error("F4 row not found in matched table; leftmost cells: %r", first_cells)
    raise RuntimeError("F4 row not found in table")


def run(state: dict) -> dict:
    index_html = _fetch(INDEX_URL)
    bulletin_url = _find_current_bulletin_url(index_html)
    log.info("current bulletin: %s", bulletin_url)

    bulletin_html = _fetch(bulletin_url)
    soup = BeautifulSoup(bulletin_html, "html.parser")

    for state_key, label, phrases in CHECKS:
        try:
            table = _table_after_heading(soup, phrases)
            if table is None:
                raise RuntimeError(f"heading {phrases!r} / its table not found on page")
            current = _f4_all_other(table)
            log.info("F4 All-Other %s: %s", label, current)

            previous = state.get(state_key)
            if previous == current:
                log.info("%s unchanged, no push", label)
                continue

            if previous is None:
                body = f"First seen F4 (All Other) {label}: {current}"
            else:
                body = f"F4 (All Other) {label} changed: {previous} -> {current}"
            state = events.emit(
                state,
                title=f"F4 {label} changed",
                body=body,
                topic="visa_bulletin",
                severity="critical",
                source="Visa Bulletin",
                click_url=bulletin_url,
                tags="passport_control",
                legacy_action="push",
            )
            state[state_key] = current
        except Exception as exc:  # noqa: BLE001 - isolate each cell's check
            log.error("F4 %s check failed: %s", label, exc)

    return state
