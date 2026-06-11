# Design — Reply buttons: two-way control (backlog #9, FLAGSHIP)

**Status:** 📝 proposed — design only, no implementation yet.
**Composes with:** `notify_watcher/ntfy.py` (transport), `events.emit` (routing funnel),
`topics/habits.py` (DONE), `topics/reminders.py` (SNOOZE), `digest.py` /
`topics/digest_topic.py` (MUTE), `state.py` (persistence), both workflows
(`watch.yml`, `twitch.yml`).

## Problem

Every notification today is one-way: the system talks, you listen. The three
moments where you most want to talk *back* are:

| Push | What you want to say | Today's workaround |
|---|---|---|
| "Drink water" nudge (`habits`) | "Done — skip the next one, count my streak" | ignore the next nudge |
| "Passport expires in 7 days" (`reminders`) | "Not now — remind me again tomorrow" | hope you remember |
| Daily digest full of movie news (`digest`) | "Mute movie news for a week" | edit monitors.json by hand |

ntfy already has the missing half: **action buttons** on notifications, and
topics work in both directions (anyone can POST to a topic; anyone can poll
one). We can build a full two-way control loop with zero new infrastructure —
no server, no webhook endpoint, just a second private ntfy topic and a poll at
the top of each GitHub Actions run.

## Goals / non-goals

**Goals**

1. Tappable buttons on selected pushes that change watcher behavior.
2. No new infrastructure: ntfy + GitHub Actions + state.json only.
3. Commands are low-stakes and idempotent — a replayed or duplicated command
   never corrupts state.
4. Each command type lands independently (see Rollout).

**Non-goals (v1)**

- Free-text replies or arbitrary remote control (no `RUN:`, no config edits).
- Muting *live* pushes — MUTE only suppresses digest-bound items, so a muted
  topic's high/urgent alerts (quakes, hurricanes) still ring through.
- Sub-15-minute command latency (bounded by the Actions cadence, see below).

## Architecture

### How ntfy action buttons work

ntfy notifications accept up to **three** action buttons via the `Actions`
header (alias `X-Actions`). Two relevant action types:

- **`view`** — tapping opens a URL in the browser. Would require a web
  endpoint that mutates our state → needs a server or a GitHub-API bridge with
  an embedded token. Wrong fit.
- **`http`** — tapping makes the *phone itself* fire a background HTTP request
  (method, URL, headers, body all configurable; default method is `POST`).
  No browser, no server of ours — and the URL can be **another ntfy topic**.
  This is the fit: the button's payload *is* the command, and ntfy's
  server-side message cache *is* the queue.

Header syntax — ntfy supports a simple comma format and a JSON array. We use
the **JSON array** form: the simple format needs fiddly quoting the moment a
label or body contains a comma, while `json.dumps(actions)` (default
`ensure_ascii=True`) produces a single-line, ASCII-safe header value that
needs none of `ntfy.py`'s RFC 2047 encoding gymnastics:

```
Actions: [{"action": "http", "label": "Done",
           "url": "https://ntfy.sh/<NTFY_CONTROL_TOPIC>",
           "method": "POST", "body": "DONE:water", "clear": true}]
```

(`clear: true` dismisses the notification once the tap succeeds — nice
feedback that the command was sent.)

`ntfy.push()` gains one optional parameter, `actions: list[dict] | None`,
serialized into this header. `events._push` forwards
`event.metadata["actions"]` the same way it already forwards `click_url` /
`attach_url`, so any topic can attach buttons by adding metadata to its
`emit()` call — no routing changes.

### End-to-end control flow

```
 GitHub Actions run N                    your phone                ntfy.sh
 ─────────────────────                   ──────────                ───────
 habits.run → emit(...,
   metadata.actions=[Done])
   → ntfy.push(NTFY_TOPIC) ───────────────► notification
                                            [Done] tapped
                                              │ http action
                                              ▼
                                  POST https://ntfy.sh/{CONTROL_TOPIC}
                                  body: DONE:water  ─────────────► cached
                                                                   (~12 h)
 GitHub Actions run N+1
 ─────────────────────
 control.poll(state)  ◄──── GET /{CONTROL_TOPIC}/json?poll=1&since=<last_seen_id>
   parse "DONE:water"
   mutate state (streak, slot suppression)
   advance last_seen_id
 ...then topics run against the mutated state...
 habits.run sees the 15:00 slot already "sent" → next nudge suppressed
 state.json committed back by the workflow (existing step)
```

The control topic is a **store-and-forward command queue**: the phone is the
producer, the Actions run is the consumer, ntfy's message cache is the buffer.

### Poll mechanism

New module **`notify_watcher/control.py`**, called from `main()` **before the
topic loop** and **regardless of `NOTIFY_ONLY`**:

