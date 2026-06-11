"""Topic: scheduled electricity outages (EDEESTE first, EDESUR optional).

Home — Hainamosa, Santo Domingo Este — is EDEESTE territory. EDEESTE
publishes its week of scheduled work as a PDF behind a WordPress download
page: an archive lists one package per week ("Desde el Lunes 08 de Junio
hasta el Domingo 14 Junio 2026"), the package page carries the real download
URL, and the PDF is a day-by-day table of circuits and the zonas they cut.
The PDF has a text layer but a hostile layout: the page is split into
side-by-side half-page panels (Mon-Wed left, Thu onward right), and the day
headers routinely carry the WRONG month name (a "JUEVES 11 DE MAYO" header
inside the June 8-14 PDF). So parsing leans only on what is reliable: panel
columns are detected from the header x-positions, day sections are read
top-to-bottom per panel, and each header's date is resolved from its day
NUMBER against the week range in the package title — the month word is
ignored. A configured zone (accent/case-insensitive substring, e.g.
"Hainamosa") found inside a day's section schedules an alert for that day,
pushed `lead_days` before (or the day of, when published late). Dedup is by
(date, zone), so each outage day alerts once; the first run seeds silently.

EDESUR (the capital's south/west) is also supported: its weekly
"Mantenimientos Programados" HTML page is parsed per province when
`outages.regions` is non-empty. It is disabled by default — home is not in
its territory — but re-enables by listing provinces in `regions`.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import io
import logging
import re
import unicodedata

import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - requirements.txt installs it
    PdfReader = None

from .. import config, events, ids

log = logging.getLogger(__name__)

STATE_KEY = "outage_seen_ids"  # EDESUR notices
EDEESTE_STATE_KEY = "outage_edeeste_seen_ids"  # EDEESTE (date, zone) hits
CAP = 400
EDESUR_URL = "https://www.edesur.com.do/enlaces-empresa/mantenimientos-programados/"
EDEESTE_ARCHIVE_URL = "https://edeeste.com.do/index.php/programa-de-mantenimiento/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notify-watcher/1.0)"}
DEFAULT_LEAD_DAYS = 1
# Weekly packages to read per run: the current week plus, near the weekend,
# the next week's package once EDEESTE posts it.
MAX_PACKAGES = 2

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
# "06 de junio, 2026" (EDESUR's date format, with or without the comma)
_DATE_ES_RE = re.compile(r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s*,?\s*(\d{4})", re.IGNORECASE)
_WINDOW_PREFIX_RE = re.compile(r"zonas?\s+en\s+mantenimiento\s*", re.IGNORECASE)

# EDEESTE: weekly package links on the archive page (WordPress Download Manager)
_PKG_RE = re.compile(
    r"package-title[^>]*>\s*<a\s+href=[\"']([^\"']+)[\"']\s*>([^<]+)</a>",
    re.IGNORECASE)
_DLURL_RE = re.compile(r'data-downloadurl="([^"]+)"')
# A day header anywhere in a (folded) line; the column where it starts marks
# its panel. The month word is captured but deliberately not trusted.
_DAY_HEADER_RE = re.compile(
    r"(?:^|\s)(lunes|martes|miercoles|jueves|viernes|sabado|domingo)"
    r"\s+(\d{1,2})\s+de\s+[a-z]+")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*[ap]\.?\s*m\.?", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_DAYNUM_RE = re.compile(r"\b(\d{1,2})\b")


def _fold(text: str) -> str:
    """Pure: lowercase and strip accents, for tolerant matching."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower().strip()


def _today_local() -> _dt.date:
    # DR is UTC-4 year-round (no DST), per the quiet_hours config note.
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-4))).date()


def _when_phrase(outage_date: _dt.date, today: _dt.date) -> str:
    if outage_date == today:
        return "today"
    if outage_date == today + _dt.timedelta(days=1):
        return "tomorrow"
    return outage_date.strftime("%A %d %b")


def _due(outage_date: _dt.date, today: _dt.date, lead_days: int) -> bool:
    """Pure: alert when today is within lead_days before the outage (or the
    day of, for a notice published late). Past outages never alert."""
    return outage_date - _dt.timedelta(days=lead_days) <= today <= outage_date


# --------------------------------------------------------------------------
# EDEESTE: weekly PDF behind the download archive
# --------------------------------------------------------------------------

def _parse_packages(html: str) -> list[tuple[str, str]]:
    """Pure: archive page -> [(package_url, title)], newest first."""
    return [(u, _html.unescape(t).strip()) for u, t in _PKG_RE.findall(html)]


