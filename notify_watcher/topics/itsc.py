"""Topic: ITSC academic-calendar deadlines (itsc.edu.do).

The Instituto Técnico Superior Comunitario publishes each cuatrimestre's
calendar as a PDF linked from /calendario-academico/ (e.g. "Calendario
academico 2026-2 MAYO-AGOSTO"). The PDF is a table of PROCESO/ACTIVIDAD rows
with an INICIO date and usually a FIN date, which pypdf's layout mode renders
as one line per row: the activity, then the date(s), d/m/yyyy. Wrapped name
fragments and section noise have no date on their line and fall away.

Each run scrapes the page for the newest calendar PDFs (by the /uploads/yyyy/mm/
path, so a freshly posted term takes over automatically), parses every row, and
pushes a heads-up `lead_days` before each boundary — by default 7 days and
1 day. A two-date row is a period: both its start ("starts") and its end
("ends", the actual deadline) alert; a single-date row alerts once ("on").
Each (activity, boundary, lead) fires exactly once; the first run seeds
whatever is currently due silently.

Daily-only (NOTIFY_DAILY): academic dates do not change run-to-run.
"""
from __future__ import annotations

import datetime as _dt
import io
import logging
import os
import re
import unicodedata

import requests

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - requirements.txt installs it
    PdfReader = None

from .. import config, events, ids

log = logging.getLogger(__name__)

STATE_KEY = "itsc_sent_ids"
CAP = 400
PAGE_URL = "https://www.itsc.edu.do/calendario-academico/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
DEFAULT_LEAD_DAYS = [7, 1]
MAX_PDFS = 2  # current term + a just-posted next term around the boundary

_PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf[^"]*)"', re.IGNORECASE)
_UPLOAD_DATE_RE = re.compile(r"/uploads/(\d{4})/(\d{2})/")
# One calendar row: activity text, then 1-2 d/m/yyyy dates separated from it
# by the table gap (2+ spaces in layout mode).
_ROW_RE = re.compile(
    r"^\s*(.*?\S)\s{2,}(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}/\d{1,2}/\d{4}))?\s*$")


def _fold(text: str) -> str:
    """Pure: lowercase and strip accents, for stable keys/matching."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower().strip()


def _calendar_links(html: str, base: str = "https://www.itsc.edu.do") -> list[str]:
    """Pure: page HTML -> calendar-PDF URLs, newest upload first.

    A calendar link is any .pdf whose filename mentions both "calendario" and
    "academico" (accent-insensitive), which skips the unrelated manuals the
    page also links. Sorting by the WordPress /uploads/yyyy/mm/ path puts the
    most recently posted term first.
    """
    found: dict[str, tuple] = {}
    for url in _PDF_HREF_RE.findall(html):
        if not url.startswith("http"):
            url = base + url
        name = _fold(url.rsplit("/", 1)[-1])
        if "calendario" not in name or "academico" not in name:
            continue
        m = _UPLOAD_DATE_RE.search(url)
        stamp = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        found.setdefault(url, stamp)
    return sorted(found, key=lambda u: found[u], reverse=True)


def _parse_dmy(text: str) -> _dt.date | None:
    """Pure: '30/4/2026' -> date(2026, 4, 30); None when not a real date."""
    try:
        day, month, year = (int(part) for part in text.split("/"))
        return _dt.date(year, month, day)
    except (ValueError, AttributeError):
        return None


def _parse_rows(text: str) -> list[dict]:
    """Pure: layout-mode PDF text -> [{activity, start, end}] calendar rows.

    end is None for a single-date row. Lines without a d/m/yyyy date (titles,
    the INICIO/FIN header, wrapped name fragments) are skipped, as is any row
    whose date doesn't parse.
    """
    rows: list[dict] = []
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        start = _parse_dmy(m.group(2))
        if start is None:
            continue
        end = _parse_dmy(m.group(3)) if m.group(3) else None
        rows.append({"activity": " ".join(m.group(1).split()),
                     "start": start, "end": end})
    return rows


def _boundaries(row: dict) -> list[tuple[str, _dt.date]]:
    """Pure: the dated boundaries of one row that deserve their own alert.

    A period alerts at both ends — its start (registration opens, exams begin)
    and its end (the actual deadline). A one-day row alerts once.
    """
    if row["end"] is None or row["end"] == row["start"]:
        return [("on", row["start"])]
    return [("starts", row["start"]), ("ends", row["end"])]


def _due(rows: list[dict], today: _dt.date, lead_days: list[int],
         ) -> list[tuple[str, dict, str, _dt.date, int]]:
    """Pure: boundaries landing exactly a lead-day away from today.

    Returns [(key, row, label, date, days_until)]; the key carries the lead so
    the 7-day and the 1-day heads-up for the same boundary each fire once.
    """
    leads = {int(d) for d in lead_days}
    out: list[tuple[str, dict, str, _dt.date, int]] = []
    for row in rows:
        for label, when in _boundaries(row):
            days_until = (when - today).days
            if days_until not in leads:
                continue
            key = ids.short(f"itsc|{when.isoformat()}|{label}|"
                            f"{_fold(row['activity'])}|{days_until}")
            out.append((key, row, label, when, days_until))
    return out


def _when_phrase(days_until: int) -> str:
    if days_until == 0:
        return "today"
    if days_until == 1:
        return "tomorrow"
    return f"in {days_until} days"


def _today_local() -> _dt.date:
    # DR is UTC-4 year-round (no DST), per the quiet_hours config note.
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-4))).date()


def _collect_rows(cfg: dict) -> tuple[list[dict], str] | None:
    """Fetch the newest calendar PDFs and parse their rows. Returns (rows,
    click_url) or None when the page itself was unreachable (leave the sent
    baseline alone); a single bad PDF is skipped."""
    url = cfg.get("url") or PAGE_URL
    try:
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        links = _calendar_links(resp.text)
    except Exception as exc:  # noqa: BLE001 - a fetch failure is non-fatal
        log.error("ITSC calendar page fetch failed: %s", exc)
        return None
    if not links:
        log.error("ITSC page has no calendar PDF links (page restructured?)")
        return None

    rows: list[dict] = []
    for pdf_url in links[:MAX_PDFS]:
        try:
            pdf = requests.get(pdf_url, headers=HEADERS, timeout=60)
            pdf.raise_for_status()
            reader = PdfReader(io.BytesIO(pdf.content))
            text = "\n".join(p.extract_text(extraction_mode="layout")
                             for p in reader.pages)
        except Exception as exc:  # noqa: BLE001 - skip a bad PDF, keep the rest
            log.error("ITSC calendar PDF %s failed: %s", pdf_url, exc)
            continue
        got = _parse_rows(text)
        log.info("ITSC calendar %s: %d row(s)", pdf_url.rsplit("/", 1)[-1], len(got))
        rows.extend(got)
    return rows, links[0]


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state
    if PdfReader is None:
        log.error("pypdf is not installed; skipping ITSC calendar")
        return state

    cfg = config.section("itsc")
    lead_days = cfg.get("lead_days") or DEFAULT_LEAD_DAYS
    collected = _collect_rows(cfg)
    if collected is None:
        return state
    rows, click_url = collected
    due = _due(rows, _today_local(), lead_days)

    sent = state.get(STATE_KEY)
    if sent is None:
        state[STATE_KEY] = [key for key, *_ in due][:CAP]
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 STATE_KEY, len(state[STATE_KEY]))
        return state

    sent = ids.normalize_seen(sent)
    sent_set = set(sent)
    fresh: list[str] = []
    for key, row, label, when, days_until in due:
        if key in sent_set:
            continue
        sent_set.add(key)
        fresh.append(key)
        verb = {"on": "is", "starts": "starts", "ends": "ends"}[label]
        state = events.emit(
            state,
            title=f"ITSC: {row['activity']}",
            body=(f"{verb.capitalize()} {when.strftime('%A %d %B')} "
                  f"({_when_phrase(days_until)}). Tap for the academic "
                  f"calendar (PDF)."),
            topic="itsc",
            severity="high" if days_until <= 1 else "moderate",
            source="ITSC",
            click_url=click_url,
            tags="mortar_board",
            legacy_action="push",
        )
    if fresh:
        log.info("itsc: %d heads-up(s) sent", len(fresh))
    state[STATE_KEY] = (fresh + sent)[:CAP]
    return state