- *Before the topic loop*, so a command takes effect in the same run that
  reads it (control runs, mutates state, then `habits.run` etc. see the
  mutation). Same reasoning as collectors-before-digest.
- *Regardless of `NOTIFY_ONLY`*, because the twitch workflow runs `main.py`
  every 15 minutes — piggybacking the poll on it drops effective command
  latency from "up to 3 h" to "≤ ~15 min" for free. The poll is one cheap GET
  that is empty almost every time.

The request:

```
GET {NTFY_SERVER}/{NTFY_CONTROL_TOPIC}/json?poll=1&since=<last_seen_id>
```

- `poll=1` returns cached messages and closes (no long-poll/stream).
- The response is ndjson; we process entries with `"event": "message"` only.
- `since=<message_id>` returns messages after that id. With no stored id
  (first ever run) we use `since=all` — harmless because commands are
  idempotent and the cache only holds ~12 h.

**Where `last_seen_id` lives — `state.json`**, under a single `control` key
owned by the new module:

```jsonc
"control": {
  "last_seen_id": "kp5x3kqDpe34",   // ntfy id of the newest processed message
  "last_seen_ts": 1781234567,        // its unix time — fallback cursor
  "processed_ids": ["..."]           // last ~100 ids, belt-and-braces dedup
}
```

Why three fields: `since=<id>` is the primary cursor, but ntfy's behavior when
the id has aged out of the cache needs verification (it may replay the whole
cache). `last_seen_ts` gives a fallback cursor (`since=<unix_ts>`), and
`processed_ids` makes replays harmless even if both cursors misbehave. The
commands themselves are also idempotent (see below), so triple protection is
cheap paranoia, not load-bearing complexity.

**Failure handling:** the poll wraps everything in the same
log-and-continue posture as topics — a control failure must never block the
watch run. Conversely the cursor only advances after a message is processed
(or rejected as malformed), so a crash mid-batch re-reads, and idempotency
absorbs the repeat.

**Delivery window caveat:** ntfy.sh caches messages ~12 h. The twitch
workflow gives ~96 polls/day, so a command would only be lost if GitHub
dropped *every* run for 12 h straight. Acceptable for v1; noted in Open
questions for self-hosted ntfy (configurable cache).

## Command vocabulary (v1 — deliberately minimal)

Grammar: `VERB:arg[:arg]`, ASCII only, parsed by strict regex per verb.
Anything that doesn't match a known verb + well-formed args + **known id** is
logged and dropped — unknown habit names, unknown topics, non-numeric
durations all fail closed. No command executes code or touches anything
outside its named state keys.

| Command | Example | Effect |
|---|---|---|
| `DONE:{habit_id}` | `DONE:water` | habit done now: update streak, suppress the next nudge today |
| `SNOOZE:{reminder_id}:{minutes}` | `SNOOZE:passport:1440` | re-deliver that reminder after N minutes |
| `MUTE:{topic}:{hours}` | `MUTE:movies:168` | drop that topic's digest-bound items for N hours |

- `habit_id` = the habit's `name` in habits.json (already documented there as
  "unique id").
- `reminder_id` = a new explicit `id` slug per reminders.json entry (see
  Config) — reminder *names* are free text ("Passport expires") and unfit for
  a `:`-delimited grammar.
- `topic` = the routing topic string (`movies`, `games`, `music`, ...), the
  same vocabulary monitors.json `priority.rules` already uses.
- Durations are clamped: SNOOZE 5 min – 30 days, MUTE 1 h – 30 days. At most
  `max_per_poll` (default 50) commands processed per run.

## Per-command behavior and state changes

### `DONE:{habit_id}` — habits

Two mutations, both performed by `control.py` (habits.py needs **zero new read
logic** for suppression):

1. **Suppress the next nudge** by inserting the next due-but-unsent slot key
   into the habit's existing dedup set:

   ```
   state["water_slots_sent"] += ["2026-06-10|15"]   # next unsent hour today
   ```

   `habits._due_slots` already skips slots present in that set, so the 15:00
   nudge silently doesn't fire while 18:00/21:00 still do. If no slots remain
   today, suppression is a no-op (the streak still records). Idempotent:
   re-adding the same slot key is a set-insert.

2. **Streak**, new key owned by control/habits:

   ```jsonc
   "habit_streaks": {
     "water": { "streak": 4, "last_done": "2026-06-10" }
   }
   ```

   Update rule (pure, date-only): `last_done == today` → no-op (idempotent);
   `last_done == yesterday` → `streak += 1`; else → `streak = 1`. Always set
   `last_done = today`.

**Button wiring:** `habits._run_one` adds
`metadata={"actions": [done_action(name)]}` to its `emit()` call. One button:
**[Done]** → `DONE:{name}`.

