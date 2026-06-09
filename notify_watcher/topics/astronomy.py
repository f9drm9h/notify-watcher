"""Topic: astronomy almanac - full/new moons, meteor peaks, eclipses (no network).

Pure date math, no API: a moon-phase calculation flags the full- and new-moon
days, a small annually-recurring table covers the major meteor-shower peaks and
solstices/equinoxes, and a year-specific table covers eclipses. On the daily run
we push whatever falls today; each event fires once (a seen-set of date keys).
Update ECLIPSES_2026 (and the year) as time passes.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from datetime import date, timedelta

from .. import events

log = logging.getLogger(__name__)

STATE_KEY = "astronomy_sent"
CAP = 60

# Reference new moon (2000-01-06) and the synodic month, for the phase calc.
_REF_NEW_MOON = date(2000, 1, 6)
_SYNODIC = 29.530588853

# Annually-recurring events keyed by MM-DD (approximate peak dates).
RECURRING = {
    "01-03": "Quadrantids meteor shower peaks tonight",
    "04-22": "Lyrids meteor shower peaks tonight",
    "05-06": "Eta Aquariids meteor shower peaks tonight",
    "08-12": "Perseids meteor shower peaks tonight",
    "10-21": "Orionids meteor shower peaks tonight",
    "11-17": "Leonids meteor shower peaks tonight",
    "12-14": "Geminids meteor shower peaks tonight",
}

# Year-specific events (solstices/equinoxes drift a day; eclipses are exact).
ONE_OFF = {
    "2026-02-17": "Annular solar eclipse today",
    "2026-03-03": "Total lunar eclipse today",
    "2026-03-20": "March equinox today",
    "2026-06-21": "June solstice today",
    "2026-08-12": "Total solar eclipse today (also Perseids peak)",
    "2026-08-28": "Partial lunar eclipse today",
    "2026-09-23": "September equinox today",
    "2026-12-21": "December solstice today",
}


def _phase_fraction(d: date) -> float:
    """0.0 = new moon, 0.5 = full moon (fraction through the synodic month)."""
    days = (d - _REF_NEW_MOON).days
    return (days % _SYNODIC) / _SYNODIC


def _moon_event(today: date) -> str | None:
    """Detect a full/new moon by where the 0.5 / 0.0 crossing falls (fires once)."""
    f_today = _phase_fraction(today)
    f_tom = _phase_fraction(today + timedelta(days=1))
    if f_today <= 0.5 < f_tom:
        return "full"
    if f_tom < f_today:  # fraction wrapped past 1.0 -> 0.0
        return "new"
    return None


def _events_today(today: date) -> list[str]:
    """Pure: all almanac messages for `today` (moon + tables)."""
    msgs: list[str] = []
    moon = _moon_event(today)
    if moon == "full":
        msgs.append("Full moon tonight")
    elif moon == "new":
        msgs.append("New moon tonight - darkest skies for stargazing")
    if (r := RECURRING.get(today.strftime("%m-%d"))):
        msgs.append(r)
    if (o := ONE_OFF.get(today.isoformat())):
        msgs.append(o)
    return msgs


def run(state: dict) -> dict:
    if not os.environ.get("NOTIFY_DAILY"):
        return state

    today = _dt.date.today()
    sent = list(state.get(STATE_KEY) or [])
    sent_set = set(sent)
    new = 0
    for msg in _events_today(today):
        key = f"{today.isoformat()}|{msg[:20]}"
        if key in sent_set:
            continue
        events.emit(state, title="Sky tonight", body=msg, topic="astronomy",
                    severity="low", source="Astronomy", tags="milky_way",
                    legacy_priority="low", legacy_action="push")
        sent_set.add(key)
        sent.append(key)
        new += 1

    if new:
        log.info("astronomy: sent %d event(s)", new)
    state[STATE_KEY] = sent[-CAP:]
    return state