def _parse_week_range(title: str) -> tuple[_dt.date, _dt.date] | None:
    """Pure: a package title -> (monday, sunday) of the week it covers.

    Titles are inconsistent — "Desde el Lunes 08 de Junio hasta el Domingo 14
    Junio 2026" names the month twice, "Desde el Lunes 04 hasta el Domingo 10
    de Mayo 2026" once — so this reads the two day numbers, the month
    name(s), and the year, wherever they sit. A December-to-January week rolls
    the start back into the previous year.
    """
    f = _fold(title)
    year_m = _YEAR_RE.search(f)
    days = [int(n) for n in _DAYNUM_RE.findall(f)]
    months = [_MONTHS_ES[w] for w in
              re.findall(r"\b(%s)\b" % "|".join(_MONTHS_ES), f)]
    if not year_m or len(days) < 2 or not months:
        return None
    year = int(year_m.group(1))
    m1, m2 = (months[0], months[-1]) if len(months) > 1 else (months[0], months[0])
    try:
        start = _dt.date(year - 1 if m1 > m2 else year, m1, days[0])
        end = _dt.date(year, m2, days[1])
    except ValueError:
        return None
    return (start, end) if start <= end else None


def _resolve_day(day_num: int, start: _dt.date, end: _dt.date) -> _dt.date | None:
    """Pure: the date within [start, end] whose day-of-month is `day_num`.

    This is what makes the parser immune to EDEESTE's wrong-month headers:
    the day number is always right, the month word frequently is not.
    """
    d = start
    while d <= end:
        if d.day == day_num:
            return d
        d += _dt.timedelta(days=1)
    return None


def _panel_lines(lines: list[str]) -> list[str]:
    """Pure: re-order a two-panel page into one top-to-bottom column.

    The PDF lays the week out as side-by-side half-page panels, so a raw
    line interleaves both. Day headers betray each panel's left edge (their
    x-position in the layout text); every line is sliced at those edges and
    the panels are concatenated in reading order. A single-panel page passes
    through unchanged.
    """
    cols: set[int] = set()
    for line in lines:
        for m in _DAY_HEADER_RE.finditer(line):
            cols.add(m.start(1))
    if not cols:
        return lines
    # Cluster header x-positions into panel edges (>40 chars apart = a new
    # panel; smaller wobble is the same panel). The first panel always spans
    # from column 0, wherever its headers sit.
    edges: list[int] = []
    for c in sorted(cols):
        if not edges or c - edges[-1] > 40:
            edges.append(c)
    edges[0] = 0
    blocks: list[list[str]] = [[] for _ in edges]
    for line in lines:
        for i, left in enumerate(edges):
            right = edges[i + 1] if i + 1 < len(edges) else len(line)
            blocks[i].append(line[left:right].rstrip())
    return [ln for block in blocks for ln in block]