**Surfacing the streak** (nice-to-have, same PR or later): prefix the day's
first nudge with the current streak ("Day 5 — time for a glass of water").
Read-only on `habit_streaks`, no new state.

### `SNOOZE:{reminder_id}:{minutes}` — reminders

New state key:

```jsonc
"reminder_snoozes": {
  "passport": { "until": "2026-06-11T14:05:00+00:00" }
}
```

Only id + until are stored; the **reminder text is recomputed from
reminders.json at re-fire time** (single source of truth — no stale snapshot
if the entry is edited meanwhile).

Re-fire path in `reminders.run`: a new check that runs **every cycle, before
the `NOTIFY_DAILY` gate** (the rest of the topic stays daily-only). For each
snooze entry with `until <= now`: look up the entry by `id`, recompute the
occurrence and days-left, emit a "Reminder (snoozed)" push, delete the snooze
entry. Unknown id (entry deleted from reminders.json) → drop the snooze with a
log line.

The re-fired push does **not** touch `reminders_sent` — the original
`name|occ|lead` key stays marked sent, so the normal lead-day schedule is
unaffected; a snooze is an *extra* delivery, not a reschedule of the ladder.

**Granularity caveat:** re-fires happen when a run executes `reminders.run`,
i.e. the 3-hourly full run (the 15-min twitch run filters topics to twitch).
So `SNOOZE:x:60` effectively means "next full run" (≤ 3 h). Acceptable for
expiry-style reminders; see Open questions for tightening it.

