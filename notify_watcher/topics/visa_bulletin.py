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

Wait estimator: each Final Action move is also recorded in state["f4_history"]
(cutoff + bulletin month, capped at 24 entries) and the alert body gains a pace
line — "Advanced ~14 d/bulletin over 6 bulletins — ~4.2 yr to your priority
date" (visa_math.py does the math; monitors.json -> visa_bulletin.
f4_priority_date, optional, enables the ETA clause). On the first daily run of
each quarter a low-priority check-in compares the recent pace against the full
history, so the estimate stays visible even while the cutoff crawls.

Edition tracking: the newest bulletin's URL month ("…-for-august-2026.html" ->
"2026-08") is stored in state["visa_bulletin_current"], independent of the F4
values. A new edition always notifies — one combined push carrying the date
moves (with pace line) when they happened, or "held steady" when they didn't —
so a bulletin that leaves F4 parked is no longer silent. The per-cell alerts
below still fire standalone for the rare mid-month correction (a date that
moves within the same edition). The State Dept publishes each month's bulletin
around the second week of the PRIOR month, so if no new edition has shown up
after the 15th, one "bulletin is late" alert fires (deduped per expected
edition in state["visa_bulletin_late_alerted"]; it re-arms when the bulletin
finally lands or the expectation rolls to the next month).
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .. import changes, config, events, visa_math

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

# Only the Final Action cutoff feeds the wait estimator's history: it is the
# date that actually controls visa issuance, so a Dates-for-Filing move says
# nothing about how fast the F4 queue itself is draining.
HISTORY_SOURCE_KEY = "visa_f4_final_action"

# Edition tracking: which bulletin month is currently live ("2026-08", from
# the bulletin URL), and the late-alert dedup — the *expected* edition the
# "bulletin is late" push already fired for, so it fires at most once and
# naturally re-arms when the bulletin lands or the month rolls over.
EDITION_KEY = "visa_bulletin_current"
LATE_KEY = "visa_bulletin_late_alerted"
# Next month's bulletin normally publishes around the 2nd week of this month;
# past this day of month with no new edition, it counts as late.
LATE_AFTER_DAY = 15

# Quarterly check-in: dedup key, the months it fires in, and how far back the
# "recent" pace window reaches (7 cutoff entries span >= 6 bulletin months).
QUARTER_KEY = "f4_quarterly_last"
_QUARTER_MONTHS = (1, 4, 7, 10)
_RECENT_ENTRIES = 7

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


def _bulletin_month(url: str) -> Optional[str]:
    """``…/visa-bulletin-for-july-2026.html`` -> ``"2026-07"`` (None if unmatched)."""
    m = _BULLETIN_HREF.search(url)
    if not m:
        return None
    return f"{int(m.group(2))}-{_MONTHS.index(m.group(1).lower()) + 1:02d}"


def _edition_label(edition: str) -> str:
    """``"2026-08"`` -> ``"August 2026"`` (for titles and bodies)."""
    year, month = edition.split("-")
    return f"{_MONTHS[int(month) - 1].capitalize()} {year}"


def _expected_edition(today: _dt.date) -> str:
    """The edition that should be live by mid-``today.month``: next month's.

    The State Dept publishes each monthly bulletin around the second week of
    the PRIOR month (August's appears mid-July), so past LATE_AFTER_DAY the
    newest index link should already point at the following month.
    """
    year, month = ((today.year + 1, 1) if today.month == 12
                   else (today.year, today.month + 1))
    return f"{year}-{month:02d}"


def _late_bulletin_check(state: dict, edition: Optional[str],
                         today: Optional[_dt.date] = None) -> dict:
    """One alert when no new bulletin has appeared by end of day on the 15th.

    ``edition`` is the newest bulletin month currently live. Deduped on the
    *expected* edition (LATE_KEY), so subsequent runs stay silent; the alert
    re-arms only when the bulletin actually lands (edition >= expected makes
    the condition false and a fresh expectation starts next month).
    """
    if not edition:
        return state
    today = today or _dt.date.today()
    if today.day <= LATE_AFTER_DAY:
        return state
    expected = _expected_edition(today)
    if edition >= expected or state.get(LATE_KEY) == expected:
        return state
    state = events.emit(
        state,
        title="Visa bulletin is late",
        body=(f"No new visa bulletin as of {today:%b %d} — newest published "
              f"edition is still {_edition_label(edition)} (expected "
              f"{_edition_label(expected)} by mid-month)."),
        topic="visa_bulletin",
        severity="moderate",
        source="Visa Bulletin",
        click_url=INDEX_URL,
        tags="passport_control",
        legacy_action="push",
    )
    state[LATE_KEY] = expected
    return state


def _priority_date() -> str:
    """The user's I-130 priority date from monitors.json, "" when unset."""
    return str(config.section("visa_bulletin").get("f4_priority_date") or "").strip()


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


