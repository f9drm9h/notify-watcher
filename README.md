# notify-watcher

Personal multi-topic monitor. Runs on a schedule in GitHub Actions, pushes
matches to your phone via [ntfy.sh](https://ntfy.sh). 100% free to run.

Topics:

- **F4 visa bulletin** — watches the F4 row, "All Chargeability Areas Except
  Those Listed" column, in BOTH State Dept family-sponsored tables (Final
  Action Dates and Dates for Filing), and alerts when either moves.
- **F4 wait estimator** — keeps a history of the Final Action cutoffs (up to
  24 bulletins) and appends a pace line to every F4 alert: "Advanced ~14
  d/bulletin over 6 bulletins — ~4.2 yr to your priority date". Set
  `monitors.json` → `visa_bulletin.f4_priority_date` (ISO `YYYY-MM-DD`, from
  your I-130 receipt notice) to get the ETA clause; leave it empty for the
  pace only. Each January/April/July/October a low-priority quarterly check-in
  lands in the digest comparing the recent pace against the full history, so
  the estimate stays visible even while the cutoff crawls.
- **WWDC announcements** — watches Apple Newsroom RSS for any post whose title
  contains "WWDC" or "Worldwide Developers Conference", and alerts on each new
  one (headline + link).
- **iOS / iPadOS releases** — watches the Apple Developer Releases RSS feed and
  alerts once per new *stable* iOS/iPadOS build (betas and RCs are skipped) so
  you know an update is available. The body is a one-line "major vs.
  minor/security point release" steer when an AI key is set (see below); the
  feed carries only version + build, not the changelog, so tap the linked
  release-notes page for detail.
- **Product deals** — for each entry in `watchlist.json` → `products` (plus any
  product auto-discovered below), reads the price from the page's schema.org
  JSON-LD and alerts on first sight, on any price drop, and when a `target_price`
  is reached. Store-agnostic and needs no API key. (Built for the Soundcore
  Liberty earbuds; works for any shop that publishes standard Product data.)
- **Soundcore Liberty Pro discovery** — reads Soundcore's product sitemap and
  alerts when a brand-new flagship Liberty Pro earbud appears (e.g. a future
  "Liberty 6 Pro"), then hands it to the deals watcher to price-track
  automatically. First run seeds the current catalog silently, so you only get
  pinged about genuinely new releases. No API key, no watchlist editing.
