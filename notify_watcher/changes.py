"""Reusable before/after diffs — turn "X changed" into "X moved from A to B (+N)".

Every change-detecting topic used to hand-roll the same ``previous -> current``
sentence (fx, visa_bulletin, games, movies, deals all had their own f-string), and
none of them computed the *magnitude* of the move — which is exactly the part a human
wants ("+115 days", "+3.3%", "-$20"). This module centralizes that: a topic hands
``diff`` the previous and current value and gets back a :class:`Change` carrying both a
rendered one-line ``summary`` (for the ntfy body / digest detail) and the structured
``metadata`` move (for the event log / dashboard, so nothing downstream re-parses the
sentence).

Pure and deterministic, exactly like ``priority.py`` and ``scoring.py``: no network, no
state, trivially unit-tested. It does NOT decide *whether* to alert (the topic's
threshold/zone logic and ``priority.decide`` own that) and it does NOT store history
(``eventlog`` owns that) — it only renders *how* a value moved once a topic has decided
to speak. Adoption is opt-in and backward compatible: a topic that never calls ``diff``
is unaffected, and ``events.emit`` gains one optional ``change=`` kwarg (see
docs/design/01-change-summary-framework.md).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

# --- kinds -----------------------------------------------------------------
NUMBER = "number"
DATE = "date"
STRING = "string"
SET = "set"
LIST = "list"

# Sign marker for downward number/date deltas and set/list removals. A plain ASCII
# hyphen-minus keeps notification bodies conventional and free of any console/log
# encoding pitfalls (the watcher logs bodies through cp1252 streams on Windows dev boxes).
_MINUS = "-"

_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}
_DDMONYY = re.compile(r"^(\d{1,2})([A-Za-z]{3})(\d{2})$")


@dataclass(frozen=True)
class Change:
    """A normalized before/after, with a rendered human summary.

    ``summary`` is the one-line sentence that goes in ``Event.body``; ``metadata``
    carries the STRUCTURED move so the digest/dashboard reuse it without re-parsing
    the sentence (number -> {abs_delta, pct_delta}; date -> {days}; set/list ->
    {added, removed}).
    """
    previous: Any
    current: Any
    kind: str
    direction: str   # up | down | changed | added | removed | mixed
    summary: str
    metadata: dict = field(default_factory=dict)


# --- value coercion --------------------------------------------------------
def _as_number(v: Any) -> Optional[float]:
    if isinstance(v, bool):  # bool is an int subclass; never treat True/False as numeric
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", ""))
        except (ValueError, AttributeError):
            return None
    return None


def _as_date(v: Any) -> Optional[_dt.date]:
    """Parse the formats the topics actually produce: ``date``/``datetime`` objects,
    ISO ``YYYY-MM-DD`` (RAWG/TMDB), and the visa bulletin's ``DDMONYY`` (``15FEB21``)."""
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            return _dt.date.fromisoformat(s[:10])
        except ValueError:
            pass
        m = _DDMONYY.match(s)
        if m:
            day, mon, yr = m.group(1), m.group(2).upper(), m.group(3)
            if mon in _MONTHS:
                try:
                    return _dt.date(2000 + int(yr), _MONTHS[mon], int(day))
                except ValueError:
                    return None
    return None


# --- default renderers -----------------------------------------------------
def _num_str(v: float) -> str:
    """Clean default number rendering: ``2.0`` -> ``"2"``, ``58.2`` -> ``"58.2"``."""
    if v == int(v):
        return str(int(v))
    return f"{v:g}"


def _date_str(d: _dt.date) -> str:
    """``"May 26 2027"`` — month abbreviation, non-zero-padded day, 4-digit year.

    Hand-rolled rather than ``strftime("%b %-d %Y")`` because ``%-d`` is not portable
    (it is a glibc extension; Windows uses ``%#d``)."""
    return f"{d.strftime('%b')} {d.day} {d.year}"


def _detect_kind(previous: Any, current: Any) -> str:
    if isinstance(previous, (set, frozenset)) and isinstance(current, (set, frozenset)):
        return SET
    if isinstance(previous, (list, tuple)) and isinstance(current, (list, tuple)):
        return LIST
    if _as_number(previous) is not None and _as_number(current) is not None:
        return NUMBER
    if _as_date(previous) is not None and _as_date(current) is not None:
        return DATE
    return STRING


