# Design — Lightweight dashboard

**Status:** ✅ implemented — event-log sink (`eventlog.py`), topic-health/last-run telemetry (`main.py`), static renderer (`dashboard.py` → `docs/dashboard/index.html`), wired into `watch.yml`. Tests in `tests/test_eventlog.py`, `tests/test_dashboard.py`. **Remaining manual step:** enable GitHub Pages (Settings → Pages → Deploy from a branch → `main` / `/docs`).
**Shares infrastructure with:** [01-change-summary-framework.md](01-change-summary-framework.md) — both build on the normalized `events.Event`.

## Constraints (from the task)

- Must stay **free**. GitHub Actions stays the only scheduler. **No always-on server.** Minimal maintenance.

These rule out anything that needs a process running between scheduled Actions ticks.
The dashboard must be **static, regenerated each run**.

## The shared foundation: an event-log artifact

Today `emit` routes an `Event` to ntfy/digest/drop and then the Event is gone — nothing
persists what was notified. Both this dashboard *and* the change-summary detail line want
the same thing: the normalized Event, kept.

So the first piece of work — shared by both tasks — is an **event-log sink**: `emit`
appends every Event it routes to a rolling log in `state.json` (or a sibling file in the
same repo, committed by the same `watch.yml` step that already commits `state.json`).

```python
# in events.emit, after a decision is made (push OR digest; drop is configurable):
eventlog.record(state, event, decision)   # append-only, capped ring buffer
```

Each record is exactly the fields the mockup needs — no new data modelling:

```json
{
  "ts": "2026-06-08T14:02:11+00:00",
  "topic": "games",
  "title": "GTA VI release date changed",
  "source": "RAWG",
  "severity": "high",
  "score": 15,                      // priority.decide's global score -> the [NN] prefix
  "action": "push",                 // push | digest | drop
  "detail": "moved from May 26 2027 to Sep 18 2027 (+115 days)",  // change.summary
  "click_url": "https://..."
}
```

`score` is already produced by `priority.decide` (`Decision.score`); `detail` is exactly
`Change.summary` from Task 1. **The dashboard invents no new pipeline — it renders the
Event the engine already builds.** This is the explicit shared-infrastructure point.

### Why a log, not just state.json's digest buffer

The digest buffer is *pending* items only and is **emptied every flush** — useless as
history. The event log is append-only (capped), so it survives flushes and powers
"recent alerts", "counts", "trends", and "priority distribution".

## Storage options evaluated

| option | free? | serverless? | maintenance | history? | verdict |
|---|---|---|---|---|---|
| **(1) GitHub Pages** | ✅ | ✅ (static host) | low | — (a *host*, not storage) | **use as the host** |
| **(2) Static HTML generation** | ✅ | ✅ | low | — (a *method*) | **use as the build** |
| **(3) JSON artifacts** (Actions artifacts) | ✅ | ✅ | medium | 90-day expiry, **not web-served**, awkward to fetch | rejected as primary |
| **(4) State-repo files** (`events.jsonl` committed by the runner) | ✅ | ✅ | low | ✅ durable, diffable, already have the commit step | **use as the store** |

These aren't mutually exclusive — they're different layers. The recommended stack uses
**(4) as the store, (2) as the build, (1) as the host.** Option (3) is rejected because
Actions artifacts expire and can't be linked as a live page. The data already round-trips
through the repo (the runner commits `state.json` every run), so adding one more committed
file is zero new infrastructure and stays 100% free/serverless.

## Recommended architecture

```
 watch.yml (full-sweep mode)
   └─ python -m notify_watcher.main
        └─ emit(...) ──► eventlog.record() ──► state["event_log"]  (capped ~500)
   └─ build step:  python -m notify_watcher.dashboard
        reads:  state.json  →  event_log (recent alerts, counts, trends, distribution)
                               digest_buffer (today's digest)
                               per-topic last-run/last-error stamps (topic health)
                               reminders state (upcoming reminders)
        writes: docs/dashboard/index.html        (single self-contained file)
                docs/dashboard/events.json        (embedded for client-side search)
   └─ commit state.json + the dashboard files  [skip ci]
 GitHub Pages serves docs/dashboard/  (Pages → "deploy from branch", /docs)
```

**No new workflow needed** — the existing `watch.yml` run that already commits state
just also writes and commits the static page. Pages redeploys on push automatically.
(Alternative: a tiny `pages.yml` on `push` to `main` that builds and deploys via
`actions/deploy-pages` — slightly cleaner separation, one more file. Recommend starting
with the simpler "deploy from /docs branch folder" and only adding `pages.yml` if we want
the build off the critical watcher path.)

