"""Pure F4 wait-time math over the cutoff history visa_bulletin records.

``state["f4_history"]`` holds the F4 (All Other) Final Action cutoffs the topic
has seen, one entry per bulletin month the cutoff moved::

    {"cutoff": "08NOV08", "bulletin": "2026-07"}

This module turns that history into a pace ("the cutoff advances ~N days per
bulletin") and, when ``monitors.json -> visa_bulletin.f4_priority_date`` is
set, a remaining-wait estimate ("~4.2 yr to your priority date"). Pure and
deterministic, exactly like ``changes.py`` and ``scoring.py``: no network, no
state writes, trivially unit-tested.

The pace divides total calendar days advanced by BULLETIN MONTHS elapsed, not
by history entries: a month where the cutoff did not move adds no entry but
still counts in the denominator, slowing the pace exactly as it slows the real
queue.
"""
from __future__ import annotations

from typing import Optional

# The bulletin renders cutoffs as DDMONYY ("08NOV08"); changes.py already
# parses that (plus ISO) for its date diffs, so reuse its coercer rather than
# growing a second parser that could drift from it.
from .changes import _as_date

HISTORY_KEY = "f4_history"
HISTORY_MAX = 24  # ~2 years of monthly bulletins


def _month_index(bulletin) -> Optional[int]:
    """``"2026-07"`` -> absolute month count, for counting bulletins elapsed."""
    try:
        y, m = str(bulletin).split("-")
        return int(y) * 12 + int(m)
    except (ValueError, AttributeError):
        return None


def record_cutoff(history: list, cutoff: str, bulletin: str) -> list:
    """Return ``history`` with one bulletin's cutoff added, capped at HISTORY_MAX.

    A second change seen under the same bulletin month (a State Dept
    correction) replaces that month's entry rather than appending, so one
    bulletin never counts twice in the pace math.
    """
    entry = {"cutoff": cutoff, "bulletin": bulletin}
    out = [e for e in (history or []) if isinstance(e, dict)]
    if out and out[-1].get("bulletin") == bulletin:
        out[-1] = entry
    else:
        out.append(entry)
    return out[-HISTORY_MAX:]


def estimate_wait(history: list, priority_date: str = "") -> Optional[dict]:
    """Average advance pace over ``history``, plus remaining wait when configured.

    Returns ``{"days_per_bulletin": float, "bulletins": int, "years_remaining":
    float | None}``, or ``None`` when the history cannot support an estimate
    (fewer than two entries with parseable cutoff dates, or no bulletin months
    elapsed between the first and last). ``years_remaining`` is ``None`` when
    ``priority_date`` is unset/unparseable or the pace is non-positive (a
    stalled or retrogressing cutoff has no finite ETA), and ``0.0`` when the
    cutoff has already reached the priority date.
    """
    entries = []
    for e in history or []:
        if not isinstance(e, dict):
            continue
        cutoff = _as_date(e.get("cutoff"))  # skips "C"/"U" non-date cells
        month = _month_index(e.get("bulletin"))
        if cutoff is not None and month is not None:
            entries.append((month, cutoff))
    if len(entries) < 2:
        return None

    first_month, first_cutoff = entries[0]
    last_month, last_cutoff = entries[-1]
    bulletins = last_month - first_month
    if bulletins <= 0:
        return None
    days_per_bulletin = (last_cutoff - first_cutoff).days / bulletins

    years_remaining: Optional[float] = None
    target = _as_date(priority_date) if priority_date else None
    if target is not None:
        days_left = (target - last_cutoff).days
        if days_left <= 0:
            years_remaining = 0.0
        elif days_per_bulletin > 0:
            # One bulletin per month: months needed = days_left / pace.
            years_remaining = days_left / days_per_bulletin / 12.0

    return {
        "days_per_bulletin": days_per_bulletin,
        "bulletins": bulletins,
        "years_remaining": years_remaining,
    }


def pace_sentence(est: Optional[dict]) -> str:
    """Render one estimate as an alert's trailing sentence; ``""`` for ``None``.

    "Advanced ~14 d/bulletin over 6 bulletins — ~4.2 yr to your priority date."
    Without a priority date the clause after the dash is dropped; a negative
    pace reads "Retreated" and (like a stall) carries no ETA.
    """
    if not est:
        return ""
    pace = est["days_per_bulletin"]
    n = est["bulletins"]
    verb = "Retreated" if pace < 0 else "Advanced"
    head = f"{verb} ~{abs(pace):.0f} d/bulletin over {n} bulletin{'s' if n != 1 else ''}"
    years = est.get("years_remaining")
    if years is None:
        return f"{head}."
    if years == 0.0:
        return f"{head} — your priority date is current."
    return f"{head} — ~{years:.1f} yr to your priority date."
