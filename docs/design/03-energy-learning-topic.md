# Design — "Energy & Electricity Learning" topic

**Status:** proposal (design-first; no code yet)
**Reuses:** `kb.load`/`kb.pick` (curated rotation), `summarize.one_line` (news only), `events.emit` (daily push), the `event_log` ring from [02-dashboard.md](02-dashboard.md) (the optional current-events source). Mirrors the daily-educational pattern in `topics/learn.py`, `topics/health_tip.py`, `topics/astronomy.py`.

## Context

The existing `energy` topic is a **news alerter** (collector → digest/push). The user wants the opposite: a calm, once-a-day *teaching* drip — "Today's interesting thing about electricity" — that gradually builds understanding of grids, generation, nuclear, storage, transmission, renewables, reliability, history, and infrastructure. It must be educational not urgent, deterministic (works offline from curated JSON), low-noise (exactly one push/day), and varied (no frequent repeats). This is a **new, independent topic**, not a change to the news `energy` topic.

## Design goals (from the task)

1. Exactly **one** educational push per day; never breaking-news behavior.
2. Rotate four curated content types: **A** historical events, **B** how-it-works facts, **C** modern developments, **D** infrastructure spotlights.
3. Each item answers **What happened? · Why it matters? · Why should a normal person care?**
4. Deterministic curated knowledge base; works even if all external feeds fail.
5. **Occasional** (not daily) current-events item: the highest-scoring energy story of the last 24h, summarized with a "why it matters" line.
6. Variety: avoid frequent repeats by tracking delivered items in `state.json`.
7. Warm, "did-you-know" tone.

## Architecture

One new pure-ish module `notify_watcher/topics/energy_learn.py` exposing `run(state) -> dict`, registered in `main.py`'s daily-only group. It is **daily-gated** (`NOTIFY_DAILY`) and **idempotent** (`energy_learn_last_sent == today` guard), exactly like `learn`/`health_tip`.

```
run(state)  [daily-only, once/day]
  ├─ guard: NOTIFY_DAILY set?  last_sent == today? -> skip
  ├─ choose slot:  NEWS (occasional) vs CURATED (default)
  │     NEWS chosen IFF a fresh, high-enough energy story exists AND
  │            it's been >= news_min_gap_days since the last news slot
  │     else CURATED
  ├─ CURATED: pick today's channel (A/B/C/D by day rotation) ->
  │            pick an UNSEEN entry in that channel (reset when exhausted)
  ├─ NEWS:    top energy entry in state["event_log"] within 24h ->
  │            summarize.one_line("why it matters") (verbatim headline fallback)
  ├─ compose ONE body (What / Why it matters / Why care)
  └─ events.emit(..., topic="energy_learn", severity="low", legacy_action="push")
        then stamp last_sent (+ record delivered id / last_news)
```

Why this shape:
- **Curated-first, news-occasional** keeps it teaching-led and guarantees something to send even with no network and no API key.
- **One module, four JSON channels** mirrors `learn.CHANNELS`, so adding/editing content is a JSON edit, not code.
- Reuses the engine funnel (`events.emit`) so the item is a normalized `Event`, lands in the `event_log`, and shows on the dashboard like everything else.

### Curated vs AI (decided)
Curated educational items are delivered **verbatim** — they are hand-authored to be correct and well-phrased, and we never risk an LLM dropping a number or date. `summarize.one_line` is used **only** for the optional news slot (turn a headline into a one-line "why it matters"), with a graceful fallback to the plain headline when no API key is set or the call fails. This matches `summarize`'s existing "None → fall back" contract.

## Data-file structure

Four channels, one JSON file each under `data/` (beside the existing KB files):

| Channel | File | Content type |
|---|---|---|
| A | `data/energy_history.json` | Historical events (first nuclear plant, major blackouts, grid creation, milestones) |
| B | `data/energy_facts.json` | How-it-works facts (AC vs DC, transformers, high-voltage transmission, reactor basics, grid frequency) |
| C | `data/energy_modern.json` | Modern developments (notable battery projects, new reactors/SMRs, grid modernization, fusion milestones) |
| D | `data/energy_infrastructure.json` | Infrastructure spotlights (largest dams, intercontinental links, national grids, unique engineering) |

Grid / transmission / reliability subjects live inside **B** (concepts) and **D** (physical infrastructure) rather than a separate file.

Each file is a JSON array of entries with a **stable `id`** (the dedup key for variety tracking) and the three teaching fields:

```json
[
  {
    "id": "obninsk-1954",
    "title": "The world's first grid-connected nuclear plant",
    "what": "In 1954 the Obninsk station in the USSR became the first reactor to send electricity to a public grid — about 5 megawatts.",
    "why": "It proved fission could power cities, not just weapons, opening the civilian nuclear age.",
    "care": "Every nuclear kilowatt-hour today descends from this small proof-of-concept.",
    "year": 1954,
    "src": "World Nuclear Association"
  }
]
```

- `id`, `what`, `why`, `care` are **required**; `title`, `year`, `src` are optional.
- Loaded via the existing `kb.load(path, field="what")` (it already filters out entries missing the required field and tolerates a missing/corrupt file by returning `[]`). `id` uniqueness is a content-authoring invariant; a tiny test asserts no dupes per file.
- Seeded with ~25–40 entries per channel initially (enough for months of non-repeating rotation); the KB grows by appending JSON.

## Rotation strategy