def _day_sections(lines: list[str], start: _dt.date, end: _dt.date,
                  ) -> list[tuple[_dt.date, list[str]]]:
    """Pure: split panel-ordered lines into per-day sections with real dates."""
    sections: list[tuple[_dt.date, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        m = _DAY_HEADER_RE.search(line)
        if m and m.start(1) <= 1:  # header at a panel's left edge, not body text
            d = _resolve_day(int(m.group(2)), start, end)
            current = [] if d else None
            if d:
                sections.append((d, current))
            continue
        if current is not None:
            current.append(line)
    return sections


def _window_near(lines: list[str], idx: int, radius: int = 4) -> str:
    """Pure: best-effort '9:20 a.m. a 3:20 p.m.' from the lines around a zone
    mention; the table puts the window on the circuit's row nearby. Empty
    string when none is found — the alert goes out without it."""
    for off in sorted(range(-radius, radius + 1), key=abs):
        i = idx + off
        if 0 <= i < len(lines):
            times = _TIME_RE.findall(lines[i])
            if len(times) >= 2:
                return f"{times[0]} a {times[1]}"
    return ""


def _scan_pdf_text(text: str, start: _dt.date, end: _dt.date,
                   zones: list[str]) -> list[dict]:
    """Pure: layout-mode PDF text -> [{date, zone, window}] for watched zones.

    A zone term matching anywhere in a day's section (circuit name like
    "HAINAMOSA, NO 6" or a sector list naming the barrio) counts as a hit
    for that day; one hit per (day, zone).
    """
    folded = [_fold(ln) for ln in text.splitlines()]
    rows: list[dict] = []
    for date, sec_lines in _day_sections(_panel_lines(folded), start, end):
        for zone in zones:
            z = _fold(zone)
            if not z:
                continue
            idx = next((i for i, ln in enumerate(sec_lines) if z in ln), None)
            if idx is None:
                continue
            rows.append({"date": date, "zone": zone,
                         "window": _window_near(sec_lines, idx)})
    return rows


def _edeeste_key(date: _dt.date, zone: str) -> str:
    """Pure: dedup key — one alert per outage day per watched zone."""
    return ids.short(f"edeeste|{date.isoformat()}|{_fold(zone)}")


def _edeeste_collect(cfg: dict, today: _dt.date) -> list[dict] | None:
    """Fetch the newest still-relevant weekly PDFs and scan them for the
    watched zones. None means the archive itself was unreachable (leave the
    seen baseline alone); a per-package failure just skips that package."""
    url = cfg.get("archive_url") or EDEESTE_ARCHIVE_URL
    try:
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        packages = _parse_packages(resp.text)
    except Exception as exc:  # noqa: BLE001 - a fetch failure is non-fatal
        log.error("EDEESTE archive fetch failed: %s", exc)
        return None
    if not packages:
        log.error("EDEESTE archive parsed to 0 packages (page restructured?)")
        return None

    zones = cfg.get("zones") or []
    rows: list[dict] = []
    read = 0
    for pkg_url, title in packages:
        week = _parse_week_range(title)
        if week is None:
            continue
        if week[1] < today:
            break  # newest-first: everything after this is older still
        if read >= MAX_PACKAGES:
            break
        read += 1
        try:
            page = requests.get(pkg_url, headers=HEADERS, timeout=40)
            page.raise_for_status()
            m = _DLURL_RE.search(page.text)
            if not m:
                log.warning("EDEESTE package %r has no download link", title)
                continue
            pdf = requests.get(_html.unescape(m.group(1)), headers=HEADERS, timeout=60)
            pdf.raise_for_status()
            reader = PdfReader(io.BytesIO(pdf.content))
            text = "\n".join(p.extract_text(extraction_mode="layout")
                             for p in reader.pages)
        except Exception as exc:  # noqa: BLE001 - skip a bad package, keep the rest
            log.error("EDEESTE package %r failed: %s", title, exc)
            continue
        hits = _scan_pdf_text(text, week[0], week[1], zones)
        log.info("EDEESTE %r: %d watched-zone day(s)", title, len(hits))
        for r in hits:
            r["url"] = pkg_url
            rows.append(r)
    return rows


def _run_edeeste(state: dict, cfg: dict, lead_days: int, today: _dt.date) -> dict:
    if PdfReader is None:
        log.error("pypdf is not installed; skipping EDEESTE outages")
        return state
    rows = _edeeste_collect(cfg, today)
    if rows is None:
        return state

    seen = state.get(EDEESTE_STATE_KEY)
    if seen is None:
        state[EDEESTE_STATE_KEY] = [_edeeste_key(r["date"], r["zone"])
                                    for r in rows][:CAP]
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 EDEESTE_STATE_KEY, len(state[EDEESTE_STATE_KEY]))
        return state

    seen = ids.normalize_seen(seen)
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = 0
    for r in rows:
        if not _due(r["date"], today, lead_days):
            # Too far out (or past): leave it unseen so it alerts when its
            # day-before window arrives.
            continue
        h = _edeeste_key(r["date"], r["zone"])
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)
        window = f", {r['window']}" if r["window"] else ""
        state = events.emit(
            state,
            title=f"Power outage {_when_phrase(r['date'], today)}: {r['zone']}",
            body=(f"{r['date'].strftime('%A %d %B')}{window}\n"
                  f"EDEESTE's weekly maintenance program lists {r['zone']}. "
                  f"Tap for the schedule (PDF)."),
            topic="outages",
            severity="moderate",
            source="EDEESTE",
            click_url=r.get("url"),
            tags="electric_plug",
            legacy_action="push",
        )
        pushed += 1
    if pushed:
        log.info("outages (EDEESTE): %d pushed", pushed)
    state[EDEESTE_STATE_KEY] = (fresh + seen)[:CAP]
    return state


# --------------------------------------------------------------------------
# EDESUR: weekly HTML page (kept for the capital's west side; off by default)
# --------------------------------------------------------------------------

