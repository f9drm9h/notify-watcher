"""Topic: scheduled electricity outages for Santo Domingo (EDESUR).

EDESUR Dominicana publishes its week of "Mantenimientos Programados" as one
HTML page: a tab per day (with the date), and inside each day an accordion per
province whose body lists time windows and the neighborhoods they affect.
There is no feed or API, so this scrapes that page. (EDENORTE covers the
north of the country; for Santo Domingo, EDESUR's page is the relevant one —
point `outages.url` at a different distributor's page and adjust `regions`
to move territories.)

Only notices for the configured `regions` (default: the Santo Domingo
province and the Distrito Nacional) are considered. A matching outage pushes
the day before it happens (configurable via `lead_days`) — or the day of, if
it only appeared on the site that late. Dedup is by (date, province, time
window, zones), so a notice alerts once even though the page shows the whole
week on every run; the first run seeds the currently published week silently.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import unicodedata

import requests
from bs4 import BeautifulSoup

from .. import config, events, ids

log = logging.getLogger(__name__)

STATE_KEY = "outage_seen_ids"
CAP = 400
DEFAULT_URL = "https://www.edesur.com.do/enlaces-empresa/mantenimientos-programados/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; notify-watcher/1.0)"}
DEFAULT_REGIONS = ["Santo Domingo", "Distrito Nacional"]
DEFAULT_LEAD_DAYS = 1

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
# "06 de junio, 2026" (the page's date format, with or without the comma)
_DATE_ES_RE = re.compile(r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s*,?\s*(\d{4})", re.IGNORECASE)
_WINDOW_PREFIX_RE = re.compile(r"zonas?\s+en\s+mantenimiento\s*", re.IGNORECASE)


def _fold(text: str) -> str:
    """Pure: lowercase and strip accents, for tolerant region matching."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower().strip()


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
    """Pure: stable dedup key for one outage notice."""
    return ids.short("|".join([row["date"].isoformat(), row["province"],
                               row["window"], row["zones"]]))


def _due(outage_date: _dt.date, today: _dt.date, lead_days: int) -> bool:
    """Pure: alert when today is within lead_days before the outage (or the
    day of, for a notice published late). Past outages never alert."""
    return outage_date - _dt.timedelta(days=lead_days) <= today <= outage_date


def _today_local() -> _dt.date:
    # DR is UTC-4 year-round (no DST), per the quiet_hours config note.
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-4))).date()


def run(state: dict) -> dict:
    cfg = config.section("outages")
    url = cfg.get("url") or DEFAULT_URL
    regions = cfg.get("regions") or DEFAULT_REGIONS
    lead_days = int(cfg.get("lead_days", DEFAULT_LEAD_DAYS))

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
    today = _today_local()
    fresh: list[str] = []
    pushed = 0

    for row in matching:
        if not _due(row["date"], today, lead_days):
            # Too far out (or already past): leave it unseen so it alerts when
            # its day-before window arrives.
            continue
        h = _key(row)
        if h in seen_set:
            continue
        seen_set.add(h)
        fresh.append(h)
        when = "today" if row["date"] == today else "tomorrow" \
            if row["date"] == today + _dt.timedelta(days=1) else row["date"].strftime("%A %d %b")
        zones = row["zones"][:300]
        state = events.emit(
            state,
            title=f"Power outage {when}: {row['province']}",
            body=f"{row['date'].strftime('%A %d %B')}, {row['window']}\nZonas: {zones}",
            topic="outages",
            severity="moderate",
            source="EDESUR",
            click_url=url,
            tags="electric_plug",
            legacy_action="push",
        )
        pushed += 1

    if pushed:
        log.info("outages: %d pushed", pushed)

    state[STATE_KEY] = (fresh + seen)[:CAP]
    return state
