"""Static dashboard: render the watcher's state into one self-contained HTML page.

The watcher has no server — GitHub Actions runs it every 3h and commits ``state.json``.
So the dashboard is **static, regenerated each run**: this module reads the durable
data the watcher already commits (the ``eventlog`` history, the ``digest`` buffer, the
per-topic ``topic_health`` stamps from ``main``, and the ``last_run`` summary) and emits
a single ``index.html`` that GitHub Pages serves for free (docs/design/02-dashboard.md).

It invents no new pipeline: every alert row is the normalized ``Event`` the engine
already produced, and the ``[NN]`` score prefix is ``priority.decide``'s score verbatim.
The page is a pure *reader* of committed data, so it can never affect notifications.

``summarize`` is the pure, testable core (state -> a view-model dict, no HTML, no clock
dependency beyond the injected ``now``); ``render`` turns that into a self-contained HTML
string (inline CSS, a tiny inline-JS search over an embedded events blob — no build
toolchain, no runtime dependency); ``build`` wires it to disk for the workflow.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import logging
from pathlib import Path
from typing import Optional

from . import eventlog

log = logging.getLogger(__name__)

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "dashboard"
_RECENT_DAYS = 7
_MAX_ROWS = 200          # cap rows rendered + embedded for client-side search
# Priority-distribution buckets (label, inclusive lower bound), highest first.
_BANDS = (("90+", 90), ("70+", 70), ("40+", 40), ("<40", 0))


# --- time helpers ----------------------------------------------------------
def _parse_ts(s) -> Optional[_dt.datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _age(then: Optional[_dt.datetime], now: _dt.datetime) -> str:
    """Compact relative age: ``"just now"``, ``"2h ago"``, ``"3d ago"``."""
    if then is None:
        return "never"
    secs = (now - then).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _day_label(d: _dt.date) -> str:
    # %-d is not portable (glibc-only); build the non-zero-padded day by hand.
    return f"{d.strftime('%B')} {d.day}"


# --- pure view-model -------------------------------------------------------
def summarize(state: dict, now: Optional[_dt.datetime] = None) -> dict:
    """Reduce ``state`` to the dashboard's view-model. Pure and clock-injectable."""
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)

    log_entries = list(state.get(eventlog.EVENT_LOG_KEY) or [])
    # newest first, by timestamp (fall back to insertion order for unparseable ts)
    log_entries.sort(key=lambda e: e.get("ts") or "", reverse=True)

    cutoff = now - _dt.timedelta(days=_RECENT_DAYS)
    recent = [e for e in log_entries if (_parse_ts(e.get("ts")) or now) >= cutoff]

    # action counts over the window
    counts = {"push": 0, "digest": 0, "drop": 0}
    for e in recent:
        a = e.get("action", "")
        if a in counts:
            counts[a] += 1

    # priority distribution over the window
    dist = {label: 0 for label, _ in _BANDS}
    for e in recent:
        score = int(e.get("score", 0) or 0)
        for label, lo in _BANDS:
            if score >= lo:
                dist[label] += 1
                break

    # day-grouped recent alerts (cap total rows; skip drops — they were never shown)
    shown = [e for e in log_entries if e.get("action") != "drop"][:_MAX_ROWS]
    days: list[dict] = []
    cur_key = None
    for e in shown:
        ts = _parse_ts(e.get("ts"))
        key = ts.date().isoformat() if ts else "—"
        if key != cur_key:
            label = _day_label(ts.date()) if ts else "Unknown date"
            days.append({"label": label, "items": []})
            cur_key = key
        days[-1]["items"].append(e)

    # topic health rows, alphabetical
    health_rows = []
    for name in sorted(state.get("topic_health", {})):
        h = state["topic_health"][name]
        err = h.get("last_error")
        ts = _parse_ts(h.get("last_error_ts") if err else h.get("last_ok"))
        health_rows.append({
            "topic": name,
            "ok": not err,
            "error": err or "",
            "age": _age(ts, now),
        })

    last_run = state.get("last_run") or {}
    return {
        "now": now,
        "last_run_ts": last_run.get("ts", ""),
        "last_run_ok": last_run.get("ok", 0),
        "last_run_failed": last_run.get("failed", 0),
        "counts": counts,
        "distribution": dist,
        "days": days,
        "digest": list(state.get("digest_buffer") or []),
        "digest_last_sent": state.get("digest_last_sent", ""),
        "health": health_rows,
        "embed": shown,   # rows exposed to client-side search
    }


