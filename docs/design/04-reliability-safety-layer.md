# 04 — Reliability & Safety Layer

**Status: IMPLEMENTED, then AMENDED by the August 2026 reliability audit — read the Addendum at the end of this file before trusting §1–§7; several claims there (the heartbeat's query, the watchdog's one-alert-per-outage lifecycle, "always exits 0", "stays inside GitHub Actions") are now out of date.**

Goal: no silent failures. Every way this system can break should eventually
produce exactly one ntfy push saying what broke, or be blocked in CI before it
ever reaches a runner. The design adds two thin layers around the existing
watchdog instead of rebuilding it, stays inside GitHub Actions (no external
services, no new infrastructure), and changes no runtime behavior until the
final, optional phase.

---

## 1. What already exists (and is kept as-is)

The review found a real, working in-run health system. The boundary it draws is
the foundation of this design, so it is worth stating precisely:

- **`main.py` topic loop** — every topic runs in its own try/except and stamps
  `state["topic_health"][name]` with `last_ok` on success or
  `last_error`/`last_error_ts` on failure. The process **always exits 0**, so a
  topic failure never turns a workflow red.
- **`topics/watchdog.py`** — runs last in every cycle, reads `topic_health`,
  and pushes once per outage when a topic has had no successful run for
  `stale_hours` (monitors.json → watchdog, default 48 h). It bundles
  simultaneous outages, re-arms on recovery, and handles never-succeeded topics
  via `watchdog_failing_since`. This already covers dead feeds, moved URLs,
  revoked API keys, and any scraper that *raises*.
- **`topics/recap.py`** — folds `topic_health` into the Monday recap;
  **`dashboard.py`** renders the health panel.
- **Fail-soft config loaders** — `config.py`, `watchlist.py`, and the
  reminders/bills/habits topics all treat a missing file, malformed JSON, or a
  wrong-typed field as "nothing to do" so a typo never crashes a scheduled run.
- **State-push resilience** — shared `watch` concurrency group plus the
  rebase-and-retry push loop in watch.yml.

None of this is duplicated below. The new layers cover only what this
machinery *cannot* see.

## 2. Failure-mode matrix

| # | Failure | Today | Covered by |
|---|---------|-------|------------|
| 1 | Topic raises persistently (dead feed, moved URL, revoked key) | ✅ watchdog push after 48 h | existing watchdog |
| 2 | Topic raises transiently (network blip) | ✅ logged, retried next run | existing main loop |
| 3 | Run crashes before the topic loop (import error, broken dependency, pip failure) | ❌ red run nobody sees | **Layer 1: failure alerts** |
| 4 | State push fails after 5 retries | ❌ red run nobody sees | **Layer 1: failure alerts** |
| 5 | Schedules silently stop (GitHub drops crons, auto-disables workflows) | ❌ nothing runs, watchdog never executes | **Layer 1: heartbeat** |
| 6 | Config file has malformed JSON | ❌ loaders return `{}`/`[]`; every topic no-ops and stamps `last_ok` | **Layer 2: CI validation** |
| 7 | Config is structurally wrong (string `due_day`, bad date, missing `name`) | ❌ entry silently skipped forever | **Layer 2: CI validation** |
| 8 | Scraper gets HTTP 200 but parses zero items (site changed its HTML) | ❌ "successfully does nothing" forever | **Layer 3: data heartbeat** |
| 9 | Watchdog's own alert push fails | ❌ `watchdog_alerted` is stamped anyway; alert lost | **Layer 3: alert-retry fix** |
| 10 | ntfy itself is down | ⚠️ undetectable *through ntfy* | residual risk (§9) |
| 11 | Topic swallows its own fetch failure (log + `return state`) so main stamps `last_ok` anyway | ✅ reported as `source_failed`; watchdog alerts after 48 h | **topic health contract (`health.py`)** |

Rows 6–7 are the most likely in practice: the whole point of watchlist.json /
reminders.json / habits.json is that they are edited by hand on github.com, and
today a stray comma silently disables the edited feature while the dashboard
shows green.

