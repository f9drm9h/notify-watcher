"""Topic: U.S. State Dept Visa Bulletin, F4 Dates for Filing, All Other column.

Logic:
  1. Hit the visa-bulletin index page on travel.state.gov.
  2. Find the newest monthly bulletin link there.
  3. Fetch that monthly bulletin and locate section B,
     "Dates for Filing Family-Sponsored Visa Applications".
  4. Read the F4 row, "All Chargeability Areas Except Those Listed" column.
  5. Compare to state["visa_f4_dates_for_filing"]. If different, push and update.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .. import ntfy

log = logging.getLogger(__name__)

INDEX_URL = (
    "https://travel.state.gov/content/travel/en/legal/visa-law0/"
    "visa-bulletin.html"
)
USER_AGENT = "notify-watcher/1.0 (+https://github.com/) personal-use"
STATE_KEY = "visa_f4_dates_for_filing"

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


def _parse_f4_all_other(bulletin_html: str) -> str:
    """Find the F4 / All Other cell in the Family-Sponsored Dates for Filing table.

    Strategy: the heading "DATES FOR FILING FAMILY-SPONSORED VISA
    APPLICATIONS" appears in a <p> above the target table. We anchor on
    that heading and grab the next <table>. The Employment table follows
    a different heading, so we never confuse them.
    """
    soup = BeautifulSoup(bulletin_html, "html.parser")

    heading_node = None
    for text_node in soup.find_all(string=True):
        compact = _norm(text_node).upper()
        if "DATES FOR FILING" in compact and "FAMILY-SPONSORED" in compact:
            heading_node = text_node
            break
    if heading_node is None:
        raise RuntimeError(
            "Heading 'DATES FOR FILING ... FAMILY-SPONSORED' not found on page"
        )

    table = heading_node.find_next("table")
    if table is None:
        raise RuntimeError("Found heading but no <table> followed it")

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
    raise RuntimeError("F4 row not found in Family Dates for Filing table")


def run(state: dict) -> dict:
    index_html = _fetch(INDEX_URL)
    bulletin_url = _find_current_bulletin_url(index_html)
    log.info("current bulletin: %s", bulletin_url)

    bulletin_html = _fetch(bulletin_url)
    current = _parse_f4_all_other(bulletin_html)
    log.info("F4 All-Other dates-for-filing: %s", current)

    previous = state.get(STATE_KEY)
    if previous == current:
        log.info("unchanged, no push")
        return state

    title = "F4 Dates for Filing changed"
    if previous is None:
        body = f"First seen F4 (All Other) Dates for Filing: {current}"
    else:
        body = f"F4 (All Other) Dates for Filing changed: {previous} -> {current}"
    ntfy.push(title=title, message=body, click_url=bulletin_url, tags="passport_control")

    state[STATE_KEY] = current
    return state
