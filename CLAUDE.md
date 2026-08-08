# CLAUDE.md — notify-watcher project reference

This file exists so that anyone with zero context — including a future you, or a
fresh Claude Code session — can understand this project without re-deriving
everything from scratch. If you're reading this because something broke, jump
straight to **RUNBOOK.md** in this same folder.

## What this project is

A personal Discord notification bot: 40 independent "topics" each check one
thing (weather, prices, bills, visa dates, media releases, etc.) and push a
Discord message when something's worth knowing. It runs unattended on a
15-minute cycle via GitHub Actions, dispatched by a Cloudflare Worker cron
(not GitHub's own schedule trigger — see "Dispatch system" below for why).

You build and maintain this entirely through Claude Code / Claude prompts —
you don't write code by hand. Keep that in mind when asking for changes:
prefer one clear goal per prompt, ask for a plan before code on anything
non-trivial, and always review the diff.

## The 40 topics, one line each

| Topic | What it does |
|---|---|
| air_quality | Local air-quality alerts (Open-Meteo) |
| anthropic_news | Official Anthropic announcements (Google News) |
| apod | NASA Astronomy Picture of the Day |
| astronomy | Astronomy almanac — moons, meteor peaks, eclipses (no network) |
| bills | Monthly utility-bill reminders (reminders.json, no network) |
| blood_donation | Blood-donation eligibility reminder (no network, daily) |
| deals | Price-drop watcher for a list of product pages |
| digest | Flushes the daily digest of moderate-importance items |
| energy | Energy/electricity domain monitor (EIA, IEA, NRC feeds) |
| energy_learn | One daily educational "Today's spark" push about electricity |
| fda | FDA drug approvals via openFDA |
| fuel | DR weekly fuel prices (MICM official notice) |
| fx | USD→DOP exchange-rate threshold alert |
| games | Video game release dates + news for a personal watchlist |
| golden_sun | Golden Sun community news (wiki + reddit + Google News) |
| groceries | Weekly grocery deals — La Sirena, Nacional, Bravo |
| habits | Daily habit tracker — reminders + reaction-based completion |
| holidays | Dominican Republic public-holiday heads-up |
| iss | Visible ISS passes over your location |
| itsc | ITSC academic-calendar deadlines |
| launches | Imminent rocket launches (Launch Library 2) |
| learn | One consolidated daily learning push |
| life_dashboard | Weekly rich Sunday digest of the past week |
| marine | Rough-seas heads-up for the nearest coast |
| music | New releases from followed artists + a daily discovery pick |
| onamet | ONAMET/INDOMET severe-weather alerts for the DR |
| outages | Scheduled electricity outages (EDEESTE) |
| quakes | Nearby earthquake alerts (USGS) |
| recap | Weekly Monday-morning summary of the past week |
| reminders | Personal expiry/deadline reminders (no network) |
| research | `/research` — on-demand article summary |
| soundcore_pro | Auto-discover new Soundcore Liberty Pro earbuds |
| spark | Consolidated push every 6h, rotating content (wikiquote/health-tip/etc.) |
| spending | Weekly spending summary from BHD transaction emails (Gmail IMAP) |
| twitch | Alert when specific Twitch streamers go live |
| uv | High-UV heads-up for your location |
| visa_bulletin | U.S. State Dept Visa Bulletin, F4 row, "All Other" column |
| watchdog | Self-monitoring over `state["topic_health"]` (no network) |
| weather | Tropical storm/hurricane alerts (NHC) |
| youtube | New uploads from followed YouTube channels |

Full detail lives in each topic's own docstring at the top of
`notify_watcher/topics/<name>.py` — always check there first, it's kept
accurate and is usually more current than any summary here.

**README note:** the README documents a few things that don't currently
exist as topics — iOS releases, baseball, beach day (removed), and Project
Gutenberg / Library of Congress channels (never built, still in the prompt
queue). Don't trust the README's feature list as ground truth; trust
`notify_watcher/main.py`'s `TOPICS` list instead.

## How the dispatch system works, end to end

1. **Cloudflare Worker cron** (`worker/wrangler.toml`, `:07/:22/:37/:52` past
   every hour) fires `scheduled()` in `worker/src/index.js`, which calls
   GitHub's `workflow_dispatch` API to trigger `watch.yml`. This replaced
   GitHub's own schedule trigger, which was measured delaying/dropping runs
   (116-min average gap vs. the configured 15, in July 2026).
2. **`watch.yml`** decides its mode: every 15-min tick runs a lightweight
   "twitch fast lane" (just `twitch` + `habits`, for live-alert latency and
   minute-level reminder slots); once per rolling 3-hour window it runs a
   **full sweep** of all 40 topics. A manual `workflow_dispatch` always runs
   full.
3. **`notify_watcher/main.py`** iterates the `TOPICS` list in order, calling
   each topic's `run(state) -> state`. Every topic is wrapped in its own
   try/except — **one topic raising never stops the sweep**; the error is
   logged to that topic's `state["topic_health"]` entry and the loop moves
   on. Topics in `health.ADOPTED` (29 of the 40) report their outcome
   through a structured contract (`health.source_ok` / `health.source_failed`)
   that distinguishes "fetch worked but found nothing" from "fetch failed" —
   the rest just raise-or-don't, which the loop still catches safely but
   logs less precisely.
4. **`discord_delivery.py`** routes each topic's notification to a Discord
   channel by category (`CATEGORY_BY_TOPIC` dict → finance/discovery/logs/
   briefing/habits). **21 of the 40 topics aren't explicitly mapped and
   fall through to `CHANNEL_GENERAL`** by deliberate design (a new topic
   ships without needing a routing PR first) — this includes some
   safety-relevant ones (`onamet`, `outages`, `weather`, `quakes`), worth
   knowing if you're ever wondering why a severe-weather alert landed in
   the general channel instead of somewhere more prominent.
5. **State** (`state.json`, `audit.json`, the encrypted spending log) is
   committed back to the repo at the end of a full-sweep run, with a
   rebase-and-retry loop (5 attempts) in case a concurrent run raced it.
6. **`alert.yml`** watches `watch.yml` and `test.yml` via `workflow_run`,
   posts a red embed on `failure`/`timed_out`/`startup_failure`, a green
   one on recovery, and runs its own 4x/day heartbeat checking that `watch`
   has *succeeded* (not just run) within 7 hours — plus a self-check on its
   own recent failure rate, since nothing else watches the alerter.
7. **The Cloudflare Worker's `externalHeartbeat()`** is the one monitor
   that runs *outside* GitHub Actions, specifically so a total GitHub
   Actions outage or billing lockout — which silences `alert.yml` too,
   since it also runs on Actions — still gets reported to Discord.

## Where secrets and config live

- **Local dev:** `.env` (gitignored) — see `watch.yml`'s env block for the
  full list of keys it reads; there's no `.env.example` yet, so that env
  block is currently the source of truth for what to set.
- **GitHub Actions secrets** (repo Settings → Secrets → Actions):
  `DISCORD_TOKEN`, `CHANNEL_FINANCE`, `CHANNEL_DISCOVERY`, `CHANNEL_LOGS`,
  `CHANNEL_BRIEFING`, `CHANNEL_HABITS`, `CHANNEL_GENERAL`,
  `DISCORD_CONTROL_CHANNEL`, `NTFY_CONTROL_TOPIC` (dormant fallback),
  `RAWG_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `NASA_API_KEY`,
  `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `SPENDING_KEY`. Everything except
  `DISCORD_TOKEN` and the channel IDs is optional — its topic degrades
  gracefully (skips, or falls back) if unset.
- **Cloudflare Worker secrets** (`wrangler secret put <NAME>`, not in
  `wrangler.toml`): `DISCORD_PUBLIC_KEY`, `DISCORD_BOT_TOKEN`,
  `DISCORD_CONTROL_CHANNEL`, `GITHUB_DISPATCH_TOKEN`, `CHANNEL_LOGS`.
- **`monitors.json`** — per-topic thresholds (e.g. fuel's 5% push
  threshold, quiet hours, watchdog settings) and the **priority engine**
  (`priority.rules`): every topic/severity combination maps to a 0-100
  score; `>=60` pushes live now, `25-59` buffers to the daily digest,
  `<25` is dropped. This is what decides whether something interrupts you
  immediately or waits for the digest.
- **Dependencies:** `requirements.txt` / `requirements-dev.txt`, all pinned
  **floor-only** (`>=`), no lock file, no Dependabot. `pypdf` and
  `cryptography` in particular are several major versions behind what's
  currently on PyPI — worth an eye given `pypdf`'s parsing behavior is
  exactly what broke `fuel` for a month (fixed 2026-08-02).

## Adding a new topic safely

1. Write `notify_watcher/topics/<name>.py` with a `run(state: dict) -> dict`
   function. Start the module with a docstring — that's what this file and
   any future audit will read to understand it.
2. If it makes a real network call, use `health.source_ok` /
   `health.source_failed` (see any existing topic in `health.ADOPTED` for
   the pattern) rather than just letting exceptions propagate — you get
   much better observability (empty-result detection, not just crash
   detection) for a small amount of extra code.
3. Register it in `notify_watcher/main.py`'s `TOPICS` list (import + tuple
   entry). Dispatch order matters only in that earlier topics run first in
   a full sweep; it's not otherwise significant.
