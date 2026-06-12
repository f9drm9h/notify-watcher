# 04 — Reliability & Safety Layer

**Status: IMPLEMENTED — all three phases (config validation, alert.yml, watchdog extensions) are live**

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
  rebase-and-retry push loop in watch.yml / twitch.yml.

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

Rows 6–7 are the most likely in practice: the whole point of watchlist.json /
reminders.json / habits.json is that they are edited by hand on github.com, and
today a stray comma silently disables the edited feature while the dashboard
shows green.

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
the runtime image (watch.yml / twitch.yml) is byte-for-byte unchanged —
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
    workflows: [watch, twitch, test]   # workflow `name:` fields, not filenames
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
          # workflow that fails every cycle (twitch runs 96x/day) must push on
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
merges green. *Runtime change: none — watch.yml/twitch.yml and all loaders are
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