Row 11 shipped after the original three layers: every direct scraper — fuel,
weather, quakes, onamet, outages, youtube, twitch, deals, groceries, fx —
deliberately swallows fetch errors so one dead source never kills the sweep,
which used to make "source down" indistinguishable from "ran fine". Topics in
`health.ADOPTED` now report `source_ok(data_count=N)` / `source_failed(msg)`
once per run (`notify_watcher/health.py`); main.py stamps `last_ok` only for a
true ok report, records soft failures into `topic_health` as
`last_error` + `source_failed` (the exact shape the existing watchdog
threshold already reads), keeps them sticky across gated no-claim runs, and an
ok report with items also stamps `last_data` — extending the Layer-3 data
heartbeat to the direct scrapers.

The story-engine topics — wikiquote, apod, and learn's Wikimedia featured feed
— joined `health.ADOPTED` after the gutenberg / library_of_congress retirement:
both of those topics had been silently dead for weeks (gutendex.com and loc.gov
403 GitHub's runner IPs) while their graceful per-run skip kept logging "ok".
Any topic that fetches external content on a timer now reports its source
outcome so that failure mode reaches the watchdog.

## 3. Architecture: three layers, crisp boundaries

```
            ┌──────────────────────────────────────────────────────┐
  before    │ Layer 2 — CI config validation (test.yml)            │
  merge     │ schemas + unit tests fail the push/PR that breaks    │
            │ watchlist/monitors/reminders/habits.json             │
            └──────────────────────────────────────────────────────┘
            ┌──────────────────────────────────────────────────────┐
  around    │ Layer 1 — workflow-level (alert.yml)                 │
  the run   │ a) workflow_run trigger: any failed run of watch/    │
            │    twitch/test → one high-priority ntfy push         │
            │ b) scheduled heartbeat: "has `watch` completed in    │
            │    the last 7 h?" → catches dead schedules           │
            └──────────────────────────────────────────────────────┘
            ┌──────────────────────────────────────────────────────┐
  inside    │ Layer 3 — in-run watchdog (existing + 2 extensions)  │
  the run   │ per-topic 48 h stale alert (unchanged) + opt-in      │
            │ "no data for N days" check + alert-retry fix         │
            └──────────────────────────────────────────────────────┘
```

**Why the layers cannot duplicate each other.** `main.py` always exits 0, so a
topic failure never reaches Layer 1, and a workflow crash never reaches the
topic loop, so Layer 3 never sees it. The boundary already exists in the code;
this design just puts an alarm on each side of it. The single overlap-looking
case — a syntax error in one topic module — fails the *import* in `main.py`
before the loop runs, so it is a whole-run crash and belongs to Layer 1, which
is correct: the watchdog can't run if the program can't start.

**Layers 1 + 2 together close the github.com-editing loop**: an edit to
watchlist.json that breaks the schema fails test.yml within a minute or two,
test.yml's failure fires alert.yml, and the phone gets "test failed on main —
your last change probably broke a config file" with a link to the run. CI
validation alone would be invisible to someone who never opens the Actions tab.

## 4. Recommended file structure

```
.github/workflows/alert.yml          NEW  Layer 1 (failure alerts + heartbeat)
schemas/
  watchlist.schema.json              NEW  Layer 2
  monitors.schema.json               NEW
  reminders.schema.json              NEW
  habits.schema.json                 NEW
tests/test_config_files.py           NEW  Layer 2 (picked up by unittest discover)
requirements-dev.txt                 NEW  `-r requirements.txt` + jsonschema
notify_watcher/monitor.py            EDIT Layer 3a: stamp last_data (one line)
notify_watcher/topics/watchdog.py    EDIT Layer 3a+3b: data check, retry fix
tests/test_watchdog.py               EDIT new cases for 3a/3b
.github/workflows/test.yml           EDIT install requirements-dev.txt instead
```

`jsonschema` goes in a new requirements-dev.txt rather than requirements.txt so
the runtime image (watch.yml) is unchanged —
validation is a CI concern; the runtime loaders stay deliberately fail-soft.

## 5. Layer 1 — workflow failure alerts (alert.yml)

A single new workflow with two jobs. Stateless by design: no committed state,
no cache, no external service — dedup and streak suppression come from asking
the GitHub API about the previous run.

```yaml
name: alert

# Reliability layer: push an ntfy notification when any monitored workflow
# fails, and a scheduled heartbeat that notices when `watch` stops running
# entirely (dropped/disabled schedule — the in-run watchdog can never see
# that, because it only executes inside a run).
#
# Adding a future workflow = add its `name:` to the list below (one line).

on:
  workflow_run:
    workflows: [watch, test]   # workflow `name:` fields, not filenames
    types: [completed]
  schedule:
    - cron: "30 5,11,17,23 * * *"      # heartbeat 4x/day, offset from :00 grid
  workflow_dispatch:
    inputs:
      test_alert:
        description: "Send a sample failure alert and exit (verifies delivery)"
        type: boolean
        default: false

permissions:
  actions: read   # to query previous runs of the failed workflow

jobs:
  on-failure:
    if: github.event_name == 'workflow_run' &&
        github.event.workflow_run.conclusion == 'failure'
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{ github.token }}
      RUN_NAME: ${{ github.event.workflow_run.name }}
      RUN_URL: ${{ github.event.workflow_run.html_url }}
      RUN_BRANCH: ${{ github.event.workflow_run.head_branch }}
      RUN_TS: ${{ github.event.workflow_run.updated_at }}
      WF_ID: ${{ github.event.workflow_run.workflow_id }}
      RUN_ID: ${{ github.event.workflow_run.id }}
    steps:
      - name: Alert only on the FIRST failure of a streak
        id: streak
        run: |
          # workflow_run fires once per completed run-attempt, so per-run dedup
          # is inherent. Streak suppression handles the other spam source: a
          # workflow that fails every cycle (watch runs 96x/day) must push on
          # the first failure only, then stay quiet until it recovers.
          prev=$(gh api "repos/${{ github.repository }}/actions/workflows/${WF_ID}/runs?branch=${RUN_BRANCH}&status=completed&per_page=5" \
            --jq "[.workflow_runs[] | select(.id != ${RUN_ID})][0].conclusion // \"none\"")
          echo "prev_conclusion=${prev}" >> "$GITHUB_OUTPUT"
      - name: Push failure alert to ntfy
        if: steps.streak.outputs.prev_conclusion != 'failure'
        run: |
          curl -fsS --retry 3 \
            -H "Title: Workflow failed: ${RUN_NAME} (${RUN_BRANCH})" \
            -H "Priority: high" -H "Tags: rotating_light" \
            -H "Click: ${RUN_URL}" \
            -d "The '${RUN_NAME}' workflow failed at ${RUN_TS}. The run that broke: ${RUN_URL}" \
            "${{ secrets.NTFY_SERVER || 'https://ntfy.sh' }}/${{ secrets.NTFY_TOPIC }}"

  on-recovery:
    # One calm push when a previously-failing workflow goes green again, so a
    # failure alert is never left dangling ("is it still broken?").
    if: github.event_name == 'workflow_run' &&
        github.event.workflow_run.conclusion == 'success'
    runs-on: ubuntu-latest
    steps:
      # same previous-run lookup; push (default priority, white_check_mark)
      # only when the previous conclusion was 'failure'.
      - run: echo "symmetric to on-failure; omitted here for brevity"

  heartbeat:
    if: github.event_name == 'schedule' || inputs.test_alert
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Check that `watch` has completed within the last 7 hours
        run: |
          # watch runs every 3 h; 7 h of silence means at least two consecutive
          # ticks vanished — that's a dead schedule, not normal GitHub jitter.
          last=$(gh api "repos/${{ github.repository }}/actions/workflows/watch.yml/runs?status=completed&per_page=1" \
            --jq '.workflow_runs[0].updated_at // empty')
          now=$(date -u +%s); then=$(date -u -d "${last:-1970-01-01}" +%s)
          age_h=$(( (now - then) / 3600 ))
          if [ "$age_h" -ge 7 ]; then
            curl -fsS --retry 3 \
              -H "Title: watch has not run in ${age_h}h" \
              -H "Priority: urgent" -H "Tags: skull" \
              -d "Last completed watch run: ${last:-never}. The schedule may have been dropped or disabled (GitHub disables crons after 60 days without repo activity). Check the Actions tab." \
              "${{ secrets.NTFY_SERVER || 'https://ntfy.sh' }}/${{ secrets.NTFY_TOPIC }}"
          fi
```

Design points:

- **Dedup** (requirement: no duplicates for the same failed run): `workflow_run`
  fires exactly once per completed run attempt, so the per-run guarantee is
  structural. The previous-run lookup adds *streak* suppression on top —
  a twitch outage produces one push at the start, not 96/day.
- **Recovery push** closes the loop for a reader who doesn't watch CI.
- **Heartbeat** is the only thing that can catch failure mode 5 ("no runs at
  all"), which neither `workflow_run` nor the in-run watchdog can ever see.
  Four checks a day is plenty for a 7-hour threshold and costs nothing.
- **No checkout, no pip** — the alert path must not depend on the repo's own
  dependencies being installable (that's failure mode 3, one of the things it
  reports on). Plain `curl` + the preinstalled `gh` CLI only.
- **Caveat:** `workflow_run` triggers only from the alert.yml on the *default
  branch*, so the PR adding it cannot test it. Verification path: merge, then
  `gh workflow run alert.yml -f test_alert=true` for delivery, and one
  deliberately-failing `test` push on a scratch branch for the real trigger.
- test.yml failures on PR branches alert too. That is intentional: branch
  pushes here are either Claude-driven PRs or github.com config edits, and both
  want a loud signal. The branch name in the title keeps it interpretable.

## 6. Layer 2 — schema validation for the four config files

Schema-based (JSON Schema draft 2020-12 via `jsonschema`), not bare
`json.load()`: a schema catches wrong types, missing required fields, bad enum
values, and out-of-range numbers, and `iter_errors` reports *every* problem
with a JSON path in one CI run — far friendlier for hand-editing than a stack
trace. Runtime behavior is untouched: the loaders stay fail-soft; CI becomes
fail-hard.

Strictness policy, derived from how each file is actually loaded:

- **watchlist / reminders / habits** — small, user-edited, stable shapes →
  strict: `additionalProperties: false`, required fields mirror what the topic
  code actually requires to act on an entry (`reminders.py` skips entries
  without `name`+`date`; `bills.py` without `name`+`due_day`; `habits.py`
  without `name`, `hours`, `messages`). A field the loader would silently
  ignore is exactly what validation exists to catch.
- **monitors.json** — large policy file that grows a section with nearly every
  new topic → validate *known* sections strictly but allow unknown top-level
  keys, so adding a topic doesn't force a schema edit in every PR. The
  high-blast-radius sections (`location`, `quiet_hours`, `priority`,
  `watchdog`, `scoring`) get full sub-schemas first; others can be tightened
  incrementally.
- `_comment`-style keys are allowed everywhere via `patternProperties: {"^_": {}}`
  — they are the project's documentation convention.

Example — `schemas/reminders.schema.json` (complete):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "reminders.json",
  "type": "object",
  "properties": {
    "reminders": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "date"],
        "properties": {
          "id": { "type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$" },
          "name": { "type": "string", "minLength": 1 },
          "date": { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
          "lead_days": { "type": "array", "items": { "type": "integer", "minimum": 0 } },
          "recurring": { "const": "yearly" },
          "note": { "type": "string" }
        },
        "additionalProperties": false
      }
    },
    "bills": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "name", "due_day"],
        "properties": {
          "id": { "type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$" },
          "name": { "type": "string", "minLength": 1 },
          "due_day": { "type": "integer", "minimum": 1, "maximum": 31 },
          "lead_days": { "type": "array", "items": { "type": "integer", "minimum": 0 } },
          "note": { "type": "string" }
        },
        "additionalProperties": false
      }
    }
  },
  "patternProperties": { "^_": {} },
  "additionalProperties": false
}
```

Example — `schemas/habits.schema.json` core (per-habit item):

```json
{
  "type": "object",
  "required": ["name", "title", "hours", "messages"],
  "properties": {
    "name": { "type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$" },
    "title": { "type": "string", "minLength": 1 },
    "tag": { "type": "string" },
    "enabled": { "type": "boolean" },
    "hours": { "type": "array", "minItems": 1,
               "items": { "type": "integer", "minimum": 0, "maximum": 23 } },
    "messages": { "type": "array", "minItems": 1,
                  "items": { "type": "string", "minLength": 1 } }
  },
  "additionalProperties": false
}
```

watchlist.schema.json mirrors the documented shape in `watchlist.py` (arrays of
strings for `movies`/`games`; `products` items require `name`+`url`, allow
optional `target_price` number and `group` string). monitors.schema.json
example excerpt for the strict sections:

```json
{
  "quiet_hours": {
    "type": "object",
    "properties": {
      "enabled": { "type": "boolean" },
      "defer_to_digest": { "type": "boolean" },
      "start": { "type": "string", "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$" },
      "end":   { "type": "string", "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$" },
      "utc_offset_hours": { "type": "number" }
    },
    "patternProperties": { "^_": {} },
    "additionalProperties": false
  },
  "watchdog": {
    "type": "object",
    "properties": {
      "stale_hours": { "type": "number", "exclusiveMinimum": 0 },
      "data_stale_days": {
        "type": "object",
        "additionalProperties": { "type": "number", "exclusiveMinimum": 0 }
      }
    },
    "patternProperties": { "^_": {} },
    "additionalProperties": false
  }
}
```

The exact required/optional split for every field must be re-derived from the
loader code at implementation time (as done above for reminders/bills/habits),
with the unit tests pinning the result — the schema documents the loader, never
the other way around.

## 7. Layer 2 — example unit tests (`tests/test_config_files.py`)

Runs inside the existing `unittest discover` step of test.yml, so **no workflow
restructuring is needed** — the requirement "validation runs inside test.yml"
is satisfied by discovery. Semantic checks that JSON Schema cannot express
(real calendar dates, cross-field relations, uniqueness) live in the same file.

```python
"""CI gate: the live config files must parse and satisfy their schemas.

Runtime stays fail-soft (a typo never crashes a scheduled run); this test makes
CI fail-hard instead, so the typo never reaches a runner. Schema errors are
reported all at once with JSON paths, because these files are edited by hand
on github.com.
"""
from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
CONFIG_NAMES = ("watchlist", "monitors", "reminders", "habits")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ConfigFilesTest(unittest.TestCase):
    def test_json_syntax(self):
        for name in CONFIG_NAMES:
            with self.subTest(file=f"{name}.json"):
                _load(ROOT / f"{name}.json")  # raises with line/column on bad JSON

    def test_schemas_are_valid_schemas(self):
        for name in CONFIG_NAMES:
            with self.subTest(schema=f"{name}.schema.json"):
                Draft202012Validator.check_schema(_load(SCHEMAS / f"{name}.schema.json"))

    def test_configs_match_schemas(self):
        for name in CONFIG_NAMES:
            with self.subTest(file=f"{name}.json"):
                validator = Draft202012Validator(_load(SCHEMAS / f"{name}.schema.json"))
                errors = [f"  {e.json_path}: {e.message}"
                          for e in sorted(validator.iter_errors(_load(ROOT / f"{name}.json")),
                                          key=lambda e: e.json_path)]
                self.assertFalse(errors,
                                 f"{name}.json failed validation:\n" + "\n".join(errors))

    def test_reminder_dates_are_real_dates(self):
        # The YYYY-MM-DD pattern admits 2026-02-30; only date parsing rejects it.
        for r in _load(ROOT / "reminders.json").get("reminders", []):
            with self.subTest(reminder=r.get("id") or r.get("name")):
                dt.date.fromisoformat(r["date"])

    def test_ids_are_unique(self):
        rem = _load(ROOT / "reminders.json")
        habits = _load(ROOT / "habits.json").get("habits", [])
        for label, ids in (
            ("reminder id", [r["id"] for r in rem.get("reminders", []) if "id" in r]),
            ("bill id", [b["id"] for b in rem.get("bills", [])]),
            ("habit name", [h["name"] for h in habits]),
        ):
            with self.subTest(field=label):
                dupes = {i for i in ids if ids.count(i) > 1}
                self.assertFalse(dupes, f"duplicate {label}(s): {sorted(dupes)}")
