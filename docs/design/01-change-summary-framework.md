# Design — Reusable "change summary" framework

**Status:** proposal (design-first; no code yet)
**Composes with:** `events.Event.body`, the digest `detail` line, the event-log sink in [02-dashboard.md](02-dashboard.md).

## Problem

Notifications say *that* something changed, not *how*. And every change-detecting
topic hand-rolls the same `previous -> current` sentence:

| topic | today's body | file |
|---|---|---|
| fx | `USD/DOP rose to 60.10, above 59.00.` | `topics/fx.py` |
| visa_bulletin | `F4 (All Other) Dates for Filing changed: 01JAN21 -> 15FEB21` | `topics/visa_bulletin.py` |
| games | `GTA VI release date changed: 2027-05-26 -> 2027-09-18` | `topics/games.py` |
| movies | `<title> release date changed: <prev> -> <cur>` | `topics/movies.py` |
| deals | `Price dropped: USD 99.99 -> USD 79.99` | `topics/deals.py` |

Five copies of "format old, format new, join with `->`". None of them compute the
*magnitude* of the move (+115 days, +3.3%, −$20), which is exactly the part a human
wants. We want:

- `GTA VI moved from May 26 2027 to Sep 18 2027 (+115 days)`
- `USD/DOP moved from 58.20 to 60.10 (+3.3%)`

## Design goals (from the task)

1. One generic diff utility over numbers, dates, strings, sets, lists.
2. Topics supply `previous` + `current`; framework produces the human summary.
3. Topics can override formatting (currency, date style, units).
4. Minimal duplicated logic across topic modules.
5. **Backward compatible** — a topic that doesn't adopt it keeps working unchanged.

## Architecture

One new pure module, `notify_watcher/changes.py`, plus an **opt-in** hook on
`events.emit`. Pure + deterministic, exactly like `priority.py` and `scoring.py`,
so it is trivially unit-testable with no network.

```
topic detects prev/current ──► changes.diff(prev, cur, ...) ──► Change
                                                                  │
                                  ┌───────────────────────────────┤
                                  ▼                               ▼
                          Change.summary (str)           Change.metadata (dict)
                                  │                               │
                          events.emit(..., change=Change)         │
                                  │                               │
                   body = change.summary (if body="")     metadata["change"] = {...}
                                  │                               │
                                  ▼                               ▼
                         ntfy push / digest detail        event-log artifact (Task 2)
```

### Core data type

```python
# notify_watcher/changes.py
from dataclasses import dataclass, field
from typing import Any, Optional, Callable

@dataclass(frozen=True)
class Change:
    """A normalized before/after, with a rendered human summary."""
    previous: Any
    current: Any
    kind: str                 # "number" | "date" | "string" | "set" | "list"
    direction: str            # "up"|"down"|"changed"|"added"|"removed"|"mixed"
    summary: str              # one-line human sentence (goes in Event.body)
    metadata: dict = field(default_factory=dict)
    # metadata carries the STRUCTURED move so the dashboard/digest can reuse it
    # without re-parsing the sentence, e.g.:
    #   number: {"abs_delta": 1.9, "pct_delta": 3.27}
    #   date:   {"days": 115}
    #   set:    {"added": [...], "removed": [...]}
```

### The one entry point

```python
def diff(
    previous,
    current,
    *,
    kind: Optional[str] = None,      # force a kind; else auto-detect
    label: str = "",                 # subject, e.g. "USD/DOP", "GTA VI"
    unit: str = "",                  # appended to numbers, e.g. "days", "%", "USD"
    fmt: Optional[Callable] = None,  # per-value renderer override (currency, dates)
    template: Optional[str] = None,  # full-sentence override (escape hatch)
) -> Optional[Change]:
    """Return a Change, or None when previous == current (no-op, nothing to say).

    Returning None on equality means a topic can call diff() unconditionally and
    only emit when it's truthy — but topics already gate on "changed", so adoption
    is just: build the body via diff() instead of an f-string.
    """
```

Returning `None` on equality keeps the *first-seen* path (`previous is None`) the
topic's own concern — first-seen messages ("Now tracking X") are not diffs and stay
as-is. `diff` is only for the *moved* case.

### Kind detection (the `kind=None` default)

| detected kind | trigger | summary shape |
|---|---|---|
| `number` | both `int`/`float`, or both parse as numeric | `{label} moved from {a} to {b} ({±abs}{unit}, {±pct}%)` |
| `date` | both `date`/`datetime`, or both parse via a date sniffer | `{label} moved from {a} to {b} ({±N} days)` |
| `set` | both `set`/`frozenset` | `{label}: +{added} / −{removed}` |
| `list` | both `list`/`tuple` | ordered diff: `added {…}, removed {…}` (sequence, dupes kept) |
| `string` | fallback | `{label} changed from "{a}" to "{b}"` |

A topic that knows its type passes `kind=` to skip detection (e.g. visa dates are
strings like `15FEB21`, so it passes `kind="date"` to get the day delta). The date
sniffer reuses one shared parser (ISO `YYYY-MM-DD`, the visa `DDMONYY`, and TMDB/RAWG
formats) so no topic re-implements date parsing.