# --- HTML rendering --------------------------------------------------------
def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _bar(n: int, peak: int, width: int = 14) -> str:
    if peak <= 0:
        return ""
    return "█" * max(1, round(n / peak * width)) if n else ""


def render(state: dict, *, now: Optional[_dt.datetime] = None,
           reminders: Optional[list] = None) -> str:
    """Render the view-model to a self-contained HTML page (inline CSS + search JS)."""
    vm = summarize(state, now)
    c = vm["counts"]

    last = _parse_ts(vm["last_run_ts"])
    run_age = _age(last, vm["now"])
    run_ok = vm["last_run_failed"] == 0
    run_badge = "OK" if run_ok else f"{vm['last_run_failed']} failed"

    rows_html = []
    for day in vm["days"]:
        rows_html.append(f'<h3 class="day">{_esc(day["label"])}</h3>')
        for e in day["items"]:
            score = int(e.get("score", 0) or 0)
            action = e.get("action", "")
            mark = "pushed" if action == "push" else "digested"
            title = _esc(e.get("title", ""))
            url = e.get("url") or ""
            if url:
                title = f'<a href="{_esc(url)}" target="_blank" rel="noopener">{title}</a>'
            detail = e.get("detail", "")
            detail_html = f'<span class="detail">{_esc(detail)}</span>' if detail else ""
            rows_html.append(
                f'<div class="alert" data-text="{_esc((e.get("title","") + " " + (e.get("detail") or "") + " " + e.get("topic","")).lower())}">'
                f'<span class="score s{_band(score)}">{score}</span>'
                f'<span class="title">{title}{detail_html}</span>'
                f'<span class="topic">{_esc(e.get("topic",""))}</span>'
                f'<span class="mark {action}">{mark}</span>'
                f"</div>")
    alerts_html = "\n".join(rows_html) or '<p class="empty">No alerts logged yet.</p>'

    # today's digest (pending)
    digest_rows = []
    for it in sorted(vm["digest"], key=lambda i: i.get("score", 0), reverse=True):
        title = _esc(it.get("title", ""))
        detail = it.get("detail", "")
        line = f"{title} — {_esc(detail)}" if detail else title
        digest_rows.append(
            f'<li><span class="src">{_esc(it.get("source",""))}</span> {line}</li>')
    digest_html = ("<ul class='digest'>" + "".join(digest_rows) + "</ul>"
                   if digest_rows else '<p class="empty">Digest buffer is empty.</p>')

    # priority distribution
    peak = max(vm["distribution"].values() or [0])
    dist_rows = []
    for label, _ in _BANDS:
        n = vm["distribution"][label]
        dist_rows.append(
            f'<div class="distrow"><span class="band">{label}</span>'
            f'<span class="bar">{_bar(n, peak)}</span><span class="n">{n}</span></div>')
    dist_html = "".join(dist_rows)

    # topic health
    health_rows = []
    for h in vm["health"]:
        icon = "✓" if h["ok"] else "⚠"
        note = h["error"] if h["error"] else h["age"]
        cls = "ok" if h["ok"] else "warn"
        health_rows.append(
            f'<div class="hrow {cls}"><span class="hicon">{icon}</span>'
            f'<span class="htopic">{_esc(h["topic"])}</span>'
            f'<span class="hnote">{_esc(note)}</span></div>')
    health_html = "".join(health_rows) or '<p class="empty">No topic runs recorded yet.</p>'

    # upcoming reminders (optional, computed by build())
    rem_html = ""
    if reminders:
        items = "".join(
            f'<li><span class="when">{_esc(r.get("when",""))}</span> {_esc(r.get("name",""))}</li>'
            for r in reminders)
        rem_html = f"<section><h2>Upcoming reminders</h2><ul class='rem'>{items}</ul></section>"

    embed_json = json.dumps(vm["embed"], ensure_ascii=False)
    generated = vm["now"].strftime("%Y-%m-%d %H:%M UTC")

    return _PAGE.format(
        generated=generated,
        run_age=_esc(run_age),
        run_cls="ok" if run_ok else "warn",
        run_badge=_esc(run_badge),
        pushes=c["push"], digested=c["digest"], dropped=c["drop"],
        recent_days=_RECENT_DAYS,
        digest_last=_esc(vm["digest_last_sent"] or "—"),
        digest_html=digest_html,
        alerts_html=alerts_html,
        dist_html=dist_html,
        health_html=health_html,
        rem_html=rem_html,
        embed_json=embed_json,
    )


