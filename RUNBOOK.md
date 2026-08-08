# RUNBOOK.md — notify-watcher

Symptom-first troubleshooting. Find what you're seeing, go to the likely
cause, do the fix. See `CLAUDE.md` for how the system fits together.

---

### "I haven't gotten any notifications at all in a while"

**Likely cause:** either nothing new is happening (genuinely quiet), or
GitHub Actions itself is the problem (billing lockout, disabled workflow,
outage) — this is the one failure mode the in-repo alerting can't see
itself, because `alert.yml` also runs on GitHub Actions.

**Check:**
1. Discord's LOGS channel for a message from the **Cloudflare Worker**
   heartbeat ("watch has not succeeded in 7h+") — this is the one monitor
   that runs outside GitHub Actions and can see this specific failure mode.
2. GitHub repo → Actions tab → is `watch` even showing recent runs? If
   none in the last ~20 min, the Cloudflare Worker cron dispatch itself may
   be failing, or the workflow got disabled (GitHub auto-disables workflows
   after 60 days with no repo activity).
3. Repo → Settings → Billing, if this is ever a private repo again (it's
   currently public, so Actions minutes are unlimited and this specific
   cause shouldn't recur the way it did in July 2026).

---

### "One specific topic hasn't pushed anything in a while"

**Likely cause:** could be genuinely quiet (nothing to report), a source
pausing/changing on their end, or a real bug. **Green tests and a recent
`last_ok` timestamp do NOT rule out a bug** — see the fuel precedent below.

**Check, in order:**
1. `state.json` → `topic_health.<name>` — look at `last_ok`, `last_error`,
   `source_failed`, and `empty_runs`/`empty_since` if present.
2. If `source_failed: true` with a real error message — that's usually the
   external source's problem, not yours (confirm by checking the source
   directly, e.g. visiting the page the topic scrapes).
3. If `last_ok` is recent and healthy-looking but you *know* something
   should have happened — don't assume it's fine. **Precedent:** `fuel`
   reported itself healthy every single day for a month while returning
   identical, wrong prices, because a PDF-parsing bug made every fuel look
   unchanged and unchanged always evaluates as "nothing to alert." Any
   topic that works by diffing against its own last stored value is
   exposed to this exact failure shape. If in doubt, manually verify the
   actual current source value against what the topic last reported.
4. Check the priority score for that topic/severity in `monitors.json` —
   if it's below `digest_floor` (25), it's being dropped intentionally,
   not failing.

---

### "A topic's digest line seems to be missing even though state.json shows it ran"

**Likely cause:** digest eviction. The digest buffer caps at 30 items/day;
when full, the *lowest-scored* items get dropped first (`_drop_lowest` in
`digest.py`).

**Check:** that topic's priority score in `monitors.json` vs. everything
else that emitted the same day. A topic sitting at the default/low end
(25-45) can occasionally lose out to a genuinely crowded day. This is by
design, not a bug — but if it's happening often for something you care
about, raise its score in `monitors.json`.

---

### "The dashboard (docs/dashboard or dashboard.html) looks stale"

**Likely cause:** the dashboard-build steps in `watch.yml` are deliberately
non-fatal (`|| echo "...non-fatal"`) so a broken dashboard build can never
block the state commit or fail the whole run. That also means **a broken
dashboard build sends you no notification at all.**

**Check:** the Actions log for the specific `watch` run, the "Build
dashboard" / "Generate status dashboard" steps — look for the actual error
even though the step shows green overall.

---

### "spending hasn't picked up any transactions in days" *(resolved 2026-08-08)*

**Root cause, confirmed:** not a credentials or mailbox problem — IMAP was
matching plenty of real BHD mail (19 messages in one 7-day window). The
actual cause was behavioral, not technical: the debit card was cut, so
card-purchase alerts ("BHD Notificación de Transacciones", the only subject
originally watched) stopped arriving almost entirely. What kept arriving
instead — PIN cash withdrawals and inter-account transfers — uses a
completely different BHD template ("Transacciones entre productos BHD y a
otros Bancos") with no HTML table at all, just inline prose naming the
transaction type/date/amount. The parser only knew how to read the table
format, so even the rare matching email parsed zero transactions.

**Fix applied:** `spending.py` now watches both subjects and has a second
parser (`_parse_prose_transaction`) for the no-table format, tried as a
fallback whenever the table parser finds nothing. PIN withdrawals are
recorded with the transaction type ("PIN Pesos") as the merchant, so
repeated withdrawals group into one line in the weekly summary rather than
each looking like a separate untraceable expense. `monitors.json`'s
`spending.subject` is now a list of both subjects instead of one string.

**If this class of issue recurs** (a topic goes quiet after a real change in
your own account activity, not a bug): check `state.json`'s
`topic_health.spending.data_count` first — if it's genuinely 0 across many
runs while other bank mail is still arriving, the diagnostic line to pull
from the Actions log is `"spending: IMAP search matched"` (total messages
from the sender) alongside `"...message(s) matched subject; parsed N
transaction(s)"` (how many of those matched a known subject and actually
parsed). A gap between those two numbers is the exact signal a new BHD
template needs support — same shape as this fix.

---

### "outages hasn't fired in a while" *(known, source-side, as of this audit)*

**Likely cause:** EDEESTE (the utility) simply hasn't published a new
weekly maintenance-schedule PDF. Confirmed by direct site check: their
homepage is active, but the specific "Programa de Mantenimiento" archive
page has had no new package since the one covering 2026-07-20 to 07-26.
The code is correctly detecting and reporting this — nothing to fix here,
just wait on them or check the page manually.

---

### "A GitHub Actions run failed and I want to know why fast"

1. Actions tab → the specific `watch` run → expand the failed step.
2. If it failed inside the Python process (per-topic try/except didn't
   save it) rather than crashing the workflow, that's specifically the
   **watchdog topic itself** failing — by design, that's the one topic
   whose failure is allowed to turn the whole run red (exit code 1),
   because a broken watchdog can't report its own outage otherwise. Check
   `state["topic_health"]["watchdog"]` and the watchdog's own log line.
3. `pip install` step failing outright usually means a dependency issue —
   check whether a new release of something in `requirements.txt` (all
   pinned floor-only, no lock file) shipped a breaking change. `pypdf` and
   `cryptography` are the two furthest behind current releases and worth
   checking first.

---

### "I changed something and now tests fail"

Run `python -m unittest discover -s tests -v` locally (or have Claude Code
do it) before pushing — the full suite runs in well under a second, so
there's no reason to find out via a broken production run. 1,187 tests as
of this audit; if that number looks very different, something structural
changed and it's worth understanding why before continuing.