### Formatting overrides (goal 3)

Two override levels, smallest blast radius first:

1. `unit=` / `fmt=` — render each value but keep the standard sentence.
   - deals: `fmt=lambda p: _fmt(p, currency)` → `"USD 79.99"`.
   - fx: `unit="", fmt=lambda r: f"{r:.2f}"` → `"60.10"`.
   - dates: `fmt=` a `strftime("%b %-d %Y")` so the sentence reads `Sep 18 2027`.
2. `template=` — full escape hatch for a topic whose phrasing can't be expressed
   by the standard shape; receives the `Change` fields and returns the sentence.
   This is the "topics can override if needed" guarantee, and keeps the common
   path duplication-free while never blocking an odd one out.

### Integration with `events.emit` (the keystone)

Add **one optional parameter** to `emit`, fully backward compatible:

```python
def emit(state, *, title, topic, body="", ..., change: Optional[Change] = None):
    if change is not None:
        if not body:
            body = change.summary          # the human "how it moved" line
        md.setdefault("change", change.metadata | {"summary": change.summary,
                                                    "kind": change.kind,
                                                    "direction": change.direction})
    ...
```

Why this is the right seam:
- **Backward compatible:** every existing `emit` call omits `change=` and is byte-identical. The full 271-test suite stays green.
- **Single source of truth:** the structured move lands in `Event.metadata["change"]`, so the digest `detail` line, the ntfy body, and the Task-2 event log all read the *same* data — no re-parsing the sentence downstream.
- **Severity could even key off magnitude later** (a +200-day delay is more alarming than +3 days) — `priority.decide` already reads `event.metadata`, so a future boost rule can match `change.direction`/magnitude with zero plumbing.

## Worked examples (before → after)

### 1. Exchange rates — `topics/fx.py` (number)
```python
ch = changes.diff(prev_rate, rate, label=f"{base}/{quote}", fmt=lambda r: f"{r:.2f}")
# ch.summary -> "USD/DOP moved from 58.20 to 60.10 (+1.90, +3.27%)"
state = events.emit(state, title=f"{base}/{quote} rate", change=ch,
                    topic="fx", severity="moderate", source="FX", tags="moneybag",
                    legacy_priority="default")
```
Note: fx today alerts on *zone transitions*, not raw moves. It keeps the zone logic
and simply enriches the body with the magnitude — band context ("above 59.00") can be
appended to `ch.summary` or carried in `template=`.

### 2. Visa bulletin — `topics/visa_bulletin.py` (date, custom parse)
```python
ch = changes.diff(previous, current, kind="date", label=f"F4 {label}")
# previous="01JAN21", current="15FEB21"
# ch.summary -> "F4 Dates for Filing moved from Jan 1 2021 to Feb 15 2021 (+45 days)"
# ch.metadata -> {"days": 45}   (a forward visa movement — the number you care about)
```
The shared date sniffer recognises the `DDMONYY` bulletin format, so the topic stops
storing/printing the raw cell and starts reporting "how many days it advanced".

### 3 & 4. Movie / game release dates — `topics/movies.py`, `topics/games.py` (date)
```python
ch = changes.diff(previous, current, kind="date", label=name,
                  fmt=lambda d: d.strftime("%b %-d %Y"))
# ch.summary -> "GTA VI moved from May 26 2027 to Sep 18 2027 (+115 days)"
```
Both modules carry near-identical release-tracking code; both adopt the same one-liner,
deleting two copies of the `prev -> cur` f-string. (`+115 days` reads as a delay; a
negative delta reads as "pulled forward".)

### 5. Product prices — `topics/deals.py` (number, currency)
```python
ch = changes.diff(previous, price, label=name, fmt=lambda p: _fmt(p, currency))
# ch.summary -> "Soundcore Liberty 4 moved from USD 99.99 to USD 79.99 (-20.00, -20.0%)"
# deals only emits on a DROP, so it can phrase as "Price dropped:" + ch.summary,
# and append the target note exactly as today.
```

## Backward-compatibility & rollout

- `changes.py` is additive; `emit` gains one optional kwarg → **zero** behavior change until a topic opts in.
- Adopt one topic per commit (fx → deals → games → movies → visa), each with a small unit test pinning the summary string, mirroring how the priority engine was migrated phase-by-phase.
- A topic that never adopts it is unaffected — the framework is pull, not push.

## What this deliberately does NOT do

- It does not decide *whether* to alert (that's the topic's threshold/zone logic and `priority.decide`). It only renders *how* something moved once the topic has decided to speak.
- It does not store history — that's the event-log artifact in Task 2, which this feeds.

## Test plan

`tests/test_changes.py` — pure cases per kind: number (abs+pct, sign, zero-prev guard
for pct), date (forward/backward day delta, each accepted input format), string, set
(added/removed), list (order-aware), plus `fmt`/`unit`/`template` overrides and the
`previous == current → None` no-op. Then one assertion per migrated topic that its
emitted `body` equals the expected sentence.
