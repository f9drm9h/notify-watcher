# notify-watcher

Personal multi-topic monitor. Runs on a schedule in GitHub Actions, pushes
matches to your phone via [ntfy.sh](https://ntfy.sh). 100% free to run.

Two topics:

- **F4 visa bulletin** — watches the F4 row, "All Chargeability Areas Except
  Those Listed" column, in the State Dept "Dates for Filing Family-Sponsored
  Visa Applications" table (section B), and alerts when it moves.
- **WWDC announcements** — watches Apple Newsroom RSS for any post whose
  title contains "WWDC", and alerts on each new one (headline + link).

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

| Secret name   | Value                                              |
| ------------- | -------------------------------------------------- |
| `NTFY_TOPIC`  | the topic name from step 1                         |
| `NTFY_SERVER` | (optional) leave unset to use the default `https://ntfy.sh` |

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
│   └── topics/
│       ├── visa_bulletin.py         F4 All-Other Dates for Filing
│       └── wwdc.py                  Apple Newsroom RSS, WWDC items
├── state.json                       dedup memory (committed by workflow)
├── requirements.txt
└── README.md
```

## Adding a future AI summary to WWDC

`notify_watcher/topics/wwdc.py` isolates the notification body inside
`build_notification(entry)`. Swap the `body = ...` line for an
AI-generated summary there — the rest of the module (fetch, dedup, push)
does not need to change. A `TODO(ai-summary)` comment marks the spot.
