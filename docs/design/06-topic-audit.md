# 06 — Topic Audit (August 2026)

**Status: IMPLEMENTED.** Full sweep of all 41 registered notification topics.
One broken, two degraded-by-design, three orphaned state entries, the rest
healthy. This note records the evidence so the next audit starts from facts
rather than re-deriving them.

Companion to `04-reliability-safety-layer.md`, whose addendum covers the
*monitoring* layer. This one covers the *topics* the monitoring layer watches.

---

## Method

Health was judged from three independent sources, not from one:

1. **`state.json` → `topic_health`** — the `last_ok` / `last_error` /
   `last_data` stamps `main.py` writes every run.
2. **A full-sweep Actions log** (run 2392, the 2m6s run — the 30s runs are
   twitch/habits fast lanes and prove nothing about the other 39 topics).
   Every `ERROR`/`WARNING` line was extracted.
3. **Live requests to the sources themselves**, which is the only way to tell
   "quiet because nothing happened" from "quiet because it is broken".

The third matters because `last_ok` alone is not evidence of health for the 12
topics that are not on the health contract (`health.ADOPTED`) — for those,
"didn't raise" is all a fresh `last_ok` means.

---

## Verdict

| Bucket | Count | Topics |
|---|---|---|
| Healthy | 38 | everything not listed below |
| Warning | 2 | `deals` (Costco leg), `golden_sun` (Reddit JSON leg) |
| Broken | 1 | `visa_bulletin` |
| Intentionally quiet | — | see note |

**"Intentionally quiet" is a property of a run, not of a topic.** Every topic
here is registered and runs; several are gated (daily, weekly, on-demand) and
so are silent on most runs by design: `research` (no-op without a dispatched
URL), `recap` / `life_dashboard` / `games` / `spending` (weekly), `learn` /
`energy_learn` / `groceries` / `fuel` / `holidays` / `itsc` / `uv` / `marine` /
`astronomy` / `apod` / `reminders` / `bills` (daily), `spark` (6-hour window).
None of those were mis-reporting.

---

## 1. `visa_bulletin` — BROKEN, repaired

### Root cause

`travel.state.gov` moved behind **Cloudflare bot management**. Every HTTP
client gets `HTTP 403` with `cf-mitigated: challenge` and a "Just a moment…
Enable JavaScript and cookies to continue" interstitial.

### Evidence

Four User-Agent variants tested against the index URL:

| Request | Result |
|---|---|
| `notify-watcher/1.0` (the topic's UA) | 403, `server: cloudflare` |
| No UA header | 403 |
| `requests` default UA | 403 |
| Real Chrome UA string | **403** |

So it is not a User-Agent problem and not an IP-reputation problem. A real
Chrome browser loads the same URL fine after roughly five seconds of JS
challenge — the content is intact; only unattended clients are refused.

**Cost of the outage:** the topic had been failing since **2026-07-14** and
alerted once, on 07-16, then went silent (the watchdog bug fixed in
`04`'s addendum). It therefore missed the **August 2026 bulletin entirely**:

- F4 Final Action, All Other: `01JAN09` → `01SEP09` (**+243 days**)
- F4 Dates for Filing, All Other: `01MAR10` → `22JUN10` (**+113 days**)

An eight-month advance on an F4 priority date, unreported for two and a half
weeks.

### What was rejected

Getting past a Cloudflare JS challenge needs a headless browser, a
TLS-fingerprint spoofer (`curl_impersonate`, `cloudscraper`), or a
challenge-solving service. All three are bot-detection evasion; all three break
whenever Cloudflare updates; none belong in an unattended workflow running
96×/day. **Not implemented, and should not be.**

### Fix applied

The State Department publishes the same bulletin as a **PDF under
`/content/dam/`** — the canonical published artifact, not a mirror — and serves
it plainly:

```
https://travel.state.gov/content/dam/visas/Bulletins/visabulletin_August2026.pdf
→ HTTP 200, application/pdf, no challenge
```

Verified across the Jan / May / Jun / Jul / Aug 2026 editions; future months
404 cleanly, which is what makes discovery work.

Changes in `notify_watcher/topics/visa_bulletin.py`:

- **Discovery** — was "scrape every link off the index page". Now derives the
  filename from the calendar and walks *down* from two months ahead, taking the
  first PDF that exists. An early publication is found immediately; a late one
  resolves to the previous edition, which the existing late-bulletin alert then
  reports on, unchanged.
- **Parsing** — was BeautifulSoup over two HTML tables. Now `pypdf` (already a
  dependency, for the `fuel` and `outages` notices) over the flattened text.
  The employment tables use E1–E5, so `F4` appears exactly twice; each row is
  attributed to its **nearest preceding heading** rather than assuming order.
- **Strictness** — if the document does not yield exactly one F4 row per
  section, it **raises**. These are dates someone plans an immigration case
  around; a confidently-wrong number is far worse than a loud failure, and the
  rebuilt watchdog now reports that failure within one run.
- **Links unchanged** — notification click targets still point at the HTML
  page. A person tapping the embed opens a real browser, which clears the
  challenge on its own. Only the unattended fetch moved.

Everything downstream — change detection, the F4 pace estimator, edition
tracking, the late-bulletin alert — is untouched.

### Remaining concern

If `/content/dam/` is ever put behind the same challenge, this breaks too, and
there is no third official source: **USCIS was evaluated and rejected** — it
publishes only *which chart to use* each month and links back to
travel.state.gov for the actual numbers, so it can detect a new edition but
cannot supply the F4 dates.

**Maintenance cost:** near zero while it holds. The URL scheme is mechanical
and the PDF layout has been stable across every 2026 edition. No credentials,
no rate limits, one HEAD probe plus one download per run.

**If it does break:** the honest options are (a) subscribe to the State
Department's own GovDelivery email list and parse the message — this repo
already has Gmail IMAP wired up for the spending tracker, so the plumbing
exists; or (b) retire the topic and check manually each month.
**Recommendation: do not retire now.** The fix is clean, official, and cheap,
and the topic tracks something genuinely consequential.

---

## 2. `deals` (Costco leg) — WARNING, no action

**Root cause:** `costco.com` product pages sit behind Akamai bot protection and
return 403 to the runner. Already documented in `deals.py`.

**Why no fix:** the topic is on the health contract and only reports
`source_failed` when *every* product fails; one blocked store is a logged
warning, not an outage. The same product is also tracked via Amazon
(`Highland Tactical Foxtrot Backpack (Amazon)`), so price coverage is intact.
The entry is kept deliberately so it resumes the moment Costco is reachable,
and `DEALS_PROXY` exists as an operator-supplied escape hatch.

**Cost:** one log line per full sweep. No user-visible impact.

---

## 3. `golden_sun` (Reddit leg) — WARNING, no action

**Root cause:** Reddit 403s its `.json` endpoint for datacenter IPs.

**Why no fix:** `_reddit_get` already retries with a compliant UA and backoff,
then falls back to the `.rss` feed, which succeeds. Observed working in the
audited sweep. The fallback loses post scores, so `reddit_min_score` filtering
is skipped and the top *N* are taken instead — a documented, intended
degradation.

---

## 4. Orphaned health entries — fixed

`topic_health` carried three entries for topics that no longer exist:
`beach_day`, and `health_tip` / `wikiquote` (both absorbed into `spark`). They
had been frozen for ~18 days and were still being counted as tracked topics by
the dashboard, the weekly recap, and the watchdog's log lines.

Fixed in code rather than by hand-editing `state.json` (which the runner
rewrites on every sweep): `main._prune_retired_health` drops entries not in the
full `TOPICS` registry, once per run. It compares against the **full** registry,
never the `NOTIFY_ONLY` subset — otherwise the 15-minute twitch fast lane would
erase 39 topics of history every run.

---

## Recommendation not acted on

`outages` (EDEESTE scheduled power cuts) is **not** in
`monitors.json → watchdog.data_stale_days`, so a silent parse-to-zero would go
unnoticed. Its `last_data` was 5.6 days old at audit time, consistent with a
weekly PDF, so nothing is wrong today. Adding it at a ~14-day threshold would
close the gap. Left out of this change to keep it scoped to what is actually
broken; it is a one-line config edit when wanted.
