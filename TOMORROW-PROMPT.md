# Change-request prompt — paste into a fresh Claude Code session (in ~/Desktop/notify-watcher)

Copy everything between the lines, fill in the change request, and paste it.

---

Context: notify-watcher is a Python personal notification hub running on GitHub
Actions, pushing to my phone via ntfy.sh. Current shape (all merged & live on
`main` as of 2026-06-09, PRs #1–#13, 396 unit tests green):

- **32 topic modules** in `notify_watcher/topics/`: visa_bulletin,
  ios_release, movies (TMDb dates + scored Google-News headlines), games (RAWG
  dates + scored news, weekly), twitch, music, soundcore_pro, deals, fda,
  energy, weather, quakes, air_quality, fx (band alerts + weekly trend line),
  launches, iss, anthropic_news, habits, digest_topic, health_tip, learn
  (6 rotating channels), energy_learn, recap (Monday weekly summary),
  reminders, blood_donation, holidays, uv, marine, beach_day (Saturday 0-10
  score), astronomy, apod (NASA picture, inline image), watchdog (alerts if a
  topic fails 48h+; runs LAST).
- **Personal Priority Engine** (`events.py` → `priority.decide`): every topic
  emits a normalized Event; `monitors.json → priority` scores it cross-topic.
  Score ≥ 60 pushes live (ntfy priority from `ntfy_bands`), 25–59 buffers into
  the daily digest, below 25 is dropped.
- **Shared engines**: `monitor.run_source` + `scoring.py` for domain feeds;
  `news.py` for per-title Google-News routing + the `is_recent` age gate
  (`news.max_age_days`=14 — Google News resurfaces old articles under new
  URLs); `digest.py` (ranked daily digest, `max_per_source`=4); `changes.py`
  for "from X to Y (+N%)" summaries; `summarize.py` for optional AI bodies.
- **Transport**: `ntfy.push` supports click_url, tags, priority, and
  `attach_url` (inline images via the ntfy Attach header). Quiet hours
  (disabled by default) DEFER suppressed pushes into the morning digest
  (`quiet_hours.defer_to_digest`); high/urgent always ring.
- **Event log + dashboard**: every routed Event lands in a capped ring (500)
  in `state.json`; `dashboard.py` regenerates `docs/dashboard/index.html`
  each run. GitHub Pages deliberately OFF — view locally.
  `state["topic_health"]` records last-ok/last-error per topic.
- **Workflows**: `watch.yml` (every 3 h; daily-only topics gate on the first
  run on/after 12:00 UTC via `main._is_daily_run`; weekly topics gate on the
  first daily run of the ISO week), `twitch.yml` (every 15 min,
  `NOTIFY_ONLY=twitch`), `test.yml` (CI on every push/PR).
- **Config (no secrets in code)**: `monitors.json`, `watchlist.json`,
  `reminders.json`, `habits.json`; curated knowledge in `data/*.json`.
  Secrets: NTFY_TOPIC + optional TMDB/RAWG/GEMINI/ANTHROPIC/NASA keys (all
  currently set except GEMINI/ANTHROPIC unknown).

Change request: [DESCRIBE THE CHANGE HERE]

Requirements:
- Follow the existing topic module pattern: a `run(state)` function registered
  in `main.TOPICS`, graceful error handling (never crash the sweep), silent
  first-run seeding where applicable, and emit through `events.emit()` so the
  priority engine, event log, and dashboard pick it up automatically.
- If it's a domain monitor, wire it through the scoring/digest engine and add
  its config block (with a `_comment`) to `monitors.json`, plus a `priority`
  rule for its topic name.
- If it reports a value change, build the body with `changes.py`; if it has a
  natural image, attach it via `metadata={"attach_url": ...}`.
- Add unit tests for any pure logic introduced (stdlib `unittest`, no network).
- Update README.md if the change adds a new topic or changes setup steps.
- Do not touch unrelated files. Work on a feature branch, open a PR, merge
  after the `unittest` CI check is green (cron only fires on `main`).

---

## Idea backlog (approved 2026-06-09 — pick one as the change request)

1. **F4 visa wait estimator** (easy, no new APIs) — store each bulletin value
   as history in state; every alert (and a quarterly summary) adds "F4
   advanced N days over the last M bulletins — at this pace, ~X years to your
   priority date." Pure logic over data the visa topic already collects;
   `changes.py` synergy. The most personally meaningful line the bot can send.
2. **"Now streaming" for watchlist movies** (easy) — TMDb watch-providers
   endpoint (existing TMDB_API_KEY): when a `watchlist.json` film becomes
   streamable in the DO region, push once saying which service. Arguably more
   useful than date tracking once a film is out.
3. **Wikipedia picture of the day in the learning push** (trivial) — the
   Wikimedia featured feed `learn.py` already fetches includes the day's
   featured image; attach it via `attach_url` (~5 lines + tests).
4. **Morning weather line on the digest** (easy) — open the daily digest with
   "Today: 31 C, rain 20%, UV 9" from Open-Meteo (already used by uv/marine/
   beach_day). Turns the digest into a true morning briefing.
5. **Hurricane cone image on weather alerts** (medium) — attach NHC's
   forecast-cone PNG to a watch/warning push. FEASIBILITY FIRST: confirm the
   per-storm graphic URL can be derived from the NHC ATOM feed entry.
6. **YouTube channel uploads** (easy) — every channel has a free no-key RSS
   feed; follow a configured channel list, one push per upload. Same pattern
   as ios_release.py.
7. **Dominican baseball** (easy-medium) — MLB Stats API (free, no key): a
   daily-digest line for a followed team's result, or milestone alerts for
   Dominican players in season.
8. **Word-of-the-day learn channel** (trivial) — Wiktionary word of the day
   feed, or a curated English-vocabulary `data/*.json` as a 7th rotation slot.
9. **Reply buttons — two-way control** (medium-hard, FLAGSHIP; design doc
   first, like the priority engine) — ntfy action buttons publish commands to
   a second private "control" ntfy topic; each run polls it (ntfy JSON poll,
   `since=`). Unlocks: "Mute movie news 7d" on the digest, "Done" on water
   nudges (→ habit streaks in the weekly recap), "Snooze" on reminders.
   Stays free; needs a NTFY_CONTROL_TOPIC secret.

Rejected (do not build): game deals watcher (CheapShark) — user doesn't buy
games currently.

## Known gaps (flag if you touch the area)

- Topics with no dedicated unit-test file: ios_release, marine,
  health_tip (failures are still isolated by main's try/except).
- README "File layout" section lists only ~8 of 33 topics and none of the
  engine modules (events/priority/changes/eventlog/dashboard/news).
- deals logs a recurring 404 for one auto-discovered Soundcore product URL
  that no longer exists; consider pruning dead auto_products entries after N
  consecutive 404s.
- A one-time cloud routine (trig_01C8w9WW9vBUhzMKnyMosTg3) bumps
  actions/checkout@v5 + setup-python@v6 on 2026-06-12; verify it merged.
