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
import io
import logging
import os
import re
from typing import Optional

import requests
from pypdf import PdfReader

from .. import changes, config, events, visa_math

log = logging.getLogger(__name__)

# --- SOURCE: the official PDF, not the HTML page -----------------------------
#
# August 2026 audit. This topic read the HTML bulletin at
# travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html until
# that host moved behind Cloudflare bot management. Every HTTP client now gets
# HTTP 403 with `cf-mitigated: challenge` and a "Just a moment… Enable
# JavaScript and cookies to continue" interstitial. Verified it is the
# challenge and not a User-Agent or IP problem: a real browser UA, no UA, and
# requests' default UA all 403 identically, while a real Chrome loads the page
# after ~5s of JS challenge. The topic had been failing since 2026-07-14 and,
# because of that, silently missed the August bulletin — in which the F4 Final
# Action date advanced from 01JAN09 to 01SEP09.
#
# Getting past that challenge would need a headless browser, a TLS-fingerprint
# spoofer, or a challenge-solving service. All three are bot-detection evasion,
# all three break every time Cloudflare updates, and none belong in a workflow
# that must run unattended 96x/day.
#
# The fix needs none of them: the State Department publishes the SAME bulletin
# as a PDF under /content/dam/ (their asset path), it is the canonical
# published artifact rather than a mirror, and it is served plainly — HTTP 200,
# no challenge, verified across the Jan/May/Jun/Jul/Aug 2026 editions. So the
# topic now reads the PDF and parses it with pypdf, already a dependency here
# for the fuel and outages notices.
#
# Notification links still point at the HTML page: a human tapping the embed
# opens it in a real browser, which clears the challenge in a few seconds. Only
# the unattended fetch needed to change.
#
# If /content/dam/ is ever put behind the same challenge, do NOT reach for a
# bot-detection workaround — see the retirement analysis in the module tests
# and docs/design/06-topic-audit.md.
PDF_URL = "https://travel.state.gov/content/dam/visas/Bulletins/visabulletin_{month}{year}.pdf"
# Kept as the human-facing link (and the late-bulletin alert target) only.
INDEX_URL = (
    "https://travel.state.gov/content/travel/en/legal/visa-law0/"
    "visa-bulletin.html"
)
HTML_BULLETIN_URL = (
    "https://travel.state.gov/content/travel/en/legal/visa-law0/"
    "visa-bulletin/{year}/visa-bulletin-for-{month_lower}-{year}.html"
)
USER_AGENT = "notify-watcher/1.0 (+https://github.com/) personal-use"
# How many months ahead of today to probe for a newly published edition. The
# bulletin for month M appears around the second week of M-1, so +2 is already
# generous; the loop walks DOWN from there and takes the first PDF that exists.
_LOOKAHEAD_MONTHS = 2
_LOOKBACK_MONTHS = 3

