"""Standalone status dashboard generator — a cinematic, JRPG-styled topic grid.

Phase 1 of the dashboard expansion. Unlike ``dashboard.py`` (the GitHub Pages
alert feed under ``docs/dashboard``), this is a deliberately self-contained
script: it imports nothing from the package, reads the two committed config/data
files directly from the repo root, and emits a single ``dashboard.html`` there.

It is a pure *reader* of committed data — it never touches state, never sends a
notification, and has no runtime dependency beyond the standard library. The
page itself pulls Tailwind + fonts from CDNs, so the generated file is one
portable HTML document you can open straight from disk.

What it reads:
  state.json     topic_health (per-topic OK/error stamps), muted (topic->until),
                 digest_buffer (pending items, each tagged with its topic),
                 last_run (the most recent sweep summary).
  monitors.json  per-topic ``_comment`` strings, used as the card descriptions.

What it shows, for every topic:
  * Health status — a glowing green dot when the source last ran OK, a pulsing
    red dot when its last report was an error/source failure, dim when idle.
  * Mute status — an amber badge with a live countdown when the topic is muted.
  * Digest backlog — the number of items waiting in the daily-digest buffer.

Run it:  ``python -m notify_watcher.generate_dashboard``  (or run the file).
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state.json"
MONITORS_PATH = ROOT / "monitors.json"
OUT_PATH = ROOT / "dashboard.html"


# --- tiny self-contained helpers (no package imports, on purpose) ----------
def _load_json(path: Path) -> dict:
    """Best-effort JSON read; a missing/corrupt file yields an empty dict so the
    page still renders (the dashboard must never be the thing that breaks)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", path.name, exc)
        return {}