def run(state: dict, today: Optional[_dt.date] = None) -> dict:
    index_html = _fetch(INDEX_URL)
    bulletin_url = _find_current_bulletin_url(index_html)
    log.info("current bulletin: %s", bulletin_url)

    bulletin_html = _fetch(bulletin_url)
    soup = BeautifulSoup(bulletin_html, "html.parser")

    # Edition identity comes from the bulletin URL ("…-for-august-2026.html"
    # -> "2026-08"). A brand-new edition folds both cells' outcomes into ONE
    # combined push below instead of per-cell alerts; the per-cell alerts keep
    # firing standalone for a date that moves *within* the same edition (rare
    # mid-month correction). An unparseable URL (edition None) falls back to
    # the per-cell behavior unchanged.
    edition = _bulletin_month(bulletin_url)
    prev_edition = state.get(EDITION_KEY)
    new_edition = bool(edition and prev_edition and edition != prev_edition)

    summaries: list[str] = []  # per-cell lines for the combined push
    dates_moved = False
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
                if new_edition:
                    summaries.append(f"F4 (All Other) {label} held steady at {current}")
                continue

            # Record the Final Action move in the wait-estimator history. The
            # first sighting seeds silently (one entry can't yield a pace, so
            # no estimator line is added to the first-seen push below).
            pace_line = ""
            if state_key == HISTORY_SOURCE_KEY:
                if edition:
                    state[visa_math.HISTORY_KEY] = visa_math.record_cutoff(
                        state.get(visa_math.HISTORY_KEY) or [], current, edition)
                if previous is not None:
                    pace_line = visa_math.pace_sentence(visa_math.estimate_wait(
                        state.get(visa_math.HISTORY_KEY) or [], _priority_date()))

            ch = None
            if previous is None:
                body = f"First seen F4 (All Other) {label}: {current}"
            else:
                # Report how many days the cutoff date advanced/retreated, parsing the
                # bulletin's DDMONYY cells; degrades to a string diff for non-date
                # cells like "C" (current) or "U" (unavailable).
                ch = changes.diff(previous, current, kind="date",
                                  label=f"F4 (All Other) {label}")
                body = f"{ch.summary}\n{pace_line}" if pace_line else ch.summary

            if new_edition and previous is not None:
                # Fold this move into the combined new-bulletin push below.
                summaries.append(body)
                dates_moved = True
            else:
                state = events.emit(
                    state,
                    title=f"F4 {label} changed",
                    body=body,
                    change=ch,
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

    if edition and edition != prev_edition:
        if prev_edition is None:
            # First sighting seeds silently: the two first-seen cell pushes
            # above already announce the tracker starting up.
            log.info("seeding bulletin edition %s", edition)
        else:
            state = events.emit(
                state,
                title=f"New visa bulletin: {_edition_label(edition)}",
                body="\n".join(summaries)
                     or "F4 cells could not be read from this bulletin (see log).",
                topic="visa_bulletin",
                severity="critical" if dates_moved else "moderate",
                source="Visa Bulletin",
                click_url=bulletin_url,
                tags="passport_control",
                legacy_action="push",
            )
        state[EDITION_KEY] = edition

    try:
        state = _late_bulletin_check(state, state.get(EDITION_KEY), today)
    except Exception as exc:  # noqa: BLE001 - the late check never blocks the alerts
        log.error("late-bulletin check failed: %s", exc)

    try:
        state = _quarterly_summary(state)
    except Exception as exc:  # noqa: BLE001 - the check-in never blocks the alerts
        log.error("F4 quarterly summary failed: %s", exc)
    return state


def _quarterly_summary(state: dict, today: Optional[_dt.date] = None) -> dict:
    """Once per quarter, digest a pace check-in: recent vs. full-history.

    The cutoff alert above only speaks when the bulletin moves, so months of
    stall leave the estimate invisible. On the first daily run of each quarter
    month (Jan/Apr/Jul/Oct — same NOTIFY_DAILY ride-along as recap/fx) this
    sends one low-severity event comparing the recent window's pace against
    the full ~24-bulletin history. Deduped per quarter (``f4_quarterly_last``),
    so a failed send naturally retries on the next daily run; with fewer than
    two recorded cutoffs it stays silent without consuming the quarter.
    """
    if not os.environ.get("NOTIFY_DAILY"):
        return state
    today = today or _dt.date.today()
    if today.month not in _QUARTER_MONTHS:
        return state
    quarter = f"{today.year}-Q{(today.month - 1) // 3 + 1}"
    if state.get(QUARTER_KEY) == quarter:
        return state

    history = state.get(visa_math.HISTORY_KEY) or []
    pd = _priority_date()
    recent = visa_math.estimate_wait(history[-_RECENT_ENTRIES:], pd)
    full = visa_math.estimate_wait(history, pd)
    if recent is None or full is None:
        log.info("F4 quarterly: not enough cutoff history yet; skipping")
        return state

    body = (f"Recently: {visa_math.pace_sentence(recent)}\n"
            f"Full history: {visa_math.pace_sentence(full)}")
    state = events.emit(
        state,
        title="F4 wait estimate — quarterly check-in",
        body=body,
        topic="visa_bulletin",
        severity="low",
        source="Visa Bulletin",
        tags="passport_control",
        legacy_action="digest",
        score=45,
    )
    log.info("F4 quarterly: sent %s check-in", quarter)
    state[QUARTER_KEY] = quarter
    return state