**Channel of the day (deterministic):** `CHANNELS[kb.day_of_year() % 4]` — cycles A→B→C→D→A…, so consecutive days teach different content types. Re-running on the same date always selects the same channel (safe against the runner's repeated/rebased runs).

**Entry within the channel (unseen-first):** filter the channel's entries to those whose `id` is **not** in `state["energy_learn_seen"][channel]`, then `kb.pick(unseen)` (the same day-of-year pick, now over the unseen sublist — deterministic per day, no repeat until the channel is exhausted). When `unseen` is empty, **reset** that channel's seen-list to `[]` (start a fresh pass) and pick again. This delivers the user's "track delivered items, avoid frequent repeats" with the project's existing deterministic picker.

**News slot (occasional, opportunistic):** before the curated pick, decide whether today is a news day:
- Look in `state["event_log"]` for entries with `topic == "energy"` and `ts` within the last 24h; take the highest `score`.
- Use the news slot **iff** that top score ≥ `min_news_score` **and** `today - energy_learn_last_news ≥ news_min_gap_days`.
- Otherwise fall through to the curated channel.

This keeps news genuinely occasional (gap-spaced and quality-gated), and **absent entirely when offline / no notable story** — the curated drip always covers the day. The `event_log` is the natural source: the news `energy` topic already records every routed item (even dropped ones) there, so this needs no new fetch.

## State-management strategy

All keys namespaced under `energy_learn_*` (and one nested dict), committed in `state.json` like every other topic:

| Key | Shape | Purpose |
|---|---|---|
| `energy_learn_last_sent` | `"YYYY-MM-DD"` | Idempotency guard — one push/day, survives repeated/rebased runs. |
| `energy_learn_seen` | `{channelKey: [id, …]}` | Delivered ids per channel for unseen-first rotation; each list capped (e.g. to the channel length) and reset to `[]` when the channel is exhausted. |
| `energy_learn_last_news` | `"YYYY-MM-DD"` | Spaces out the occasional news slot via `news_min_gap_days`. |

**Idempotency / ordering:** the `last_sent == today` check returns early before any selection, so a same-day re-run never double-sends. State is mutated and the delivered `id` (or `last_news`) recorded **after** a successful `emit`, then `last_sent` is stamped — matching the established `*_last_sent` pattern and the project's "commit every run" behavior.

**Config** (`monitors.json → energy_learn`, all optional with sane defaults):
```json
"energy_learn": { "min_news_score": 6, "news_min_gap_days": 5 }
```

## Example notifications

**Curated — Channel B (how-it-works), verbatim:**
```
Title: ⚡ Today's spark — Why power lines run at high voltage
Body:  What: Grid lines push electricity at hundreds of thousands of volts, then
       transformers step it back down near your home.
       Why it matters: Higher voltage means lower current for the same power, and
       lower current means far less energy wasted as heat in the wires.
       Why you should care: It's the reason a power plant can serve a city 300 km
       away without melting the cables — and why those towers are so tall.
       (Source: U.S. Dept. of Energy)
```

**Curated — Channel A (history), verbatim:**
```
Title: ⚡ Today's spark — The Northeast Blackout of 1965
Body:  What: A single mis-set relay in Ontario cascaded into a blackout across the
       US Northeast, leaving ~30 million people dark for up to 13 hours.
       Why it matters: It led directly to the creation of coordinated grid-
       reliability councils (now NERC).
       Why you should care: The rules that keep your lights on during a heat wave
       were written in response to this night.
```

**Occasional news slot (AI "why it matters", headline fallback):**
```
Title: ⚡ Energy now — Grid-scale battery milestone
Body:  Headline: "California adds 3 GW of battery storage to the grid this year."
       Why it matters: Batteries now cover the evening demand peak that gas plants
       used to handle — a structural shift in how the grid balances supply.
       (World Nuclear News · tap to read)
```

## Backward-compatibility & rollout

- Purely **additive**: a new topic module + new data files + one `main.py` registration + one optional config block. No existing topic, test, or routing changes.
- With no `energy_learn` config block, defaults apply (it still runs). With the `priority` engine on, the item routes like any `severity="low"` daily push (the engine may digest it under quiet config — acceptable and consistent).
- Author the four KB files incrementally; the topic degrades gracefully if a file is short or missing (skips to the next channel's content or, in the worst case where everything is empty, logs and sends nothing — never crashes the run).

## Testing strategy

`tests/test_energy_learn.py`, pure and network-free (KB is local; the news slot reads `event_log` from an injected `state`; `summarize.one_line` is monkeypatched or returns `None` to exercise the fallback):

- **Daily gate & idempotency:** no `NOTIFY_DAILY` → no-op; `last_sent == today` → no-op; one send stamps `last_sent`.
- **Channel rotation:** consecutive `day` values select A→B→C→D; same day → same channel.
- **Unseen-first + reset:** repeated days never repeat an `id` until the channel is exhausted, then the seen-list resets and rotation resumes.
- **News-slot decision:** chosen only when a ≥`min_news_score` energy entry exists within 24h **and** the gap since `last_news` ≥ `news_min_gap_days`; otherwise curated. Stale (>24h) or low-score entries are ignored.
- **Composition:** body contains the What/Why/Why-care parts and the source when present; AI path used for news only, with verbatim-headline fallback when `one_line` returns `None`.
- **Graceful degradation:** missing/empty KB file, empty `event_log`, and no API key each produce a sensible result (curated send or clean skip), never an exception.
- **Content invariant:** each `data/energy_*.json` is a non-empty array with unique `id`s and the required `what`/`why`/`care` fields.

Mirror the assertion style of `tests/test_learn.py` / `tests/test_health_tip.py`.
