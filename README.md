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
- **Grocery deals (La Sirena / Nacional / Bravo)** — watches each chain's
  weekly promotions (`monitors.json` → `groceries`, no key). La Sirena's
  "Especiales del día" collection is read via its store API (sale vs. list
  price gives the discount); Nacional's `/ofertas` page is scraped for items
  showing a real price cut. A discount of `big_discount_pct` or more (30% by
  default) is a **significant deal and pushes**; smaller cuts buffer into the
  daily digest. One alert per (store, product, price) — a *deeper* cut on the
  same product alerts again — and each store seeds its current offers silently
  on first run. Bravo's site publishes no prices at all (their specials are
  image flyers), so for Bravo you get a digest heads-up when a new promo
  campaign page appears, with the link.
- **ITSC academic calendar** — scrapes the academic-calendar page on
  itsc.edu.do for the newest "Calendario académico" PDF (`monitors.json` →
  `itsc`, no key) and pushes a heads-up **7 days and 1 day before** every
  dated item: registration windows and document-reception periods alert at
  both their start and their end (the deadline), exams and semester start/end
  the same way, one-day items once. A freshly posted term's PDF takes over
  automatically; each (activity, date, lead) alerts exactly once and the
  first run seeds silently.
- **Movie release dates + streaming + news + countdowns** — for each title in
  `watchlist.json` → `movies`, tracks its TMDb release date: a date CHANGE
  (delay, moved up, or a date landing on a TBA film) pushes live, first sight
  goes to the digest (needs `TMDB_API_KEY`). Also watches TMDb's
  watch-provider data and pushes once when a film becomes streamable on a
  subscription service in the DR (e.g. "🎬 The Odyssey is now streaming on
  Netflix in DO") — rent/buy listings don't count, and the first run seeds
  silently so an already-streaming title stays quiet. Each title's Google
  News headlines are scored via `movies_scoring`, tuned so only high-signal
  events push live: casting announcements, release-date moves, cancellations,
  and real trailer/teaser drops push from any source; a leak pushes only when
  confirmation language meets a trusted outlet; generic coverage, rumor
  pieces, box office, reviews and awards chatter go to the daily digest or
  are dropped. Every Monday one **countdown push** lists each watchlist film
  with a confirmed release date in the next 60 days ("Avengers: Doomsday
  releases in 18 days"); TBA films are skipped.
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
- **Golden Sun community tracker** — merges three sources into the same scored
  news engine (`monitors.json` → `golden_sun` + `golden_sun_scoring`, no key):
  the Golden Sun Universe wiki's news posts (read from goldensunwiki.net — the
  old goldensununiverse.net domain is dead), the week's top r/GoldenSun posts
  (the score-bearing JSON endpoint filtered to >50 upvotes when Reddit allows
  it, else the top-of-week RSS as fallback), and a Google News "Golden Sun"
  search. Remaster/Switch Online listings, official Nintendo/Camelot mentions,
  and major ROM-hack releases push **live**; popular community posts and press
  chatter buffer to the **daily digest**; memes, listicles, and speculation are
  **dropped**. First run seeds silently.
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
- **ONAMET severe-weather alerts** — the Dominican meteorological office's own
  watches and warnings (vaguadas, ondas tropicales, flood alerts, …) via its
  official Common Alerting Protocol feed (`monitors.json` → `onamet`, no key;
  ONAMET was renamed INDOMET, same office). Every new alert pushes live
  immediately — never buffered to the digest: an **AVISO** (the highest level)
  rides urgent, an **ALERTA** high. The body carries the event, the office's
  description, and the affected provinces. The feed re-posts the same alert
  while forecasters revise it, so re-issuances of identical text are suppressed
  until the alert expires; the first run seeds silently.
- **Electricity outage alerts (EDEESTE)** — reads EDEESTE's weekly scheduled-
  maintenance PDF (`monitors.json` → `outages`, no key) and pushes when any
  watched zone (`outages.edeeste.zones`, e.g. Hainamosa) appears in a day's
  program — **the day before** the cut, or the day of, when EDEESTE publishes
  it late. The push carries the date, the time window when the PDF shows one,
  and a link to the weekly schedule. One alert per outage day per zone; the
  first run seeds the current week silently. (EDESUR's page scrape — the
  capital's south/west — is still built in: list provinces in
  `outages.regions` to turn it on.)
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
- **Fuel prices (DR weekly)** — reads the MICM's official "Aviso Semanal de
  Precios de Combustibles" PDF (`monitors.json` → `fuel`, no key; the MICM
  inherited this duty from the now-defunct CNPE) and reports the consumer
  fuels' RD$/gal prices — Gasolina Premium/Regular, Gasoil Regular/Óptimo,
  Kerosene, GLP — each week a new notice appears. Ordinary weeks land one
  line per fuel in the morning digest ("Gasolina Premium: RD$339.80 (-4.70,
  -1.4%)"); any fuel moving by `fuel.push_pct` (default 5%) or more
  week-over-week pushes live instead, linking to the official PDF. The first
  run seeds silently.