**Button wiring:** reminder pushes get two buttons:
**[Snooze 1d]** → `SNOOZE:{id}:1440`, **[Snooze 3d]** → `SNOOZE:{id}:4320`.
(Sub-day snoozes are possible in the grammar but pointless at current
granularity, so the buttons don't offer them.) Idempotent: a repeated SNOOZE
just overwrites `until`.

### `MUTE:{topic}:{hours}` — digest

New state key:

```jsonc
"topic_mutes": {
  "movies": "2026-06-17T14:00:00+00:00"   // iso until
}
```

**Enforcement point: `events.emit`**, the single funnel every topic already
goes through. After the routing decision (engine or legacy, and after the
quiet-hours deferral), one new check:

> final action == `"digest"` **and** `event.topic` has an unexpired mute
> → action = `"drop"`.

Properties that fall out of this placement:

- Live pushes are untouched — a muted topic's high/urgent alerts still ring
  (deliberate: muting `weather` chatter must never mute a hurricane warning).
- A quiet-hours-deferred push that lands in the digest window of a muted topic
  is also dropped — consistent: it would have surfaced as a digest entry.
- The drop is recorded in the event log with the normal `eventlog.record`
  call, so the dashboard shows what the mute suppressed (and you can see it
  was working).

Items **already in `digest_buffer`** when the mute arrives: buffer items gain
a `topic` field (one-line change in `events._to_digest` / `digest.add`), and
`digest.flush` filters muted topics at flush time. Old buffer entries without
the field pass through (graceful migration). Expired `topic_mutes` entries are
pruned by `control.py` each run.

**Button wiring:** the digest push attaches up to three mute buttons for the
topics contributing the most items to *that* digest (computable at flush time
from the items' new `topic` field), e.g. **[Mute movies 7d]** →
`MUTE:movies:168`. `digest.flush` builds the buttons; `ntfy.push` carries
them.

### state.json — new keys, one table

| Key | Owner | Shape | Read by |
|---|---|---|---|
| `control` | control.py | `{last_seen_id, last_seen_ts, processed_ids}` | control.py only |
| `habit_streaks` | control.py | `{habit: {streak, last_done}}` | habits.py (display) |
| `{habit}_slots_sent` | *existing* | DONE inserts one slot key | habits.py (unchanged) |
| `reminder_snoozes` | control.py | `{id: {until}}` | reminders.py (re-fire) |
| `topic_mutes` | control.py | `{topic: until_iso}` | events.emit, digest.flush |
| `digest_buffer[].topic` | events.py | new field per item | digest.flush (mute filter, mute buttons) |

All keys ride the existing commit-state-back workflow step; no persistence
changes.

## Security

**Threat model.** The control topic name is a capability: anyone who knows it
can inject commands. It can leak three ways: brute force (mitigate: ≥ 16
random chars, same standard as the main topic), compromise of a device
subscribed to the main topic (the Actions header embeds the control topic
name in every buttoned push), or ntfy.sh itself (it sees both topics; already
trusted with all push content today).

**Blast radius is the real argument.** The command grammar is closed and every
command is bounded and reversible: the worst an attacker with the topic name
can do is mark your water habit done, snooze a reminder ≤ 30 days, or mute a
topic's *digest entries* ≤ 30 days — live high/urgent alerts cannot be muted
by design, no command reads or exfiltrates anything, none executes code or
touches keys outside the table above. Parser fails closed on unknown
verbs/ids/values and processing is capped per poll.

**Why not HMAC?** A per-command HMAC token sounds stronger but adds nothing
here: the signed command string would be embedded verbatim in the Actions
header of pushes to the main topic, so any leak path that reveals the control
topic name reveals valid signed commands right next to it — same capability,
plus a secret to manage and a replay-window scheme to design. Replay of a
captured command is already absorbed by idempotency.

**Verdict:** a private, high-entropy control topic is sufficient for v1, given
the bounded command set. Revisit (ntfy access tokens on a self-hosted server
being the cleaner upgrade, not HMAC) only if the command vocabulary ever grows
teeth — anything config-writing or data-reading changes this calculus.

## New config / secrets

**GitHub Actions secret:** `NTFY_CONTROL_TOPIC` — a second random private
topic name, e.g. `nw-ctl-<16 random chars>`. Exported in **both**
`watch.yml` and `twitch.yml` env blocks (the twitch run is the low-latency
poller). Missing/empty secret = feature off: `control.poll` returns
immediately and no buttons are attached (push behavior byte-identical to
today), which is also the rollback story.

**monitors.json — new `control` section** (tunables only, no secrets):

```jsonc
"control": {
  "_comment": "Reply-button command channel. enabled=false detaches all buttons and skips the poll even when the secret is set. Clamps below bound every command's effect.",
  "enabled": true,
  "max_per_poll": 50,
  "max_snooze_minutes": 43200,   // 30 d
  "max_mute_hours": 720          // 30 d
}
```

**reminders.json — new per-entry `id`** (required for SNOOZE buttons):

```jsonc
{ "id": "passport", "name": "Passport expires", "date": "2026-09-15", ... }
```

Entries without an `id` simply get no snooze buttons — fully backward
compatible.

`habits.json` needs no change (`name` is already the id).

## Rollout plan — four independently shippable steps

1. **Transport + poll loop (no commands).** `ntfy.push(actions=...)`,
   `events._push` forwarding, `control.py` with cursor management that
   **logs** parsed commands but executes nothing, secret + env wiring in both
   workflows. *Test:* `curl -d "DONE:water" https://ntfy.sh/$CTRL`, dispatch
   the workflow, see the log line and the advanced `control.last_seen_id` in
   the state commit. Re-dispatch: no reprocessing.
2. **DONE for habits.** Streak + slot-suppression handlers, [Done] button on
   habit pushes. *Test:* unit tests on the pure update rules (streak
   day-transitions, slot insertion, idempotent repeat); live: tap Done after a
   water nudge, confirm next slot is skipped and `habit_streaks` commits.
3. **SNOOZE for reminders.** `id` field, snooze handler, every-cycle re-fire
   path in reminders.run, snooze buttons. *Test:* unit tests for re-fire
   timing/unknown-id; live: snooze a test reminder 5 min, confirm re-delivery
   on the next full run.
4. **MUTE for digest.** `topic` on buffer items, emit-time drop + flush-time
   filter, mute buttons on the digest. *Test:* unit tests for
   emit-routing-with-mute and flush filtering; live: mute movies 1 h, confirm
   the event log records drops and the next digest omits them.

Each step lands behind the same kill switch (`control.enabled` /
unset secret) and changes nothing when off.

## Open questions (decide before coding)

1. **DONE scope:** suppress only the *next* slot (proposed) or all remaining
   slots today? Next-slot matches "I just drank water"; rest-of-day matches
   "I'm on top of this habit today". Affects step 2 only.
2. **Streak semantics:** does one DONE per day maintain the streak (proposed),
   or should a day with zero DONEs *and* zero suppressions break it only after
   N grace days? Proposed: simple — miss a day, streak resets.
3. **Snooze granularity:** is ≤ 3 h re-fire latency acceptable (proposed), or
   should the 15-min workflow run `reminders` too (`NOTIFY_ONLY:
   twitch,reminders` — cheap, pure date math, but renames the workflow's
   purpose)?
4. **Mute button selection:** top-3 topics by item count in the current digest
   (proposed), or a fixed configured list (e.g. always movies/games/music)?
5. **Command receipts:** should a processed command trigger a tiny
   confirmation push ("✓ water done — day 5")? Proposed: no for v1 (the
   `clear` on tap is the ack; receipts double notification volume), revisit
   per-command after living with it.
6. **`since=<expired id>` behavior:** verify against ntfy.sh during step 1
   whether an aged-out id replays the whole cache; the `processed_ids` dedup
   covers either answer, but the fallback-cursor code can be simplified if the
   server behavior is benign.
7. **Self-hosting later?** The ~12 h ntfy.sh cache bounds command durability.
   Fine at 96 polls/day; if the repo ever moves to a self-hosted ntfy, raise
   the cache and consider access tokens for the control topic.