def _band(score: int) -> str:
    for label, lo in _BANDS:
        if score >= lo:
            return label.replace("+", "").replace("<", "lt")
    return "0"


def build(reminders: Optional[list] = None, out_dir: Path = OUT_DIR) -> Path:
    """Load committed state, render the page, write ``docs/dashboard/index.html``."""
    from . import state as state_mod
    state = state_mod.load()
    if reminders is None:
        reminders = _upcoming_reminders()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "index.html"
    out.write_text(render(state, reminders=reminders), encoding="utf-8")
    log.info("dashboard written to %s", out)
    return out


def _upcoming_reminders(horizon_days: int = 60, limit: int = 8) -> list[dict]:
    """Next occurrence of each reminder within the horizon, soonest first.

    Reuses the reminders date engine's ``_next_occurrence`` (so recurring birthdays/
    renewals roll forward correctly) rather than ``_due`` — ``_due`` only yields the
    items firing *today* at a configured lead, not a forward-looking list. Best-effort
    and isolated: any failure (missing/invalid reminders.json) yields an empty list
    rather than breaking the build."""
    try:
        from .topics import reminders as rem
        today = _dt.date.today()
        rows = []
        for r in rem._load():
            name, date_str = r.get("name"), r.get("date")
            if not name or not date_str:
                continue
            try:
                base = _dt.date.fromisoformat(date_str)
            except (ValueError, TypeError):
                continue
            occ = rem._next_occurrence(base, today, (r.get("recurring") or "").lower())
            if occ is None:
                continue
            days_left = (occ - today).days
            if 0 <= days_left <= horizon_days:
                when = "today" if days_left == 0 else f"{occ.strftime('%b')} {occ.day}"
                rows.append({"name": name, "when": when, "days": days_left})
        rows.sort(key=lambda x: x["days"])
        return rows[:limit]
    except Exception as exc:  # noqa: BLE001 - the dashboard never breaks the run
        log.warning("could not compute upcoming reminders: %s", exc)
        return []


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build()
    return 0


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>notify-watcher</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
  background: #11151c; color: #d7dde5; padding: 1.5rem; }}
