# Change-request prompt — paste into a fresh Claude Code session (in ~/Desktop/notify-watcher)

Copy everything between the lines, fill in the change request, and paste it.

---

Context: notify-watcher is a Python personal notification hub running on GitHub
Actions, pushing to my phone via ntfy.sh. Current shape (all merged & live on
`main` as of 2026-06-09, PRs #1–#5, 329 unit tests green):

- **29 topic modules** in `notify_watcher/topics/`: visa_bulletin, wwdc,
  ios_release, movies (TMDb dates + scored Google-News headlines), games (RAWG
  dates + scored news, weekly), twitch, music, soundcore_pro, deals, fda,
  energy, weather, quakes, air_quality, fx, launches, iss, anthropic_news,
  habits, digest_topic, health_tip, learn, energy_learn, reminders,
  blood_donation, holidays, uv, marine, astronomy.
- **Personal Priority Engine** (`events.py` → `priority.decide`): every topic
  emits a normalized Event; `monitors.json → priority` scores it cross-topic.
  Score ≥ 60 pushes live (ntfy priority from `ntfy_bands`), 25–59 buffers into
  the daily digest, below 25 is dropped.
- **Shared engines**: `monitor.run_source` + `scoring.py` for domain feeds
  (fda, energy); `news.py` for per-title Google-News routing (games, movies);
  `digest.py` for the ranked daily digest; `changes.py` for human-readable
  "from X to Y (+N)" change summaries; `summarize.py` for optional AI bodies
  (Gemini → Claude fallback, plain body if no key).
- **Event log + dashboard**: every routed Event is appended to a capped ring
  (500) in `state.json`; `dashboard.py` regenerates the static
  `docs/dashboard/index.html` each run. GitHub Pages is deliberately OFF —
  view the dashboard locally. `state["topic_health"]` records last-ok /
  last-error per topic.
- **Workflows**: `watch.yml` (full sweep every 3 h; daily-only topics gate on
  the first run on/after 12:00 UTC via `main._is_daily_run`), `twitch.yml`
  (every 15 min, `NOTIFY_ONLY=twitch`), `test.yml` (CI on every push/PR).
- **Config (no secrets in code)**: `monitors.json` (location, priority rules,
  per-topic settings, scoring keywords/weights), `watchlist.json`
  (movies/games/products), `reminders.json`, `habits.json`; curated knowledge
  bases in `data/*.json` (learn + energy_learn channels). Secrets live in repo
  Actions secrets: NTFY_TOPIC (+ optional TMDB/RAWG/GEMINI/ANTHROPIC keys).

Change request: [DESCRIBE THE CHANGE HERE]

Requirements:
- Follow the existing topic module pattern: a `run(state)` function registered
  in `main.TOPICS`, graceful error handling (never crash the sweep), silent
  first-run seeding where applicable, and emit through `events.emit()` so the
  priority engine, event log, and dashboard pick it up automatically.
- If it's a domain monitor, wire it through the scoring/digest engine and add
  its config block (with a `_comment`) to `monitors.json`, plus a `priority`
  rule for its topic name.
- If it reports a value change, build the body with `changes.py` so the push
  says "from X to Y", not just "changed".
- Add unit tests for any pure logic introduced (stdlib `unittest`, no network).
- Update README.md if the change adds a new topic or changes setup steps.
- Do not touch unrelated files. Work on a feature branch, open a PR, merge
  after CI is green (cron only fires on `main`).

---

## Idea backlog (easy to implement, rich additions — pick one as the change request)

1. **Watchdog self-monitoring** — `state["topic_health"]` already records
   last-ok/last-error per topic, but nobody reads it. New tiny topic: if any
   topic has had no successful run for 48 h, push one "⚠ topic X has been
   failing since …" heads-up (once per outage). Stops a dead feed from going
   silently unnoticed for weeks. ~60 lines + tests, no network.
2. **Weekly recap push** — Sunday daily run: aggregate the event log and
   `topic_health` into one "Your week: N pushes, M digested, top topics, any
   failures" message. All data already exists; pure aggregation + formatting.
3. **Safe quiet hours (defer, don't drop)** — quiet hours is OFF because it
   *drops* overnight low/default pushes. Change it to buffer them into the
   existing digest queue so they arrive with the morning digest instead.
   Makes the feature actually enableable; reuses `digest.add`.
4. **NASA APOD daily picture** — Astronomy Picture of the Day, free API.
   ntfy supports image attachments (`Attach:` URL), which no topic uses yet —
   the first *visual* notification. Classic `run(state)` topic, ~80 lines.
5. **"Beach day" index** — the app already fetches UV, wave height, and
   weather separately. One Saturday-morning topic combines them into a single
   0–10 "beach day" score with a one-line verdict. No new APIs, pure logic =
   very testable, very DR.
6. **FX weekly trend line** — fx only alerts on band crossings; silent weeks
   tell you nothing. Weekly: "USD/DOP this week: 60.1 → 60.8 (+1.2%)" via
   `changes.fmt_number` even when inside the band. Tiny.
7. **New learning channels** — `learn.py` rotates over `data/*.json` channels;
   adding one (e.g. DR history & culture, personal finance basics) is just a
   new JSON file + one list entry.

## Known gaps (flag if you touch the area)

- README is stale: movie *news* scoring is undocumented (described as
  release-dates only), and the "File layout" section lists 8 of 29 topics and
  none of the engine modules (events/priority/changes/eventlog/dashboard).
- Topics with no dedicated unit-test file: wwdc, ios_release, marine,
  health_tip (their failures are still isolated by main's try/except).
- Event log is dominated by movie news (~85% of entries); fine under the
  500 cap, but consider a per-topic share if it starts evicting rarer topics.