4. Write `tests/test_<name>.py`. Nothing ships without tests here — the
   full suite currently sits at 1,187 tests and every topic has dedicated
   coverage.
5. Optional but recommended: add a `priority.rules` entry in
   `monitors.json` (default score 30, digest_floor 25, push threshold 60)
   and a `CATEGORY_BY_TOPIC` entry in `discord_delivery.py` — otherwise it
   defaults to the general digest tier and the general Discord channel,
   which is a safe default but may not be what you want long-term.
6. Run the full suite (`python -m unittest discover -s tests`) before
   committing.

## Disabling a topic safely

- **Temporary** (a source is down, you want quiet for a while): set that
  topic's severity/score to something below `digest_floor` in
  `monitors.json`, or use the `only`/manual-dispatch input to skip it —
  don't delete code for a temporary pause.
- **Permanent:** remove its tuple from `main.py`'s `TOPICS` list (keep the
  module and tests in the repo for a while in case you want it back — see
  how `ios_release`/`baseball`/`beach_day` were handled), and **update the
  README** to match, since stale feature claims in the README are exactly
  what caused confusion in the last audit.

## What to check first if something breaks

1. **`state.json`'s `topic_health` entry for that topic** — `last_ok`,
   `last_error`, `source_failed`, `empty_runs`/`empty_since` if present.
   This is the fastest single source of truth and doesn't require digging
   through logs.
2. **RUNBOOK.md** in this folder — symptom-first lookup for the failure
   modes already seen in this project.
3. **The GitHub Actions run log** for the specific topic's `[name] ...`
   log lines — `main.py` logs `starting` / `ok` / `source failed: ...` /
   `failed: ...` per topic per run.
4. **Don't trust a green run alone.** `fuel` ran "successfully" and
   reported itself healthy for a full month while returning completely
   wrong data (all six prices silently identical due to a PDF-parsing
   bug, fixed 2026-08-02). A topic that diffs against its own last value
   is especially exposed to this — a consistently wrong value looks
   identical to a stable one. If a topic has gone suspiciously quiet,
   check whether it's genuinely quiet or genuinely broken before assuming
   either.