# Each tracked cell: (state key, human label, section key from _f4_all_other).
# Unchanged in meaning from the HTML era — only the way a section is located
# moved, from "the <table> after this heading" to "the F4 row nearest below
# this heading in the flattened PDF text".
CHECKS = [
    ("visa_f4_final_action", "Final Action Dates", "final_action"),
    ("visa_f4_dates_for_filing", "Dates for Filing", "dates_for_filing"),
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

# Drives both URL shapes ("visabulletin_August2026.pdf" and the human page's
# "visa-bulletin-for-august-2026.html") and the edition label.
_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def _pdf_url(year: int, month_num: int) -> str:
    return PDF_URL.format(month=_MONTHS[month_num - 1].capitalize(), year=year)


def _html_url(year: int, month_num: int) -> str:
    """The human-readable page for this edition — used only as a click target."""
    return HTML_BULLETIN_URL.format(year=year,
                                    month_lower=_MONTHS[month_num - 1])


def _shift_month(year: int, month_num: int, delta: int) -> tuple[int, int]:
    idx = (year * 12 + (month_num - 1)) + delta
    return idx // 12, idx % 12 + 1


def _pdf_exists(url: str) -> bool:
    """HEAD probe. Any non-200, or a non-PDF content type, means 'not published'."""
    try:
        resp = requests.head(url, headers={"User-Agent": USER_AGENT},
                             timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        # A transport error is NOT evidence of absence; let the caller keep
        # walking back rather than treat a blip as "no bulletin".
        log.warning("visa_bulletin: HEAD %s failed: %s", url, exc)
        return False
    if resp.status_code != 200:
        return False
    return "pdf" in (resp.headers.get("content-type") or "").lower()


def _find_current_bulletin(today: _dt.date) -> tuple[str, str]:
    """Newest published edition as ``(edition "YYYY-MM", pdf_url)``.

    Replaces the old "scrape every link off the index page" discovery, which is
    no longer reachable (see the SOURCE note at the top). The PDF filenames are
    fully derivable from the date — ``visabulletin_August2026.pdf`` — so this
    walks DOWN from a couple of months ahead and takes the first one that
    actually exists. Verified stable across every 2026 edition.

    Walking down rather than guessing means a bulletin published early is found
    immediately, and a bulletin published late simply resolves to the previous
    edition (which ``_late_bulletin_check`` then reports on, unchanged).
    """
    year, month = today.year, today.month
    for delta in range(_LOOKAHEAD_MONTHS, -_LOOKBACK_MONTHS - 1, -1):
        y, m = _shift_month(year, month, delta)
        url = _pdf_url(y, m)
        if _pdf_exists(url):
            return f"{y}-{m:02d}", url
    raise RuntimeError(
        f"No visa bulletin PDF found for any edition within "
        f"{_LOOKBACK_MONTHS} months of {today:%Y-%m}; the publisher may have "
        f"changed the /content/dam/ URL scheme"
    )


def _fetch_pdf_text(url: str) -> str:
    """Download one bulletin PDF and flatten it to text."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    reader = PdfReader(io.BytesIO(resp.content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


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


# One F4 table row as pypdf flattens it:
#   "F4 01SEP09 01SEP09 01NOV06 08APR01 01AUG07"
# Columns are All-Other, China, India, Mexico, Philippines. Cells are DDMONYY,
# or the single letters C (current) / U (unavailable), which the change
# formatter already handles.
_F4_ROW = re.compile(r"^[ \t]*F4[ \t]+(\S+)", re.MULTILINE)
_SECTION_ANCHORS = {
    "final_action": "FINAL ACTION DATES",
    "dates_for_filing": "DATES FOR FILING",
}


def _f4_all_other(pdf_text: str) -> dict[str, str]:
    """Map ``{"final_action": "01SEP09", "dates_for_filing": "22JUN10"}``.

    The employment-based tables use E1–E5, so the only two ``F4`` rows in the
    document are the two family-sponsored ones this topic tracks. Rather than
    trust that they always appear in the same order, each row is attributed to
    whichever section heading most recently precedes it.

    Deliberately strict: if the document does not yield exactly one row per
    section, this RAISES instead of returning a best guess. These are the dates
    someone plans an immigration case around — a wrong number reported
    confidently is far worse than a loud failure, and the watchdog now reports
    the failure within one run.
    """
    upper = pdf_text.upper()
    found: dict[str, str] = {}
    rows = list(_F4_ROW.finditer(pdf_text))
    for match in rows:
        # Nearest preceding heading wins.
        best_key, best_pos = None, -1
        for key, phrase in _SECTION_ANCHORS.items():
            pos = upper.rfind(phrase, 0, match.start())
            if pos > best_pos:
                best_key, best_pos = key, pos
        if best_key is None or best_pos < 0:
            continue
        if best_key in found:
            raise RuntimeError(
                f"two F4 rows both resolved to the {best_key!r} section; "
                f"the bulletin layout has changed"
            )
        found[best_key] = _norm(match.group(1))

    missing = set(_SECTION_ANCHORS) - set(found)
    if missing:
        log.error("visa_bulletin: F4 rows found=%d, resolved=%r, missing=%r",
                  len(rows), found, sorted(missing))
        raise RuntimeError(
            f"could not read the F4 'All Other' cell for {sorted(missing)} "
            f"from the bulletin PDF ({len(rows)} F4 row(s) seen)"
        )
    return found


def run(state: dict, today: Optional[_dt.date] = None) -> dict:
    # Edition discovery is now derived from the calendar and confirmed with a
    # HEAD probe, instead of scraping links off the (Cloudflare-walled) index.
    edition, pdf_url = _find_current_bulletin(today or _dt.date.today())
    year, month_num = int(edition[:4]), int(edition[5:])
    # The click target stays the HTML page: a person tapping the embed opens a
    # real browser, which clears the challenge on its own in a few seconds.
    bulletin_url = _html_url(year, month_num)
    log.info("current bulletin: %s (edition %s, pdf %s)",
             bulletin_url, edition, pdf_url)

    pdf_text = _fetch_pdf_text(pdf_url)
    cells = _f4_all_other(pdf_text)

    # A brand-new edition folds both cells' outcomes into ONE combined push
    # below instead of per-cell alerts; the per-cell alerts keep firing
    # standalone for a date that moves *within* the same edition (rare
    # mid-month correction).
    prev_edition = state.get(EDITION_KEY)
    new_edition = bool(edition and prev_edition and edition != prev_edition)

    summaries: list[str] = []  # per-cell lines for the combined push
    dates_moved = False
    for state_key, label, section in CHECKS:
        try:
            current = cells[section]
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