def _parse_ts(s: object) -> Optional[_dt.datetime]:
    """Parse an ISO timestamp; assume UTC when naive. None on anything invalid."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=_dt.timezone.utc) if dt.tzinfo is None else dt


def _age(then: Optional[_dt.datetime], now: _dt.datetime) -> str:
    """Compact relative age: ``just now`` / ``5m ago`` / ``3h ago`` / ``2d ago``."""
    if then is None:
        return "never"
    secs = (now - then).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _countdown(until: _dt.datetime, now: _dt.datetime) -> str:
    """How much of a mute is left: ``2d 3h left`` / ``11h 42m left`` / ``8m left``."""
    secs = int((until - now).total_seconds())
    if secs <= 0:
        return "expiring"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h left"
    if h:
        return f"{h}h {m}m left"
    return f"{max(m, 1)}m left"


def _label(slug: str) -> str:
    """``anthropic_news`` -> ``Anthropic News`` (with a few nicer special cases)."""
    special = {"fx": "FX", "uv": "UV", "iss": "ISS", "apod": "APOD",
               "fda": "FDA", "itsc": "ITSC", "ios_release": "iOS Release"}
    if slug in special:
        return special[slug]
    return " ".join(p.capitalize() for p in slug.replace("-", "_").split("_") if p) or slug


def _first_sentence(comment: object, limit: int = 120) -> str:
    """Trim a monitors.json ``_comment`` to a one-line card description."""
    if not isinstance(comment, str) or not comment.strip():
        return ""
    text = " ".join(comment.split())
    dot = text.find(". ")
    if 0 < dot < limit:
        return text[: dot + 1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _esc(s: object) -> str:
    return html.escape(str(s if s is not None else ""))


# --- pure view-model -------------------------------------------------------
# status -> (border ring, dot classes, hover-title verb)
_STATUS_VISUALS = {
    "healthy": (
        "border-emerald-400/20 hover:border-emerald-400/40",
        "bg-emerald-400 shadow-[0_0_12px_2px_rgba(16,185,129,.75)] animate-pulse-ring",
    ),
    "failing": (
        "border-rose-500/40 hover:border-rose-500/60 bg-rose-950/10",
        "bg-rose-400 shadow-[0_0_12px_2px_rgba(244,63,94,.8)] animate-alert-ring",
    ),
    "idle": (
        "border-white/5 hover:border-white/15",
        "bg-slate-600",
    ),
}
# Stable sort: surface problems first, then idle, then the healthy majority.
_STATUS_RANK = {"failing": 0, "idle": 1, "healthy": 2}


def build_topics(state: dict, monitors: dict, now: _dt.datetime) -> list[dict]:
    """Reduce the raw files to one view-model row per topic (problems first)."""
    health = state.get("topic_health") or {}
    muted = state.get("muted") or {}
    pending = Counter(
        it.get("topic", "")
        for it in (state.get("digest_buffer") or [])
        if isinstance(it, dict)
    )

    # Union every topic we have any signal for: a health stamp, an active mute,
    # or queued digest items. topic_health is the spine; the others rarely add
    # a name but we never want to silently drop one.
    names = set(health) | {k for k in muted} | {t for t in pending if t}

    rows: list[dict] = []
    for slug in names:
        h = health.get(slug) or {}
        failing = bool(h.get("last_error") or h.get("source_failed"))
        if failing:
            status = "failing"
            err = h.get("last_error") or "source reported a failure"
            ts = _parse_ts(h.get("last_error_ts")) or _parse_ts(h.get("last_ok"))
            note = _first_sentence(err, 80)
        elif h.get("last_ok"):
            status = "healthy"
            ts = _parse_ts(h.get("last_ok"))
            note = f"last ok {_age(ts, now)}"
        else:
            status = "idle"
            ts = None
            note = "no runs recorded yet"

        mute_until = _parse_ts(muted.get(slug))
        muted_active = mute_until is not None and mute_until > now

        rows.append({
            "slug": slug,
            "label": _label(slug),
            "desc": _first_sentence((monitors.get(slug) or {}).get("_comment")
                                    if isinstance(monitors.get(slug), dict) else ""),
            "status": status,
            "note": note,
            "muted": muted_active,
            "mute_countdown": _countdown(mute_until, now) if muted_active else "",
            "mute_until": mute_until.strftime("%Y-%m-%d %H:%M UTC") if muted_active else "",
            "digest": int(pending.get(slug, 0)),
        })

    rows.sort(key=lambda r: (_STATUS_RANK.get(r["status"], 1), r["label"].lower()))
    return rows


def summarize(state: dict, monitors: dict, now: _dt.datetime) -> dict:
    """Top-line counters plus the per-topic rows the grid renders."""
    topics = build_topics(state, monitors, now)
    last_run = state.get("last_run") or {}
    return {
        "now": now,
        "topics": topics,
        "total": len(topics),
        "healthy": sum(t["status"] == "healthy" for t in topics),
        "failing": sum(t["status"] == "failing" for t in topics),
        "muted": sum(t["muted"] for t in topics),
        "queued": sum(t["digest"] for t in topics),
        "last_run_ts": _parse_ts(last_run.get("ts")),
        "last_run_ok": int(last_run.get("ok", 0) or 0),
        "last_run_failed": int(last_run.get("failed", 0) or 0),
    }


# --- HTML rendering --------------------------------------------------------
def _stat_tile(value: int, label: str, color: str, glow: str = "") -> str:
    glow_cls = f" {glow}" if glow else ""
    return (
        '<div class="rounded-xl border border-white/5 bg-slate-900/50 px-4 py-3 '
        'backdrop-blur-md">'
        f'<div class="font-display text-3xl font-bold leading-none {color}{glow_cls}">{value}</div>'
        f'<div class="mt-1.5 text-[11px] font-medium uppercase tracking-[0.2em] text-slate-500">{_esc(label)}</div>'
        "</div>"
    )


def _pill(text: str, classes: str, title: str = "") -> str:
    title_attr = f' title="{_esc(title)}"' if title else ""
    return (
        f'<span{title_attr} class="inline-flex items-center gap-1 rounded-full '
        f'px-2.5 py-1 font-display font-semibold uppercase tracking-[0.12em] {classes}">'
        f"{_esc(text)}</span>"
    )


def _card(t: dict) -> str:
    ring, dot = _STATUS_VISUALS.get(t["status"], _STATUS_VISUALS["idle"])

    if t["status"] == "healthy":
        health_pill = _pill(f"online · {t['note']}",
                            "bg-emerald-400/10 text-emerald-300 ring-1 ring-inset ring-emerald-400/20",
                            "Source last reported OK")
        dot_title = f"Healthy — {t['note']}"
    elif t["status"] == "failing":
        health_pill = _pill(f"degraded · {t['note']}",
                            "bg-rose-500/10 text-rose-300 ring-1 ring-inset ring-rose-500/30",
                            "Source last report was a failure")
        dot_title = f"Failing — {t['note']}"
    else:
        health_pill = _pill("idle",
                            "bg-white/5 text-slate-400 ring-1 ring-inset ring-white/10",
                            t["note"])
        dot_title = "No runs recorded yet"

    mute_pill = ""
    if t["muted"]:
        mute_pill = _pill(f"muted · {t['mute_countdown']}",
                          "bg-amber-400/10 text-amber-300 ring-1 ring-inset ring-amber-400/25",
                          f"Muted until {t['mute_until']}")

    if t["digest"] > 0:
        digest_pill = _pill(f"queue · {t['digest']}",
                            "bg-cyan-400/10 text-cyan-300 ring-1 ring-inset ring-cyan-400/25 "
                            "shadow-[0_0_10px_rgba(34,211,238,.25)]",
                            f"{t['digest']} item(s) waiting in the daily digest")
    else:
        digest_pill = _pill("queue · 0",
                            "bg-white/5 text-slate-500 ring-1 ring-inset ring-white/10",
                            "Digest buffer empty for this topic")

    desc_html = (
        f'<p class="mt-2 text-xs leading-relaxed text-slate-500 line-clamp-2">{_esc(t["desc"])}</p>'
        if t["desc"] else '<p class="mt-2 text-xs italic text-slate-600">No monitor description.</p>'
    )

    return (
        f'<article class="group relative overflow-hidden rounded-2xl border {ring} '
        'bg-slate-900/50 card-sheen p-4 backdrop-blur-md transition duration-300 '
        'hover:-translate-y-0.5 hover:bg-slate-900/70">'
        '<div class="flex items-start justify-between gap-3">'
        '<div class="min-w-0">'
        f'<h3 class="truncate font-display text-lg font-semibold tracking-wide text-slate-100">{_esc(t["label"])}</h3>'
        f'<p class="font-mono text-[10px] uppercase tracking-[0.2em] text-slate-500">{_esc(t["slug"])}</p>'
        "</div>"
        f'<span title="{_esc(dot_title)}" class="relative mt-1.5 inline-flex h-3 w-3 shrink-0 rounded-full {dot}"></span>'
        "</div>"
        f"{desc_html}"
        '<div class="mt-4 flex flex-wrap items-center gap-1.5 text-[10px]">'
        f"{health_pill}{mute_pill}{digest_pill}"
        "</div>"
        "</article>"
    )


def render(state: dict, monitors: dict, now: Optional[_dt.datetime] = None) -> str:
    """Render the full self-contained HTML page (Tailwind + fonts via CDN)."""
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    vm = summarize(state, monitors, now)

    # header: last-sweep badge
    if vm["last_run_ts"] is not None:
        ok_sweep = vm["last_run_failed"] == 0
        badge_cls = ("border-emerald-400/30 bg-emerald-400/10 text-emerald-300"
                     if ok_sweep else "border-rose-500/30 bg-rose-500/10 text-rose-300")
        dot_cls = "bg-emerald-400" if ok_sweep else "bg-rose-400"
        badge = (
            f'<div class="inline-flex items-center gap-2 rounded-full border {badge_cls} '
            'px-3.5 py-1.5 text-xs font-medium backdrop-blur-md">'
            f'<span class="h-1.5 w-1.5 rounded-full {dot_cls}"></span>'
            f'last sweep {_esc(_age(vm["last_run_ts"], now))} · '
            f'{vm["last_run_ok"]}&nbsp;ok · {vm["last_run_failed"]}&nbsp;failed</div>'
        )
    else:
        badge = ('<div class="inline-flex items-center gap-2 rounded-full border border-white/10 '
                 'bg-white/5 px-3.5 py-1.5 text-xs text-slate-400">no sweep recorded</div>')

    tiles = "".join([
        _stat_tile(vm["total"], "topics", "text-slate-100"),
        _stat_tile(vm["healthy"], "healthy", "text-emerald-300",
                   "drop-shadow-[0_0_10px_rgba(16,185,129,.45)]"),
        _stat_tile(vm["failing"], "degraded",
                   "text-rose-300" if vm["failing"] else "text-slate-600",
                   "drop-shadow-[0_0_10px_rgba(244,63,94,.45)]" if vm["failing"] else ""),
        _stat_tile(vm["muted"], "muted",
                   "text-amber-300" if vm["muted"] else "text-slate-600"),
        _stat_tile(vm["queued"], "queued",
                   "text-cyan-300" if vm["queued"] else "text-slate-600",
                   "drop-shadow-[0_0_10px_rgba(34,211,238,.4)]" if vm["queued"] else ""),
    ])

    cards = "\n".join(_card(t) for t in vm["topics"]) or (
        '<p class="col-span-full text-center text-slate-500">No topics found in state.json.</p>')

    generated = now.strftime("%Y-%m-%d %H:%M UTC")

    header = (
        '<header class="mb-8 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">'
        "<div>"
        '<div class="mb-1 flex items-center gap-2.5">'
        '<span class="h-2.5 w-2.5 rounded-full bg-emerald-400 shadow-[0_0_12px_2px_rgba(16,185,129,.8)] animate-glow"></span>'
        '<span class="font-mono text-[11px] uppercase tracking-[0.35em] text-cyan-300/80">system status</span>'
        "</div>"
        '<h1 class="font-display text-4xl font-bold tracking-wide text-transparent sm:text-5xl '
        'bg-clip-text bg-gradient-to-r from-white via-slate-200 to-slate-400">NOTIFY&#8209;WATCHER</h1>'
        f'<p class="mt-1.5 text-sm text-slate-500">Live topic console · generated {_esc(generated)}</p>'
        "</div>"
        f"{badge}"
        "</header>"
    )

    summary = (
        '<section class="mb-10 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">'
        f"{tiles}</section>"
    )

    grid = (
        '<section class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">'
        f"{cards}</section>"
    )

    return _HEAD + header + summary + grid + _TAIL


# Static shell kept out of f-strings so the Tailwind config / CSS braces stay
# literal (no .format escaping). Dynamic HTML is concatenated in render().
_HEAD = """<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>notify-watcher · status</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {
    theme: {
      extend: {
        fontFamily: {
          display: ['Rajdhani', 'ui-sans-serif', 'sans-serif'],
          body: ['Inter', 'system-ui', 'sans-serif'],
          mono: ['ui-monospace', 'SFMono-Regular', 'monospace'],
        },
        keyframes: {
          glow: { '0%,100%': { opacity: '1' }, '50%': { opacity: '.5' } },
          pulseRing: {
            '0%': { boxShadow: '0 0 0 0 rgba(16,185,129,.5)' },
            '70%': { boxShadow: '0 0 0 7px rgba(16,185,129,0)' },
            '100%': { boxShadow: '0 0 0 0 rgba(16,185,129,0)' },
          },
          alertRing: {
            '0%': { boxShadow: '0 0 0 0 rgba(244,63,94,.55)' },
            '70%': { boxShadow: '0 0 0 7px rgba(244,63,94,0)' },
            '100%': { boxShadow: '0 0 0 0 rgba(244,63,94,0)' },
          },
        },
        animation: {
          glow: 'glow 3s ease-in-out infinite',
          'pulse-ring': 'pulseRing 2.6s ease-out infinite',
          'alert-ring': 'alertRing 1.6s ease-out infinite',
        },
      },
    },
  };
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  body { background-color: #05070d; }
  .bg-aurora {
    background-image:
      radial-gradient(900px 520px at 12% -12%, rgba(56,189,248,.10), transparent 60%),
      radial-gradient(820px 520px at 100% -5%, rgba(168,85,247,.10), transparent 55%),
      radial-gradient(760px 620px at 50% 118%, rgba(16,185,129,.07), transparent 60%);
    background-attachment: fixed;
  }
  .card-sheen { background-image: linear-gradient(155deg, rgba(255,255,255,.045), rgba(255,255,255,0) 42%); }

  /* --- wallpaper background layers (rotated/live media behind the UI) --- */
  /* #bg-layer holds the rotating image/video; left transparent so the aurora
     gradient on <body> shows through as a graceful fallback when no wallpaper
     is present. #bg-overlay is a dark scrim that keeps text readable over even
     a bright, high-detail fantasy wallpaper. Both sit BEHIND the content. */
  #bg-layer { position: fixed; inset: 0; z-index: -2; overflow: hidden; }
  #bg-overlay {
    position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background:
      linear-gradient(180deg, rgba(5,7,13,.72), rgba(5,7,13,.5) 42%, rgba(5,7,13,.84)),
      radial-gradient(1100px 700px at 50% -8%, rgba(5,7,13,0), rgba(5,7,13,.45));
  }
  .bg-media {
    position: absolute; inset: 0; width: 100%; height: 100%;
    object-fit: cover; opacity: 0; transition: opacity 1.1s ease-in-out;
    will-change: opacity;
  }
  .line-clamp-2 {
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 9999px; }
  ::-webkit-scrollbar-thumb:hover { background: #334155; }
</style>
</head>
<body class="bg-aurora min-h-screen font-body text-slate-200 antialiased selection:bg-cyan-500/30">
<div id="bg-layer" aria-hidden="true"></div>
<div id="bg-overlay" aria-hidden="true"></div>
<div class="mx-auto max-w-7xl px-5 py-10 sm:px-8">
"""

_TAIL = r"""
<footer class="mt-12 border-t border-white/5 pt-5 text-center text-xs text-slate-600">
  Static snapshot rendered by
  <span class="font-mono text-slate-500">notify_watcher/generate_dashboard.py</span>
  · read-only mirror of <span class="font-mono text-slate-500">state.json</span>
  · never affects notifications
</footer>
</div>

<!-- Wallpaper rotation: swaps the #bg-layer media every 5 minutes. Drop your
     own art into a sibling "wallpapers/" folder and list the files below.
     .mp4/.webm render as autoplaying muted loops; everything else as an image. -->
<script>
(function () {
  // --- configure your wallpapers here (paths are relative to dashboard.html) ---
  var WALLPAPERS = [
    'wallpapers/image1.jpg',
    'wallpapers/image2.jpg',
    'wallpapers/live.mp4'
  ];
  var ROTATE_MS = 5 * 60 * 1000;   // 5 minutes

  var layer = document.getElementById('bg-layer');
  if (!layer || !WALLPAPERS.length) return;

  function isVideo(path) { return /\.(mp4|webm)(\?.*)?$/i.test(path); }

  function build(src) {
    var el;
    if (isVideo(src)) {
      el = document.createElement('video');
      el.autoplay = true; el.loop = true; el.muted = true;
      el.playsInline = true; el.setAttribute('playsinline', '');
      el.setAttribute('preload', 'auto');
      el.src = src;
      el.play && el.play().catch(function () {});  // ignore autoplay rejections
    } else {
      el = document.createElement('img');
      el.alt = '';
      el.decoding = 'async';
      el.src = src;
    }
    el.className = 'bg-media';
    // A missing/broken file removes itself, so the aurora fallback shows through.
    el.addEventListener('error', function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    });
    return el;
  }

  function show(src) {
    var next = build(src);
    layer.appendChild(next);
    // Two frames so the browser registers opacity:0 before transitioning to 1.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { next.style.opacity = '1'; });
    });
    // Fade out and retire any previous media once the new one is in.
    var nodes = layer.querySelectorAll('.bg-media');
    for (var i = 0; i < nodes.length - 1; i++) {
      (function (old) {
        old.style.opacity = '0';
        setTimeout(function () {
          if (old.parentNode) old.parentNode.removeChild(old);
        }, 1300);
      })(nodes[i]);
    }
  }

  var idx = 0;
  show(WALLPAPERS[0]);
  setInterval(function () {
    idx = (idx + 1) % WALLPAPERS.length;
    show(WALLPAPERS[idx]);
  }, ROTATE_MS);
})();
</script>
</body>
</html>
"""


def build(out_path: Path = OUT_PATH) -> Path:
    """Read state.json + monitors.json from the repo root, write dashboard.html."""
    state = _load_json(STATE_PATH)
    monitors = _load_json(MONITORS_PATH)
    html_doc = render(state, monitors)
    out_path.write_text(html_doc, encoding="utf-8")
    log.info("dashboard written to %s", out_path)
    return out_path


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = build()
    topics = len((_load_json(STATE_PATH).get("topic_health") or {}))
    print(f"Wrote {out}  ({topics} topics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