def _parse_date_es(text: str) -> _dt.date | None:
    """Pure: '06 de junio, 2026' -> date(2026, 6, 6); None when unrecognized."""
    m = _DATE_ES_RE.search(text or "")
    if not m:
        return None
    month = _MONTHS_ES.get(_fold(m.group(2)))
    if not month:
        return None
    try:
        return _dt.date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _matches_region(province: str, regions: list[str]) -> bool:
    """Pure: accent/case-insensitive 'is this province one we care about?'."""
    p = _fold(province)
    return any(_fold(r) and _fold(r) in p for r in regions)


def _parse_page(html: str) -> list[dict]:
    """Pure: EDESUR maintenance page -> [{date, province, window, zones}].

    Day tabs and panes are paired by their shared pill id; inside each pane an
    accordion item is one province, and its body alternates an h5.title-zona
    time window with a paragraph listing the affected zones. Rows whose date
    line cannot be parsed are dropped (we cannot schedule an alert for them).
    """
    soup = BeautifulSoup(html, "html.parser")

    dates: dict[str, _dt.date] = {}
    for btn in soup.select("button.day-tag"):
        m = re.match(r"pills-(.+)-tab$", btn.get("id") or "")
        if not m:
            continue
        d = _parse_date_es(btn.get_text(" ", strip=True))
        if d:
            dates[m.group(1)] = d

    rows: list[dict] = []
    for pane in soup.select("div.tab-pane"):
        m = re.match(r"pills-(.+)$", pane.get("id") or "")
        date = dates.get(m.group(1)) if m else None
        if date is None:
            continue
        for item in pane.select("div.accordion-item"):
            h4 = item.find("h4")
            province = h4.get_text(" ", strip=True) if h4 else ""
            if not province:
                continue
            for h5 in item.select("h5.title-zona"):
                window = _WINDOW_PREFIX_RE.sub("", h5.get_text(" ", strip=True)).strip()
                zones_p = h5.find_next_sibling("p")
                zones = zones_p.get_text(" ", strip=True) if zones_p else ""
                if window or zones:
                    rows.append({"date": date, "province": province,
                                 "window": window, "zones": zones})
    return rows


def _key(row: dict) -> str:
    """Pure: stable dedup key for one EDESUR outage notice."""
    return ids.short("|".join([row["date"].isoformat(), row["province"],
                               row["window"], row["zones"]]))


def _run_edesur(state: dict, cfg: dict, lead_days: int, today: _dt.date) -> dict:
    url = cfg.get("url") or EDESUR_URL
    regions = cfg.get("regions") or []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        rows = _parse_page(resp.text)
    except Exception as exc:  # noqa: BLE001 - a fetch/parse failure is non-fatal
        log.error("EDESUR outages fetch failed: %s", exc)
        return state

    matching = [r for r in rows if _matches_region(r["province"], regions)]
    log.info("EDESUR: %d notice(s) on page, %d in watched regions",
             len(rows), len(matching))
    if not rows:
        # A published week always has notices; an empty parse means the page
        # is blocked or was restructured, so don't touch the seen baseline.
        return state

    seen = state.get(STATE_KEY)
    if seen is None:
        state[STATE_KEY] = [_key(r) for r in matching][:CAP]
        log.info("seeded %s baseline with %d id(s) (no alerts on first run)",
                 STATE_KEY, len(state[STATE_KEY]))
        return state

    seen = ids.normalize_seen(seen)
    seen_set = set(seen)
    fresh: list[str] = []
    pushed = 0

    for row in matching:
        if not _due(row["date"], today, lead_days):
            continue
        h = _key(row)
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)
        state = events.emit(
            state,
            title=f"Power outage {_when_phrase(row['date'], today)}: {row['province']}",
            body=(f"{row['date'].strftime('%A %d %B')}, {row['window']}\n"
                  f"Zonas: {row['zones'][:300]}"),
            topic="outages",
            severity="moderate",
            source="EDESUR",
            click_url=url,
            tags="electric_plug",
            legacy_action="push",
        )
        pushed += 1

    if pushed:
        log.info("outages (EDESUR): %d pushed", pushed)

    state[STATE_KEY] = (fresh + seen)[:CAP]
    return state


def run(state: dict) -> dict:
    cfg = config.section("outages")
    lead_days = int(cfg.get("lead_days", DEFAULT_LEAD_DAYS))
    today = _today_local()

    edeeste_cfg = cfg.get("edeeste") or {}
    if edeeste_cfg.get("zones"):
        state = _run_edeeste(state, edeeste_cfg, lead_days, today)
    if cfg.get("regions"):
        state = _run_edesur(state, cfg, lead_days, today)
    if not edeeste_cfg.get("zones") and not cfg.get("regions"):
        log.info("no outage zones/regions configured; skipping")
    return state
