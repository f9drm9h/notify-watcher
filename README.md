# notify-watcher

Personal multi-topic monitor. Runs on a schedule in GitHub Actions, pushes
matches to your phone via [ntfy.sh](https://ntfy.sh). 100% free to run.

Topics:

- **F4 visa bulletin** — watches the F4 row, "All Chargeability Areas Except
  Those Listed" column, in BOTH State Dept family-sponsored tables (Final
  Action Dates and Dates for Filing), and alerts when either moves.
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
- **Movie release dates** — for each title in `watchlist.json` → `movies`,
  tracks its TMDb release date and alerts on first sight and on any date
  change. Needs `TMDB_API_KEY`.
- **Game release dates** — for each title in `watchlist.json` → `games`,
  tracks its RAWG release date and alerts the same way. Needs `RAWG_API_KEY`.

### Domain monitors (scored, configured in `monitors.json`)

Unlike the entity watchers above, these scan whole domains, so they run every
item through a deterministic scorer (provenance + keyword/structural signals,
**no AI in the scoring path**). Only **breakthrough/high**-tier items push live;
**moderate** items are collected into one **daily digest**, and **minor** items
are dropped — which is what keeps a high-volume news source from causing alert
fatigue. Sources, keywords, weights, and thresholds all live in `monitors.json`
(no secrets), so tuning is a config edit, not a code change.

- **FDA approvals** — reads the openFDA Drugs@FDA API (free, no key) and alerts
  on new drug/biologic (NDA/BLA) approvals. Generic (ANDA) approvals are
  filtered out as routine. One alert per application; supplements land in the
  digest.
- **Energy / electricity** — reads the RSS sources in `monitors.json` → `energy`
  (EIA Today in Energy, DOE/Energy.gov, World Nuclear News by default) and
  scores headlines against energy keywords (grid, battery, fusion, nuclear,
  solar, storage, …). No key.
- **Daily health tip** — one evidence-based tip each morning from a curated,
  vetted knowledge base (`data/health_tips.json`, sourced from CDC/WHO/
  MedlinePlus). With an AI key set the vetted tip is optionally *reworded* for
  variety; the fact is never AI-invented. Sent on the daily run only.

The daily digest and health tip fire on a once-daily workflow run (~08:05 in the
Dominican Republic, UTC−4); the collectors run on the normal every-3-hours
schedule.

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
| `TMDB_API_KEY` | (optional) free TMDb v3 API key, for the movie watcher |
| `RAWG_API_KEY` | (optional) free RAWG API key, for the game watcher |

The movie/game watchers only run if their key is set; without it they skip
quietly. Get the keys here (both free, ~2 min, no cost):

- **TMDb**: themoviedb.org → Settings → API → request a developer key →
  copy the **"API Key (v3 auth)"** value.
- **RAWG**: rawg.io/apidocs → "Get API Key" → sign up → copy the key.

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
│   └── topics/
│       ├── visa_bulletin.py         F4 Final Action + Dates for Filing
│       ├── wwdc.py                  Apple Newsroom RSS, WWDC items
│       ├── ios_release.py           Apple Developer Releases RSS, iOS/iPadOS
│       ├── deals.py                 JSON-LD price-drop watcher (watchlist + auto)
│       ├── soundcore_pro.py         sitemap discovery of new Liberty Pro products
│       ├── movies.py                TMDb release dates (watchlist)
│       └── games.py                 RAWG release dates (watchlist)
├── watchlist.json                   movie/game titles + products you want tracked
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