```

## 8. Layer 3 — two small watchdog extensions (existing system review)

The watchdog's coverage is genuinely good; the review recommends **keeping its
architecture untouched** and closing two specific holes.

**(a) Data heartbeat — catches scrapers that "succeed" at finding nothing.**
A site that changes its HTML usually still returns HTTP 200; the parser finds
zero items, the topic returns normally, `last_ok` is stamped, and the watchdog
sees a healthy topic forever (failure mode 8 — this is how the Bravo/Nacional
scrapers or the EDEESTE PDF parser would die in practice). Fix at the single
choke point: `monitor.run_source` already receives every collector's parsed
items and its `topic` name, so one stamp covers every collector-based topic:

```python
# monitor.run_source, after items are normalized:
if items and topic:
    state.setdefault("topic_health", {}).setdefault(topic, {})[
        "last_data"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
```

The watchdog then gets one extra, **opt-in** rule: for topics listed in
monitors.json → `watchdog.data_stale_days` (e.g. `{"fda": 21, "energy": 10,
"groceries": 10}`), alert once — same alerted/re-arm pattern as today — when
`last_data` is older than the configured window. Opt-in is essential: many
sources have legitimate long quiet spells, and only the per-topic config knows
the difference between "quiet" and "broken". Unconfigured topics behave exactly
as today.

**(b) Alert-retry fix — the watchdog must not mark itself "done" if its own
push failed.** Today `_evaluate` records `al[name] = now` *before*
`events.emit` is called; if the push raises (ntfy hiccup), main.py still saves
the mutated state, so the alert is permanently swallowed (failure mode 9). Fix:
persist `watchdog_alerted` only after `emit` returns — a one-block reorder in
`run()`, with a unit test that emits through a push stub that raises and
asserts the topic re-alerts on the next run.

**Coverage map after (a)+(b)** — how each requested detection target is handled:

| Target | Layer | Mechanism |
|--------|-------|-----------|
| Broken scrapers | 3 | raises → 48 h watchdog; silent-empty → data heartbeat |
| Dead feeds | 3 | raises/timeouts → 48 h watchdog |
| Invalid API responses | 3 | parse error raises → watchdog; parses-but-empty → data heartbeat |
| Dependency failures | 1 | import/pip failure kills the run before the loop → failure alert |
| Workflow execution failures | 1 | `workflow_run` alert; "no runs at all" → heartbeat |
| Config mistakes | 2 | schema + semantic tests fail CI; failed CI → Layer 1 push |

## 9. Residual risks (accepted, documented)

- **ntfy itself down**: every alarm in all three layers is delivered through
  ntfy, so a transport outage is undetectable through the transport. Backstop:
  GitHub's own e-mail notification on failed runs still exists. A second
  delivery channel (e-mail step in alert.yml) is possible later but is new
  surface area — out of scope by the minimal-infrastructure philosophy.
- **alert.yml's own schedule dying**: the watcher of the watcher has no
  watcher. Solving this requires something outside GitHub (healthchecks.io
  etc.) — deliberately rejected for now; the failure window is bounded because
  the runner's constant state commits keep the repo "active" for GitHub's
  60-day cron-disable rule.
- **GitHub Actions platform outage**: nothing runs and nothing alerts; rare,
  externally visible, self-resolving.

## 10. Migration plan (each phase independent, reversible, behavior-preserving)

**Phase 1 — validation first (PR).** Add `schemas/`, `tests/test_config_files.py`,
`requirements-dev.txt`; point test.yml's install at requirements-dev.txt. If
any live config fails its new schema, fix file or schema in the same PR so it
merges green. *Runtime change: none — watch.yml and all loaders are
untouched.* Rollback: delete the new files. Validation goes first because it
protects every config edit made during the later phases.

**Phase 2 — alert.yml (PR, then on-main verification).** Pure addition; no
existing file changes. Because `workflow_run` only fires from the default
branch, verify after merge: `gh workflow run alert.yml -f test_alert=true`
(delivery test), then push a one-line breaking change to a scratch branch to
make `test` fail once and confirm the failure + recovery pushes and streak
suppression. *Runtime change: none; new pushes occur only when a workflow
fails.* Rollback: delete alert.yml.

**Phase 3 — watchdog extensions (PR).** The `last_data` stamp (inert by
itself), the opt-in `data_stale_days` config starting with 2–3 scraper-backed
topics, and the alert-retry reorder, each with tests following the existing
pure-`_evaluate` test style. *Default behavior identical: with no
`data_stale_days` section configured, the only observable change is the retry
fix — strictly fewer lost alerts.* Rollback: revert the PR; the stamp leaves
harmless extra keys in `topic_health`.

No phase migrates or rewrites state.json; all new state lives in existing
`topic_health` entries. Total new runtime dependencies: zero. Total new CI
dependency: `jsonschema`.


---

## Addendum — August 2026 reliability audit

*Status: implemented. This section amends §1–§7 above; where they disagree, this
section is current.*

The design above assumed the monitoring layer worked and needed extending. An
audit of the running system found that several of its own components were
failing silently. Four fixes, one per finding, plus one structural gap that
could not be closed from inside GitHub.

### A1. The heartbeat measured the wrong thing

`alert.yml`'s heartbeat asked GitHub for `status=completed` runs. GitHub counts
a **failed** run as completed, so a `watch` that failed on every 15-minute tick
refreshed the heartbeat's freshness stamp on every failure. The monitor watched
the schedule's pulse, not the patient's: continuous total failure was
indistinguishable from perfect health.

Measured on this repo at audit time: 2397 completed `watch` runs, 1948
successful ones. 449 failures the heartbeat read as evidence of life.

**Is `status=completed` → `status=success` sufficient?** It is necessary and it
is correct — the API accepts `success` as a filter and returns the newest
successful run — but on its own it is *not* sufficient, for three reasons:

1. **The heartbeat runs on the thing it monitors.** It is a `schedule:` trigger
   inside GitHub Actions. The failures it exists to catch — a dropped cron, a
   workflow disabled after 60 days of inactivity, an account-level lockout —
   stop the heartbeat by the same mechanism. A monitor that dies of the disease
   it screens for is not a monitor. Closed by A4.
2. **The lookup could kill its own alert.** `run:` steps use `bash -e`, so a
   failed `gh api` call aborted the step before the notification. Every gating
   lookup now fails *open*: an unusable answer alerts.
3. **A green run is not a working run.** `main.py` exits 0 by design, so `watch`
   can succeed while every topic inside it fails. That is the in-run watchdog's
   job (A2), not the heartbeat's, and the two together are what make "no news is
   good news" true.

### A2. The watchdog told you once and then went quiet

The original lifecycle was: alert once after `stale_hours` (48), then silence
until recovery, with no recovery notice. Three consequences, all bad:

- **Silence carried no information.** A two-day-old ongoing outage and a
  resolved one produced identical output: nothing.
- **48 hours of guaranteed blindness** before the first word about a dead feed.
- **No close-out.** A red alert was never answered by a green one.

Found live in `state.json` during the audit: `visa_bulletin` had been failing
since 2026-07-14, was alerted once on 2026-07-16, and had said nothing in the 17
days since.

Replaced with a three-phase lifecycle — first alert (`alert_delay_hours`,
default 0), reminders every `reminder_hours` (default 12), exactly one recovery
notice — with every topic crossing the same transition on one run bundled into a
single push. Anti-spam comes from bundling and from the reminder interval, not
from staying quiet.

The delivery contract from Phase 3 is preserved and strengthened: each bundle's
state markers are written only after *that bundle's* push returns, so the three
phases fail independently. Losing the recovery notice cannot roll back the
outage alert and make it fire twice.

### A3. Alerts that could not be sent, and nothing watching the sender

- `on-failure` only matched `conclusion == 'failure'`, so a run killed by the
  runner cap (`timed_out`) or a workflow whose YAML never parsed
  (`startup_failure`) was silent. `cancelled` stays excluded: `watch`'s
  concurrency group cancels superseded queued runs as normal operation.
- The streak-suppression lookup could fail the job before the push. It exists
  only to *suppress* a duplicate, so it must never be able to prevent an alert.
- **`alert.yml` was unmonitored.** It had failed 468 times — more often than
  `watch` itself (449) — and never reported it once. It is not in its own
  `workflow_run` list, and adding it there risks a self-triggering loop, so the
  scheduled heartbeat job now audits alert.yml's own recent conclusions.
- `watch` had no `timeout-minutes`, so GitHub's 6-hour default applied. One
  wedged HTTP call would hold the `watch` concurrency group for a third of a day
  while the run showed as "in progress" — no failure, no alert, nothing. Capped
  at 20 minutes (~30× the typical 35s run), which turns a hang into a
  `timed_out` conclusion that now alerts.

### A4. The blind spot that cannot be closed from inside GitHub

§1 states this design "stays inside GitHub Actions (no external services, no new
infrastructure)". The audit found the cost of that constraint, and it is not
acceptable.

In July 2026 the account hit a billing/spending limit. Every job was refused
before its first step. `watch` failed 449 times; all 468 `alert` runs meant to
report those failures were themselves refused. **Zero notifications were
delivered, for days.** No amount of care inside `alert.yml` can fix this: a
monitor hosted on the failing platform cannot report that the platform failed.

The Cloudflare Worker (`worker/src/index.js`) already fires every 15 minutes to
dispatch the `watch` cadence, and is the only part of the system hosted
elsewhere. Its `scheduled()` handler now also runs the same "when did `watch`
last SUCCEED" check and posts straight to Discord when the answer is stale or
GitHub will not answer at all. It is read-only on GitHub, costs one extra API
call per hour, and never throws — a broken heartbeat must not be able to break
the cadence that keeps the whole watcher running.

Deliberate limits, so this stays a safety net and not a second system:

- **No recovery notice.** Announcing recovery needs memory of the outage, and
  Workers have no durable storage here. Once GitHub is back, `alert.yml`'s own
  `on-recovery` sends one.
- **Reminders by pacing, not bookkeeping.** The check runs only on the first
  cron tick of each hour (`:07`), which caps it at one message per hour for as
  long as the outage lasts.
- **Requires `wrangler secret put CHANNEL_LOGS`.** Without it the check logs a
  line and no-ops, so deploying before setting the secret degrades to the old
  behavior rather than erroring every 15 minutes.

### A5. Configuration that silently disabled everything

`config.load()` returns `{}` on unparseable JSON so a typo cannot crash a
scheduled run. Correct — but every topic then sees "nothing configured" and
no-ops. Runs stay green, no topic records an error, and the watchdog has nothing
to find. `monitors.json` is edited directly on github.com by design, so one
stray comma could silence the entire system indefinitely. `config.last_error()`
now reports why a load came back empty, and `main.py` pushes that before the
topic loop. The fail-soft behavior is unchanged; it is just no longer silent.

### A6. The one deliberate break with §1's exit-0 rule

§1 states the process "**always exits 0**". It now has exactly one exception:
`main.py` exits non-zero when the **watchdog topic itself** failed on that run.

Every other topic has a reporter — the watchdog. The watchdog has none, because
it skips its own health entry precisely so it cannot try to alert about being
broken while it is broken. A watchdog that dies takes the whole monitoring layer
with it, silently. Exiting non-zero escalates that one case to `alert.yml`,
which runs in a separate process with its own Discord path. Ordinary per-topic
failures still exit 0, and the escalation is keyed on the current run's
timestamp so a stale error cannot pin every future run red.

### What is still not covered

- **Discord itself being down.** Every alert path in the system, including both
  heartbeats, delivers through Discord. If Discord is unreachable nothing gets
  through; the watchdog's at-least-once contract means the alerts are re-sent
  once it returns, but the notification is late, not lost.
- **The Cloudflare Worker being down.** It is now the only off-GitHub monitor,
  and nothing monitors it. A dead Worker also stops the `watch` cadence, so
  alert.yml's heartbeat notices within 7 hours — the two cover each other, which
  is the best that two components can do without a third.
- **Everything failing at once.** Bundled outage alerts name every affected
  topic, but a total outage still arrives as one message; it does not escalate
  differently from a five-topic one.