- **Reminders / expiry** — a tiny date engine over `reminders.json` (no network):
  document/visa/ID expiry, subscription renewals, warranties, yearly birthdays.
  Fires once at each configured lead time (default 90/30/7/1/0 days before).
  Daily run only.
- **Weekly spending summary (BHD)** — polls Gmail over IMAP every run for
  "BHD Notificación de Transacciones" alert emails from the last 7 days
  (read or unread — reading your own alerts can't hide them from the bot; the
  mailbox is opened read-only and never modified), parses the approved
  transactions out of the HTML table (date, amount, currency, merchant, type),
  and logs them to `data/spending.json.enc` (deduped on date + amount +
  merchant, so re-scanning the same emails is harmless). Every Monday
  it pushes last week's picture: total spent in DOP, top merchants, the biggest
  single expense, and a week-over-week comparison. Silent until transactions
  exist. Requires the `GMAIL_USER` + `GMAIL_APP_PASSWORD` secrets (a Gmail
  [app password](https://myaccount.google.com/apppasswords), not your real
  password); without them the topic skips quietly. A Claude-style Gmail MCP
  connector cannot be used here — MCP servers authenticate interactive
  assistant sessions, not headless CI runners — so IMAP is the supported path.
  **PRIVACY: the committed log is encrypted (Fernet, `SPENDING_KEY` secret) so
  the repo can stay public without exposing purchase history. Plaintext is
  never written; read the log locally with `python tools/show_spending.py`
  (key in `.secrets/spending.key`, gitignored). If the key is missing or
  wrong, the topic skips and leaves the bank emails unread — it never
  overwrites the existing log.**
- **Bill reminders (DR utilities)** — monthly due-date nudges from
  `reminders.json` → `bills` (no network): EDEESTE electricity, CAASD water,
  internet/cable. Each entry names the bill and its `due_day` (day of the
  month; day 31 clamps to a short month's last day) and pushes **5 days and
  1 day before** it is due (`lead_days` configurable per bill). Edit the due
  days on github.com to match your actual bills; no code change needed.
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
- **Knowledge deep-dives** — one titled, 2–3-sentence story from a curated KB
  (`data/knowledge.json`: 100 entries across ten themes — early human
  breakthroughs, scientific discoveries, astronomy & space, medical
  breakthroughs, technological revolutions, ancient civilizations, Greek &
  Roman mythology, philosophy, mathematics history, world-history turning
  points) on **every 3-hour run**, independent of the daily gate. The pick has
  memory: shown entry ids are stamped into `state.json` so nothing repeats
  within 30 days, themes rotate cyclically so consecutive pushes never cluster
  on one topic, and the pick is seeded by the current 3-hour window, so a
  re-run inside a window never drifts or double-sends. When every entry has
  been shown within the window (at ~8 pushes/day a 100-entry KB cycles in
  ~12–13 days), the least-recently-shown entry is reused — grow the KB by
  appending to `data/knowledge.json` to lengthen the cycle. The entry's title
  is the push header; the body goes out verbatim (never LLM-reworded). No
  network, no key.
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
| `NASA_API_KEY` | (optional) free api.nasa.gov key for the APOD picture; without it the shared `DEMO_KEY` quota is used |
| `GMAIL_USER` | (optional) Gmail address for the BHD spending tracker |
| `GMAIL_APP_PASSWORD` | (optional) a Gmail **app password** (myaccount.google.com/apppasswords; requires 2-Step Verification) for the spending tracker |
| `SPENDING_KEY` | (required with the Gmail secrets) Fernet key that encrypts the committed spending log (`data/spending.json.enc`); generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and keep a copy in `.secrets/spending.key` to read the log locally |

The movie/game watchers only run if their key is set; without it they skip
quietly. Get the keys here (both free, ~2 min, no cost):

- **TMDb**: themoviedb.org → Settings → API → request a developer key →
  copy the **"API Key (v3 auth)"** value.
- **RAWG**: rawg.io/apidocs → "Get API Key" → sign up → copy the key.

(The AI-summary keys, `GEMINI_API_KEY` / `ANTHROPIC_API_KEY`, are also
optional — see "Optional AI summaries" at the bottom.)

Besides the secrets, the workflows steer a run with three plain env toggles
you normally never set by hand: `NOTIFY_DAILY=1` marks the once-a-day run
(daily-only topics like the digest flush, learn, and groceries gate on it;
main.py also sets it automatically on the first run past 12:00 UTC),
`NOTIFY_ONLY=<topic,topic>` restricts a run to named topics (how the
15-minute Twitch-only mode inside `watch.yml` stays lightweight), and `NOTIFY_TEST_PUSH=1`
sends a single delivery-test notification and exits (the `test_push=true`
manual dispatch input).

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
| Daily digest | **Mute movies 24h** / **Mute games 24h** | silences that topic for 24 h: its live pushes are deferred into the next morning digest and its digest chatter is dropped; `critical` alerts still ring through |

Commands are deliberately low-stakes: the worst anyone who learns the control
topic name could do is skip a nudge, snooze a reminder, or mute a topic for a
bounded while (≤ 30 days, deferred into the digest rather than lost, and
`critical` alerts can never be muted). Nothing reads data, edits config, or
executes code; see `docs/design/reply-buttons.md` for the full design and
threat model.

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

After this, the workflow runs by itself every 15 minutes for Twitch and does
the full sweep once per 3-hour window.

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
├── .github/workflows/
│   ├── watch.yml                    15-min scheduler: Twitch every run, full sweep once per 3-hour window
│   └── test.yml                     CI: full test suite on every push/PR
├── notify_watcher/                  — engine —
│   ├── main.py                      runs each topic, isolates failures
│   ├── events.py                    Event normalizer: every topic emits through here
│   ├── priority.py                  Personal Priority Engine (pure, cross-topic scorer)
│   ├── digest.py                    daily digest buffer (rank, evict, flush)
│   ├── eventlog.py                  capped history of every routed event
│   ├── dashboard.py                 renders state into docs/dashboard/index.html
│   ├── changes.py                   reusable before/after diffs ("A → B, +N%")
│   ├── news.py                      shared scoring/routing for per-title news
│   ├── monitor.py                   shared collector engine (FDA, energy, …)
│   ├── scoring.py                   deterministic keyword importance scorer
│   ├── control.py                   reply-button command channel (ntfy poll)
│   ├── ntfy.py                      push transport + quiet-hours suppression
│   ├── config.py                    loads monitors.json sections
│   ├── state.py                     load/save state.json
│   ├── watchlist.py                 reads watchlist.json titles/entries
│   ├── ids.py                       short stable hashes for dedup seen-lists
│   ├── kb.py                        curated fact channels + day-of-year pick
│   ├── summarize.py                 shared one-line AI summary (Gemini→Claude)
│   ├── visa_math.py                 pure F4 wait-pace math (history → ETA)
│   └── topics/                      — one module per alert topic —
│       ├── air_quality.py           AQI threshold alerts (Open-Meteo)
│       ├── anthropic_news.py        official Anthropic announcements
│       ├── apod.py                  NASA Astronomy Picture of the Day
│       ├── astronomy.py             moons/meteor showers/eclipses almanac
│       ├── baseball.py              MLB team results + DR player milestones
│       ├── beach_day.py             weekend beach-day 0-10 index
│       ├── bills.py                 monthly utility-bill due-date reminders
│       ├── blood_donation.py        donation-eligibility reminder
│       ├── deals.py                 JSON-LD price-drop watcher (watchlist + auto)
│       ├── digest_topic.py          flushes the daily digest
│       ├── energy.py                energy/electricity news monitor
│       ├── energy_learn.py          daily "Today's spark" learning push
│       ├── fda.py                   new FDA drug approvals (openFDA)
│       ├── fuel.py                  DR weekly fuel prices (MICM notice PDF)
│       ├── fx.py                    USD→DOP rate thresholds + weekly trend
│       ├── games.py                 RAWG release dates + scored game news
│       ├── golden_sun.py            Golden Sun wiki/reddit/news tracker
│       ├── groceries.py             La Sirena/Nacional/Bravo weekly deals
│       ├── habits.py                config-driven daytime habit nudges
│       ├── health_tip.py            one evidence-based health tip per day
│       ├── holidays.py              DR public-holiday heads-up (Nager.Date)
│       ├── ios_release.py           Apple Developer Releases RSS, iOS/iPadOS
│       ├── iss.py                   visible ISS passes over your location
│       ├── itsc.py                  ITSC academic-calendar deadline heads-ups
│       ├── launches.py              imminent rocket launches (Launch Library)
│       ├── learn.py                 consolidated daily learning push
│       ├── marine.py                rough-seas heads-up (Open-Meteo Marine)
│       ├── movies.py                TMDb release dates + DO streaming (watchlist)
│       ├── music.py                 followed-artist releases + discovery pick
│       ├── onamet.py                official DR severe-weather alerts (CAP feed)
│       ├── outages.py               EDEESTE scheduled power cuts (weekly PDF)
│       ├── quakes.py                nearby earthquakes, geo-routed (USGS)
│       ├── recap.py                 Monday "your week in notifications" summary
│       ├── reminders.py             reminders.json expiry/deadline alerts
│       ├── soundcore_pro.py         sitemap discovery of new Liberty Pro products
│       ├── spending.py              BHD email spending log + weekly summary
│       ├── twitch.py                followed-streamer live alerts (decapi)
│       ├── uv.py                    high-UV heads-up (Open-Meteo)
│       ├── visa_bulletin.py         F4 Final Action + Dates for Filing
│       ├── watchdog.py              self-monitoring over topic_health
│       ├── weather.py               NHC hurricane/tropical-storm alerts
│       ├── wwdc.py                  Apple Newsroom RSS, WWDC items
│       └── youtube.py               new uploads from followed channels
├── data/                            curated KB content (learn/energy_learn/…)
├── docs/
│   ├── dashboard/index.html         self-contained static dashboard (view locally)
│   └── design/                      design notes for the bigger frameworks
├── tools/scan_music.py              one-off iTunes-library scanner (seeds music)
├── monitors.json                    all topic config + priority rules (no secrets)
├── watchlist.json                   movie/game titles + products you want tracked
├── reminders.json                   personal expiry/deadline reminders
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