# --- per-kind builders -----------------------------------------------------
def _build_number(previous, current, label, unit, fmt) -> Change:
    a, b = _as_number(previous), _as_number(current)
    render = fmt or _num_str
    abs_delta = b - a
    sign = "+" if abs_delta >= 0 else _MINUS
    mag = render(abs(abs_delta))
    unit_suffix = unit if unit else ""
    md = {"abs_delta": abs_delta}
    parts = [f"{sign}{mag}{unit_suffix}"]
    if a != 0:
        pct = abs_delta / abs(a) * 100
        md["pct_delta"] = pct
        psign = "+" if pct >= 0 else _MINUS
        parts.append(f"{psign}{abs(pct):.2f}%")
    summary = f"{_lbl(label)}moved from {render(a)} to {render(b)} ({', '.join(parts)})"
    direction = "up" if abs_delta > 0 else "down"
    return Change(previous, current, NUMBER, direction, summary, md)


def _build_date(previous, current, label, fmt) -> Optional[Change]:
    a, b = _as_date(previous), _as_date(current)
    if a is None or b is None:
        # A value the topic declared "date" didn't parse (e.g. "TBA"): degrade to a
        # string diff rather than raising, so a date<->TBA transition still speaks.
        return _build_string(previous, current, label)
    render = fmt or _date_str
    days = (b - a).days
    sign = "+" if days >= 0 else _MINUS
    summary = f"{_lbl(label)}moved from {render(a)} to {render(b)} ({sign}{abs(days)} days)"
    direction = "up" if days > 0 else "down"
    return Change(previous, current, DATE, direction, summary, {"days": days})


def _build_string(previous, current, label) -> Change:
    summary = f'{_lbl(label)}changed from "{previous}" to "{current}"'
    return Change(previous, current, STRING, "changed", summary, {})


def _build_collection(previous, current, kind, label) -> Change:
    if kind == SET:
        prev_items, cur_items = set(previous), set(current)
        added = sorted(cur_items - prev_items, key=str)
        removed = sorted(prev_items - cur_items, key=str)
    else:  # LIST — order-aware multiset difference, duplicates preserved
        added = _seq_minus(current, previous)
        removed = _seq_minus(previous, current)
    md = {"added": added, "removed": removed}
    chunks = []
    if added:
        chunks.append("+" + _join(added))
    if removed:
        chunks.append(_MINUS + _join(removed))
    summary = f"{_lbl(label)}{' / '.join(chunks)}" if chunks else f"{_lbl(label)}unchanged"
    if added and removed:
        direction = "mixed"
    elif added:
        direction = "added"
    elif removed:
        direction = "removed"
    else:
        direction = "changed"
    return Change(previous, current, kind, direction, summary, md)


# --- helpers ---------------------------------------------------------------
def _lbl(label: str) -> str:
    return f"{label} " if label else ""


def _join(items) -> str:
    return "{" + ", ".join(str(i) for i in items) + "}"


def _seq_minus(seq, other):
    """Multiset difference preserving ``seq`` order (each ``other`` item cancels one)."""
    counts: dict = {}
    for x in other:
        counts[x] = counts.get(x, 0) + 1
    out = []
    for x in seq:
        if counts.get(x, 0) > 0:
            counts[x] -= 1
        else:
            out.append(x)
    return out


# --- public entry point ----------------------------------------------------
def diff(
    previous: Any,
    current: Any,
    *,
    kind: Optional[str] = None,
    label: str = "",
    unit: str = "",
    fmt: Optional[Callable] = None,
    template: Optional[Callable] = None,
) -> Optional[Change]:
    """Return a :class:`Change` describing how ``previous`` became ``current``.

    Returns ``None`` when the values are equal (a no-op — nothing to say), so a topic
    may call ``diff`` unconditionally and only emit when it is truthy. ``previous is
    None`` (first sighting) is the topic's own concern — first-seen messages are not
    diffs and stay as-is; ``diff`` is only for the *moved* case.

    ``kind`` forces a comparison kind; otherwise it is auto-detected (set/list/number/
    date/string). ``label`` is the subject ("USD/DOP", "GTA VI"). ``unit`` is appended
    to a number's absolute delta. ``fmt`` renders each typed value (a ``float`` for
    numbers, a ``date`` for dates) — currency, fixed decimals, a date style — keeping
    the standard sentence. ``template`` is the full escape hatch: it receives the built
    :class:`Change` and returns the final summary string.
    """
    if previous == current:
        return None
    if kind is None:
        kind = _detect_kind(previous, current)

    if kind == NUMBER:
        change = _build_number(previous, current, label, unit, fmt)
    elif kind == DATE:
        change = _build_date(previous, current, label, fmt)
    elif kind in (SET, LIST):
        change = _build_collection(previous, current, kind, label)
    else:
        change = _build_string(previous, current, label)

    if template is not None and change is not None:
        change = replace(change, summary=template(change))
    return change