.wrap {{ max-width: 960px; margin: 0 auto; }}
header {{ display: flex; align-items: baseline; justify-content: space-between;
  border-bottom: 1px solid #2a323d; padding-bottom: .6rem; margin-bottom: 1rem; }}
h1 {{ font-size: 1.2rem; margin: 0; }}
h2 {{ font-size: .95rem; text-transform: uppercase; letter-spacing: .04em;
  color: #8b97a7; margin: 1.6rem 0 .5rem; }}
h3.day {{ font-size: .85rem; color: #8b97a7; margin: .9rem 0 .3rem; }}
.run.ok {{ color: #5fd28b; }} .run.warn {{ color: #f0a35e; }}
.counts {{ color: #8b97a7; margin: 0 0 .5rem; }}
input#q {{ width: 100%; padding: .5rem .7rem; margin: .3rem 0 1rem; border-radius: 6px;
  border: 1px solid #2a323d; background: #161b23; color: #d7dde5; }}
.alert {{ display: grid; grid-template-columns: 2.6rem 1fr auto auto; gap: .6rem;
  align-items: baseline; padding: .25rem 0; border-bottom: 1px solid #1c222b; }}
.score {{ font-variant-numeric: tabular-nums; text-align: right; font-weight: 600;
  color: #11151c; border-radius: 4px; padding: 0 .35rem; }}
.score.s90 {{ background: #f06363; }} .score.s70 {{ background: #f0a35e; }}
.score.s40 {{ background: #e7cf5a; }} .score.slt40 {{ background: #5a6b7e; color: #d7dde5; }}
.title a {{ color: #9ec5ff; text-decoration: none; }} .title a:hover {{ text-decoration: underline; }}
.detail {{ color: #8b97a7; }} .detail::before {{ content: " — "; }}
.topic {{ color: #6f7c8c; font-size: .8rem; }}
.mark {{ font-size: .75rem; }} .mark.push {{ color: #5fd28b; }} .mark.digest {{ color: #8b97a7; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; }}
@media (max-width: 640px) {{ .cols {{ grid-template-columns: 1fr; }} }}
.distrow, .hrow {{ display: grid; grid-template-columns: 3rem 1fr 2.5rem; gap: .5rem;
  align-items: baseline; }}
.hrow {{ grid-template-columns: 1.2rem 7rem 1fr; }}
.bar {{ color: #6f9bd6; letter-spacing: -1px; }} .n {{ text-align: right; color: #8b97a7; }}
.hrow.warn .hicon, .hrow.warn .hnote {{ color: #f0a35e; }} .hrow.ok .hicon {{ color: #5fd28b; }}
.hnote {{ color: #8b97a7; }}
ul.digest, ul.rem {{ list-style: none; padding: 0; margin: 0; }}
ul.digest li, ul.rem li {{ padding: .2rem 0; border-bottom: 1px solid #1c222b; }}
.src, .when {{ color: #6f7c8c; font-size: .8rem; margin-right: .4rem; }}
.empty {{ color: #6f7c8c; font-style: italic; }}
footer {{ color: #4f5b6b; font-size: .75rem; margin-top: 2rem;
  border-top: 1px solid #2a323d; padding-top: .6rem; }}
</style></head>
<body><div class="wrap">
<header>
  <h1>notify-watcher</h1>
  <span class="run {run_cls}">last run: {generated} · {run_age} · {run_badge}</span>
</header>

<p class="counts">last {recent_days}d — pushes: {pushes} · digested: {digested} · dropped: {dropped}</p>
<input id="q" type="search" placeholder="search alerts…" oninput="filter(this.value)">

<section><h2>Today's digest (pending · last flush {digest_last})</h2>
{digest_html}</section>

<section><h2>Recent alerts</h2>
<div id="alerts">
{alerts_html}
</div></section>

<div class="cols">
  <section><h2>Priority distribution ({recent_days}d)</h2>{dist_html}</section>
  <section><h2>Topic health</h2>{health_html}</section>
</div>
{rem_html}

<footer>Static page, regenerated each watcher run · served from /docs by GitHub Pages.</footer>
</div>
<script id="events" type="application/json">{embed_json}</script>
<script>
function filter(q) {{
  q = q.trim().toLowerCase();
  document.querySelectorAll('#alerts .alert').forEach(function (el) {{
    el.style.display = (!q || el.dataset.text.indexOf(q) !== -1) ? '' : 'none';
  }});
  document.querySelectorAll('#alerts .day').forEach(function (h) {{
    var n = h.nextElementSibling, any = false;
    while (n && !n.classList.contains('day')) {{
      if (n.classList.contains('alert') && n.style.display !== 'none') any = true;
      n = n.nextElementSibling;
    }}
    h.style.display = any ? '' : 'none';
  }});
}}
</script>
</body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