- **Movie release dates + streaming + news** — for each title in
  `watchlist.json` → `movies`, tracks its TMDb release date and alerts on
  first sight and on any date change (needs `TMDB_API_KEY`). Also watches
  TMDb's watch-provider data and pushes once when a film becomes streamable
  on a subscription service in the DR (e.g. "🎬 The Odyssey is now streaming
  on Netflix in DO") — rent/buy listings don't count, and the first run seeds
  silently so an already-streaming title stays quiet. Each title's Google
  News headlines are
  also scored via `movies_scoring` exactly like game news below: release
  dates / trailers / delays push live, casting / reviews / box office go to
  the daily digest, rankings / opinion / speculation are dropped.
- **Game release dates** — for each title in `watchlist.json` → `games`,
  tracks its RAWG release date and alerts the same way (a date change is a
  high-priority push). Needs `RAWG_API_KEY`. _Checked weekly (see below)._
- **Game news (scored)** — for each game title, queries Google News (no key),
  keeps only headlines specifically about that game, then runs each through the
  `games_scoring` config in `monitors.json` instead of pushing everything:
  release dates / delays / trailers / reveals / beta / DLC / sequel / official
  announcements push **live**; leaks / interviews / previews / store-page
  updates go to the **daily digest**; opinion, ranking lists, speculation, and
  passing mentions are **dropped**. Trusted/official outlets carry more weight.
  Tuning the keywords and weights is a `monitors.json` edit, not a code change.
  Both game checks run **weekly** — once per ISO week, on the first daily run of
  the week — so game updates arrive as one batched catch-up rather than a drip.
  All the Google News-based topics (game news, movie news, Anthropic) ignore
  articles older than `monitors.json` → `news.max_age_days` (default 14):
  Google News resurfaces months-old stories under brand-new URLs, which would
  otherwise slip past dedup and alert as "new".
- **Twitch live** — for each handle in `monitors.json` → `twitch.streamers`,
  checks live status via decapi.me (no key) and pushes once per live session
  (with the game + stream title), re-arming after they go offline.
- **Dominican baseball (MLB)** — two checks via the free MLB Stats API
  (statsapi.mlb.com, no key), configured in `monitors.json` → `baseball`.
  Each followed team's previous-day final score (`baseball.monitored_teams`)
  lands in the morning digest
  ("Dodgers 5 – Cubs 3 (W)"; off days stay silent), and a **live push** fires
  when a followed Dominican player has a milestone game — a home run, 3+ hits,
  or 3+ RBI ("🇩🇴 Juan Soto — 2 HR, 4 RBI vs Yankees"), at most once per game.
  Both checks skip silently in the off-season. The teams default to MLB
  clubs because LIDOM (e.g. Tigres del Licey) publishes no free API; team ids
  come from `https://statsapi.mlb.com/api/v1/teams?sportId=1`.
- **YouTube uploads** — for each channel in `monitors.json` →
  `youtube.channels`, reads the channel's free Atom feed
  (`https://www.youtube.com/feeds/videos.xml?channel_id=...`, no key) and
  pushes once per new upload (channel name + video title, tap to watch). Each
  entry is `{ "channel_id": "UC...", "name": "Display Name" }` — `channel_id`
  is the `UC...` id from the channel page URL or its source, **not** the
  `@handle`. The first sight of each channel seeds its current uploads
  silently, so adding a channel — on day one or any time later — never blasts
  its backlog.
- **Music** — `monitors.json` → `music.followed_artists` get a push on a new
  Deezer release. Plus a daily **discovery** pick: a song from a Deezer
  *related* artist seeded by your own library (`data/music_seed.json`, built by
  `tools/scan_music.py`) that you probably haven't heard — never repeating.
- **Blood-donation timer** — from `monitors.json` → `blood_donation`, reminds
  you once you're eligible to donate again (last donation + interval), re-nudging
  at most every `renotify_days`. Update `last_donation` when you donate. Daily.

### Domain monitors (scored, configured in `monitors.json`)

Unlike the entity watchers above, these scan whole domains, so they run every
item through a deterministic scorer (provenance + keyword/structural signals,
**no AI in the scoring path**). Only **breakthrough/high**-tier items push live;
**moderate** items are collected into one **daily digest**, and **minor** items
are dropped — which is what keeps a high-volume news source from causing alert
fatigue. The digest is **ranked by score**: the most important items lead the
message and, when there are more than fit, the *least* important are dropped
(shown as "+N more"). Sources, keywords, weights, and thresholds all live in
`monitors.json` (no secrets), so tuning is a config edit, not a code change.
The digest opens with a one-line morning weather summary for your `location` —
`Today: 31 °C, rain 20%, UV 9` (current temperature, rain probability, max UV
via Open-Meteo, free, no key); if the weather fetch fails for any reason the
digest simply goes out without it.

- **FDA approvals** — reads the openFDA Drugs@FDA API (free, no key) and alerts
  on new drug/biologic (NDA/BLA) approvals. Generic (ANDA) approvals are
  filtered out as routine. One alert per application; supplements land in the
  digest.
- **Energy / electricity** — reads the RSS sources in `monitors.json` → `energy`
  (EIA Today in Energy + World Nuclear News by default) and scores headlines
  against energy keywords (grid, battery, fusion, nuclear, solar, storage, …).
  No key.
- **Tropical weather / hurricanes** — reads the U.S. National Hurricane Center
  Atlantic feed (`monitors.json` → `weather`, no key). Stays silent unless a
  system names one of your `region_terms`; a watch/warning for your area pushes
  live, other region-relevant updates go to the digest. Off-region Atlantic
  activity is ignored. A live alert attaches the storm's NHC forecast-cone
  image inline (derived from the storm id in the advisory; when it can't be
  derived the alert simply goes out without the picture).
- **Nearby earthquakes** — reads the USGS feed (`monitors.json` → `quakes`, no
  key) and routes by magnitude **and** great-circle distance from your
  `location`: a strong, close quake pushes live; a smaller nearby one goes to
  the digest; everything else is dropped. A large, shallow quake within
  `tsunami_radius_km` instead fires an urgent **tsunami heads-up** pointing to
  tsunami.gov (the wide radius reflects how far tsunamis travel).
- **Air quality** — checks the local US AQI via Open-Meteo (`monitors.json` →
  `air_quality`, no key) and alerts when the air enters an unhealthy band, at
  most once per worsening band per day.
- **Exchange rate (USD→DOP)** — checks the daily rate via open.er-api
  (`monitors.json` → `fx`, no key) and pings only when it crosses out of, or
  back into, your `[low, high]` band. Once a week the morning digest also
  carries a trend line ("USD/DOP moved from 60.10 to 60.80 (+1.16%)") so quiet
  weeks inside the band still tell you which way the rate is drifting.
- **Reminders / expiry** — a tiny date engine over `reminders.json` (no network):
  document/visa/ID expiry, subscription renewals, warranties, yearly birthdays.
  Fires once at each configured lead time (default 90/30/7/1/0 days before).
  Daily run only.
- **Habit nudges** — gentle recurring reminders from `habits.json` (no network,
  no secrets). Each habit fires at several daytime slots on the every-3-hours
  grid (e.g. 12/15/18/21 UTC ≈ 08:00/11:00/14:00/17:00 DR), at most one push per
  slot per day, phrasing rotating per slot. Robust against dropped runs: a late
  or skipped run sends only the latest due slot, never a catch-up burst. A
  **drink-water** nudge ships enabled; **stand-up** and **eye-rest** ship as
  ready-to-enable examples. Adding or tuning a nudge is a `habits.json` edit
  (`name`/`title`/`tag`/`hours`/`messages`/`enabled`), not a code change.
- **Daily health tip** — one evidence-based tip each morning from a curated,
  vetted knowledge base (`data/health_tips.json`, sourced from CDC/WHO/
  MedlinePlus). With an AI key set the vetted tip is optionally *reworded* for
  variety; the fact is never AI-invented. Sent on the daily run only.
- **Daily learning** — one consolidated push each morning bundling three short
  sections: an "On this day" historical event and the day's Wikipedia featured
  article (both from Wikimedia's free, no-key feed), plus one vetted fact from a
  rotating curated channel — science, technology literacy, life skills, general
  knowledge, Dominican history & culture, personal-finance basics, or a
  **word of the day** (`data/*.json`). The word-of-the-day slot serves a curated
  vocabulary entry — word, pronunciation, part of speech, definition, and
  example sentence — from `data/vocabulary.json`, formatted verbatim (never
  LLM-reworded). The Wikimedia feed is fixed per date and the fact rotates by
  day-of-year, so the push is deterministic; each section degrades independently,
  so a feed outage still sends the rest. The push also includes the **Wikipedia
  picture of the day** as an inline image (via the `Attach` header), so the
  notification arrives with a visual — no extra config needed. Daily run only.
- **Rocket launches** — imminent orbital launches via Launch Library 2 (no key);
  alerts once per launch within `launches.imminent_hours`, skipping routine ones
  (Starlink by default).
- **ISS passes** — visible-window passes of the space station over your location
  via the g7vrd API (no key); alerts an evening/pre-dawn pass clearing
  `iss.min_elevation_deg`, with local time, peak elevation, and direction.
- **Anthropic releases** — official Anthropic posts (model launches, Claude Code
  updates) via Google News filtered to Anthropic's own `<source>`. No key.
- **DR public holidays** — heads-up before bank/office closures via Nager.Date
  (no key), on the configured lead days. Daily run only.
- **High-UV alert** — pushes when the day's max UV index reaches `uv.alert_uv`
  (Open-Meteo, no key). Daily run only.
- **Rough-seas alert** — pushes when the coast's max wave height reaches
  `marine.rough_wave_m` (Open-Meteo Marine, no key). Daily run only.
- **Beach day index** — Saturday mornings (configurable via
  `beach_day.weekdays`), one 0–10 score answering "is today a beach day?":
  wave height + rain probability + UV + max temperature at the coast, folded
  into a verdict with caution notes (rough seas, extreme UV). Open-Meteo, no
  key.
- **Astronomy almanac** — full/new moons (computed) plus meteor-shower peaks,
  solstices/equinoxes, and eclipses from a built-in table. No network. Daily run.
- **NASA picture of the day** — the day's Astronomy Picture of the Day, with
  the image attached inline in the notification (tap for the HD version; video
  days attach the thumbnail and link the video). Works without any key via
  NASA's shared `DEMO_KEY` quota; set a free `NASA_API_KEY` secret
  (api.nasa.gov) for your own quota. Daily run only.
- **Weekly recap** — one Monday-morning push summarizing the past week from the
  event log: live pushes vs. digested vs. dropped, the busiest topics, the
  highest-priority story, and whether every topic is healthy. No network, no
  key.
- **Watchdog (self-monitoring)** — every run, `main.py` records each topic's
  last successful run and latest failure in `state.json` → `topic_health`. The
  watchdog reads that record and pushes one heads-up when any topic has had no
  successful run for `watchdog.stale_hours` (default 48) — so a feed that died
  (moved URL, revoked key) can't fail silently forever. One push per outage; it
  re-arms when the topic recovers. No network, no key.

The daily digest, health tip, learning push, and reminders fire once a day, on
the first scheduled run on/after 12:00 UTC (~08:00 in the Dominican Republic,
UTC−4); the collectors run on the normal every-3-hours schedule. The game checks
fire once a *week*, on the first daily run of each ISO week. The daily/weekly
work is gated in code by the clock (`main._is_daily_run`, `games._iso_week`), not
a dedicated cron, because GitHub Actions silently drops scheduled runs. Adding a
curated learning channel is just a new `data/*.json` file referenced from
`notify_watcher/topics/learn.py`.

**Quiet hours (optional).** Set `monitors.json` → `quiet_hours.enabled` to
`true` to silence overnight pushes: any `low`/`default` notification between
`start` and `end` (local time = UTC + `utc_offset_hours`) is **deferred into
the daily digest** — it arrives with the morning flush instead of waking you —
while `high`/`urgent` safety alerts always ring through and the manual test
push is never suppressed. Set `defer_to_digest` to `false` if you'd rather
overnight pushes be dropped outright. Disabled by default, and any malformed
config fails open (sends), so it can never silently swallow your alerts. One
caveat: time-sensitive overnight pushes below the high band (e.g. a pre-dawn
ISS pass) arrive after the fact when deferred.

The app is structured so adding more topics later is a small change in
`notify_watcher/main.py`.

---

## One-time setup

You only do this once. Allow ~10 minutes.

### 1. Pick an ntfy topic name

ntfy.sh is free and needs no account. Subscribing to a topic = anyone who
knows the topic name can read every notification you send. So pick a
**long, random, hard-to-guess name** — treat it like a password.

Example: `f4-wwdc-aB7xQ9k2pZ-personal` (don't use this exact one).

### 2. Install the ntfy app on your phone and subscribe

- iOS: search "ntfy" in the App Store.
- Android: Play Store, or F-Droid.
- In the app: **Subscribe to topic** → enter the topic name from step 1.

Send a test from the command line to confirm it lands on your phone:

```bash
curl -d "hello from ntfy" https://ntfy.sh/<your-topic-name>
```

### 3. Create the GitHub repo and push this code

From inside `Desktop\notify-watcher`:

```powershell
# Create a NEW PUBLIC repo on github.com (web UI) called notify-watcher.
# Do NOT initialize it with a README on GitHub — this folder already has one.

git remote add origin https://github.com/<your-github-username>/notify-watcher.git
git push -u origin main
```

> **Why public?** Public repos get unlimited GitHub Actions minutes for
> free. Private repos are also free but capped (currently 2000 min/month),
> which is plenty for this app but unnecessary to deal with. There are no
> secrets in this code — the only secret (the ntfy topic name) lives in
> repo Secrets, not in code.

### 4. Add the secrets to the GitHub repo

On github.com → your repo → **Settings → Secrets and variables → Actions
→ New repository secret**. Add:

| Secret name    | Value                                              |
| -------------- | -------------------------------------------------- |
| `NTFY_TOPIC`   | the topic name from step 1                         |
| `NTFY_SERVER`  | (optional) leave unset to use the default `https://ntfy.sh` |
| `NTFY_CONTROL_TOPIC` | (optional) a second random private topic name, for the reply buttons below; leave unset to disable them |
| `TMDB_API_KEY` | (optional) free TMDb v3 API key, for the movie watcher |
| `RAWG_API_KEY` | (optional) free RAWG API key, for the game watcher |

The movie/game watchers only run if their key is set; without it they skip
quietly. Get the keys here (both free, ~2 min, no cost):

- **TMDb**: themoviedb.org → Settings → API → request a developer key →
  copy the **"API Key (v3 auth)"** value.
- **RAWG**: rawg.io/apidocs → "Get API Key" → sign up → copy the key.

### Reply buttons (optional): talk back to your notifications

Set the `NTFY_CONTROL_TOPIC` secret to a **second** random private topic name
(treat it like a password, e.g. `nw-ctl-x7k2m9q4w1z8r5t3`) and selected pushes
gain tappable action buttons. A tap POSTs a small command to that private
topic; the next watcher run picks it up and adjusts behavior — no server, no
extra infrastructure. The notification is dismissed when the tap goes through
(that's the ack; there is no confirmation push). Leave the secret unset and
the feature is fully off: no buttons, no polling, behavior identical to
before.

The three buttons you'll see:

| Push | Button | What it does |
| --- | --- | --- |
| Habit nudge (e.g. drink water) | **Done** | marks the habit done now and skips its next scheduled nudge today (later slots still fire) |
| Reminder | **Snooze 1h** | re-delivers that reminder after ~an hour (on the next full 3-hourly run) |
| Daily digest | **Mute movies 24h** / **Mute games 24h** | drops that topic's digest-bound items for 24 h — live high/urgent alerts are never muted |

Commands are deliberately low-stakes: the worst anyone who learns the control
topic name could do is skip a nudge, snooze a reminder, or mute digest entries
for a bounded while. Nothing reads data, edits config, or executes code; see
`docs/design/reply-buttons.md` for the full design and threat model.

### Pick what to watch: `watchlist.json`

The movie/game watchers read titles from `watchlist.json` at the repo root.
Edit it on github.com or locally — it holds no secrets:

```json
{
  "movies":   ["Avatar: Fire and Ash", "Spider-Man: Brand New Day"],
  "games":    ["Grand Theft Auto VI", "The Elder Scrolls VI"],
  "products": [
    {"name": "Soundcore Liberty 4 NC", "url": "https://www.soundcore.com/products/liberty-4-nc-a3947z11", "target_price": 79.99}
  ]
}
```

Each movie/game title is resolved to the best match on TMDb/RAWG (the matched
name is logged so you can sanity-check it). Add or remove titles any time.

For **products**, only `url` is required; `name` and `target_price` are
optional. Point `url` at the retailer's product page — prefer the
manufacturer/store page (soundcore.com, Best Buy, etc.) over Amazon, which
often blocks data-center IPs like the GitHub Actions runner and hides its price
behind JavaScript. The price is read from the page's `application/ld+json`
Product data, so any standards-compliant shop works; the matched price is
logged so you can sanity-check it.

### 5. Trigger the workflow once to verify

On github.com → your repo → **Actions → watch → Run workflow**. After it
finishes (green check), you should:

- See a push notification on your phone for at least one of the topics
  (because it's the first run and everything is "new").
- See a new commit on `main` titled `chore: update state [skip ci]` if any
  topic recorded state.

After this, the workflow runs by itself every 3 hours.

---

## How dedup works (plain language)

Every run, each topic compares "what the source says right now" against a
tiny memory file (`state.json`) that records what it last alerted on:

- **Visa**: stores the F4 date string. If today's value matches the stored
  one, no push. If different, push and overwrite the stored value.
- **WWDC**: stores a list of article URLs already pushed. Any feed item
  whose URL is not in that list is pushed and then added.
- **iOS releases**: stores a list of release titles (version + build) already
  pushed; a build not in that list is pushed and added.
- **Deals**: stores the last seen price per product URL. A push fires when the
  new price is *lower* than the stored one (or it's the first sight); a price
  rise just updates the stored baseline silently.
- **Soundcore Pro discovery**: stores the set of flagship Liberty Pro slugs it
  has seen (`soundcore_pro_seen`). A slug not in that set is a new release →
  push + add to `auto_products` (which deals price-tracks). First run records
  the baseline without alerting.

After the run, GitHub Actions commits the updated `state.json` back to the
repo. The next run reads it back, so the app never re-sends an old alert.

---

## Test locally before relying on the schedule

You'll need Python 3.10+.

```powershell
cd C:\Users\ourki\Desktop\notify-watcher

# one-time
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# every time you test
$env:NTFY_TOPIC = "<your-topic-name>"
# $env:NTFY_SERVER = "https://ntfy.sh"  # optional, only if not default

python -m notify_watcher.main
```

You should see one log line per topic and a notification on your phone for
anything not already recorded in `state.json`.

### Test a single topic

```powershell
# Visa only
python -c "from notify_watcher import state; from notify_watcher.topics import visa_bulletin; s = state.load(); s = visa_bulletin.run(s); state.save(s)"

# WWDC only
python -c "from notify_watcher import state; from notify_watcher.topics import wwdc; s = state.load(); s = wwdc.run(s); state.save(s)"
```

### Force a re-alert (for testing)

Open `state.json`, delete the key for the topic you want to re-test, save,
and run the command above. The topic will treat the next value as "new"
and push again. Don't forget to revert before pushing if you don't want a
real alert.

### Run the unit tests

The pure logic — scoring, digest, news routing, the collector engine — has a
fast unit suite (stdlib `unittest`, no extra deps, no network, sends nothing):

```powershell
python -m unittest discover -s tests -v
```

These also run automatically on every push/PR via `.github/workflows/test.yml`.
`tests/test_games_scoring_config.py` is a golden test that pins how the real
`monitors.json` keyword lists route — so a config edit that breaks tiering (or
re-introduces a substring collision) fails CI instead of your phone.

---

## File layout

```
notify-watcher/
├── .github/workflows/watch.yml      cron + run + commit state back
├── notify_watcher/
│   ├── main.py                      runs each topic, isolates failures
│   ├── ntfy.py                      shared push helper (env-driven)
│   ├── state.py                     load/save state.json
│   ├── watchlist.py                 reads watchlist.json titles/entries
│   ├── summarize.py                 shared one-line AI summary (Gemini→Claude)
│   ├── visa_math.py                 pure F4 wait-pace math (history → ETA)
│   └── topics/
│       ├── visa_bulletin.py         F4 Final Action + Dates for Filing
│       ├── wwdc.py                  Apple Newsroom RSS, WWDC items
│       ├── ios_release.py           Apple Developer Releases RSS, iOS/iPadOS
│       ├── deals.py                 JSON-LD price-drop watcher (watchlist + auto)
│       ├── soundcore_pro.py         sitemap discovery of new Liberty Pro products
│       ├── movies.py                TMDb release dates + DO streaming (watchlist)
│       ├── games.py                 RAWG release dates (watchlist, weekly)
│       ├── baseball.py              MLB team results + DR player milestones
│       └── habits.py                config-driven daytime habit nudges
├── watchlist.json                   movie/game titles + products you want tracked
├── habits.json                      recurring habit nudges (water, stand, eyes)
├── state.json                       dedup memory (committed by workflow)
├── requirements.txt
└── README.md
```

## Optional AI summaries

The WWDC and iOS-release topics can render a one-line AI summary as the
notification body. The provider plumbing lives in `notify_watcher/summarize.py`
(`one_line(system, user_text)`), which tries **Gemini** (free tier) first, then
**Anthropic**, and returns `None` so the caller falls back to a plain body when
no key is set or a call fails. Add either as a GitHub Actions secret to turn it
on:

| Secret name         | Value                                          |
| ------------------- | ---------------------------------------------- |
| `GEMINI_API_KEY`    | (optional) free Google AI Studio key           |
| `ANTHROPIC_API_KEY` | (optional) Claude API key (falls back here)    |

To give another topic an AI body, call `summarize.one_line(system, user_text)`
with a topic-specific system prompt — no provider code to copy.