### Topic health, last-run, failures

`priority.decide`/`emit` don't see failures (a topic that throws never emits). So the
build also reads lightweight run telemetry that `main.py` already has the shape for:
record per-topic `{last_ok_ts, last_error, last_error_ts}` into `state["topic_health"]`
each run (one dict write in `main`'s per-topic loop). The dashboard turns that into the
"topic health / last successful run / failures" panels. This is a small, isolated add to
`main.py`, independent of the event log.

## Dashboard layout

Matches the target mockup — day-grouped, score-prefixed, newest first:

```
notify-watcher                         last run: 2026-06-08 14:02 UTC ✓
─────────────────────────────────────────────────────────────────────
[ search alerts… ]   pushes: 12 · digested: 41 · dropped: 7  (last 7d)

Today's digest (pending, flushes 12:00 UTC) ── 6 items
  GAMES   GTA VI release date changed — moved May 26 2027 → Sep 18 2027 (+115 days)
  FX      USD/DOP moved from 58.20 to 60.10 (+3.3%)
  …

Recent alerts
  June 8
    [95] Hurricane watch issued                     quakes   ✓ pushed
    [70] FDA approval                                fda      ✓ pushed
    [40] iOS 26.1 released                           ios      · digested
    [15] GTA VI release date changed                 games    ✓ pushed
  June 7
    [80] Anthropic released Claude Code              anthropic ✓ pushed
    [35] USD/DOP left target range                   fx       · digested

Topic health                         Priority distribution (7d)
  fx          ✓  2h ago               90+  ███            3
  games       ✓  2h ago               70+  █████          5
  visa        ✓  2h ago               40+  ████████      11
  movies      ⚠  fetch failed 5h ago  <40  ████████████  29
  …

Upcoming reminders
  Jun 12  Passport renewal window opens
  Jun 20  …
```

The `[NN]` prefix is `Decision.score` straight from the engine — the dashboard reuses the
exact normalized Event (topic/severity/score/timestamp/detail) `emit` produces.

### Build strategy

- One pure renderer, `notify_watcher/dashboard.py`, mirroring `digest.py`: takes `state`
  + config, returns an HTML string. Pure → unit-testable with a synthetic state, no
  network, no headless browser.
- **Zero runtime JS dependency** for the core view (server-rendered HTML). Search/filter
  is a ~30-line inline `<script>` over the embedded `events.json` — still a static file,
  still free, no build toolchain (no npm, no bundler) to maintain.
- Self-contained `index.html` (inline CSS) so there are no asset-path/Pages-base-URL
  headaches and the whole dashboard is one diffable artifact.

## Stretch features (all fall out of the event log)

- **Search alert history** — filter over embedded `events.json` (inline JS).
- **Alert trends / most active topics** — aggregate the log by `topic` and day at build time.
- **Priority distribution** — histogram of `score` (shown above).
- **Feed/API health** — from `state["topic_health"]` (last error per topic).

## Migration plan

1. **Event-log sink** (shared with Task 1). Add `eventlog.record` + the capped
   `state["event_log"]`; wire it into `emit`. Pure, tested, no UI yet. *This is the
   foundation both tasks stand on — do it first.*
2. **Topic health telemetry.** Small `main.py` change to stamp per-topic ok/error.
3. **Static renderer.** `dashboard.py` → `index.html` from state; snapshot-tested.
4. **Wire into `watch.yml`.** Build + commit the page in the existing run; enable Pages
   on `/docs`.
5. **Stretch.** Search, trends, distribution, feed health — each an additive build-time
   aggregation over the same log.

Steps 1–2 are backend-only and safe to land before any page exists; the dashboard is
purely a *reader* of data the watcher already commits, so it can never affect notification
behavior.

## Shared-infrastructure summary (both tasks)

```
            ┌──────────────── events.Event (already exists) ────────────────┐
            │  title · body · topic · severity · source · ts · metadata     │
            └───────────────────────────────────────────────────────────────┘
                       │                                   │
        Task 1: change.diff() fills body/                  Task 2: eventlog.record()
        metadata["change"] with HOW it moved               persists the Event + score
                       │                                   │
                       └──────────────► one record ◄───────┘
                          {ts, topic, title, score, action, detail}
                                         │
                          ntfy body · digest detail · dashboard row
```

Build order: **event-log sink → change summaries → dashboard renderer.** The sink is the
single seam; the summary enriches each record's `detail`; the dashboard renders the records.
