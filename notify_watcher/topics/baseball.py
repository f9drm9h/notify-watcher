"""Topic: Dominican baseball — followed-team results + DR player milestone alerts.

Two independent checks against the free MLB Stats API (statsapi.mlb.com, JSON,
no key), configured in monitors.json -> "baseball":

1. Followed-team daily results (daily run only -> digest). Each daily run we
   fetch yesterday's schedule for every club in `monitored_teams` (or the
   legacy single `team_id`/`team_name` pair) and put each final score in the
   morning digest as one line: "Dodgers 5 - Cubs 3 (W)". An off day (no game)
   or a game that isn't Final is silently skipped. Already-reported gamePks
   are tracked in state so the repeated post-noon runs (NOTIFY_DAILY is set on
   every run after the daily threshold) never duplicate a line.

2. Dominican player milestones (every run -> live push). For each player in
   `dominican_players` we read this season's hitting game log and push once
   when they have a milestone game — a home run, 3+ hits, or 3+ RBI:
   "🇩🇴 Juan Soto — 2 HR, 4 RBI vs Yankees". Alerted gamePks are tracked in
   state["baseball_milestones_seen"], so one game alerts at most once; each
   player is isolated so a single bad lookup never blocks the rest.

Season gating: when MLB as a whole has played no games for several consecutive
days (off-season; the longest in-season league-wide pause is the ~4-day
All-Star break) both checks skip silently instead of erroring on empty data.

LIDOM (the Dominican winter league, e.g. Tigres del Licey) publishes no free
API, so the followed teams default to MLB clubs — edit `monitored_teams` to
taste; ids come from https://statsapi.mlb.com/api/v1/teams?sportId=1.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

import requests

from .. import config, events

log = logging.getLogger(__name__)

SCHEDULE_API = "https://statsapi.mlb.com/api/v1/schedule"
GAMELOG_API = "https://statsapi.mlb.com/api/v1/people/{person_id}/stats"
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}

RESULTS_SEEN_KEY = "baseball_results_seen"        # [gamePk, ...] already digested
MILESTONES_SEEN_KEY = "baseball_milestones_seen"  # [gamePk, ...] already alerted
RESULTS_CAP = 30
MILESTONES_CAP = 200

# No MLB games in this many trailing days = off-season; both checks skip.
OFFSEASON_WINDOW_DAYS = 7

# Milestone thresholds for a single game's hitting line.
MIN_HR = 1
MIN_HITS = 3
MIN_RBI = 3


def _schedule_games(params: dict) -> list[dict]:
    """Flattened game objects from one schedule query (all dates merged)."""
    resp = requests.get(
        SCHEDULE_API, params={"sportId": 1, **params}, headers=HEADERS, timeout=30,
    )
    resp.raise_for_status()
    games: list[dict] = []
    for d in resp.json().get("dates") or []:
        games.extend(d.get("games") or [])
    return games


def _season_active(today: _dt.date) -> bool:
    """True when MLB played at least one game in the trailing window.

    One league-wide schedule query (no teamId) covering the last
    OFFSEASON_WINDOW_DAYS; an empty answer means the season is over (or not
    yet started) and the caller skips everything silently.
    """
    start = today - _dt.timedelta(days=OFFSEASON_WINDOW_DAYS)
    return bool(_schedule_games(
        {"startDate": start.isoformat(), "endDate": today.isoformat()}
    ))


def _result_line(game: dict, team_id, team_name: str) -> str | None:
    """Pure: "Dodgers 5 - Cubs 3 (W)" for a Final game, else None.

    None for a game that isn't Final (in progress, postponed, suspended), that
    the followed team isn't in, or whose scores are missing — the caller just
    skips it without recording the gamePk, so a suspended game that finalizes
    later can still be reported.
    """
    if ((game.get("status") or {}).get("abstractGameState") or "") != "Final":
        return None
    teams = game.get("teams") or {}
    home, away = teams.get("home") or {}, teams.get("away") or {}

    def _tid(side: dict):
        return (side.get("team") or {}).get("id")

    if str(_tid(home)) == str(team_id):
        us, them = home, away
    elif str(_tid(away)) == str(team_id):
        us, them = away, home
    else:
        return None
    us_score, them_score = us.get("score"), them.get("score")
    if us_score is None or them_score is None:
        return None
    opp = (them.get("team") or {})
    opp_name = opp.get("teamName") or opp.get("name") or "?"
    wl = "W" if us_score > them_score else ("L" if us_score < them_score else "T")
    return f"{team_name} {us_score} – {opp_name} {them_score} ({wl})"


def _monitored_teams(cfg: dict) -> list[tuple]:
    """(team_id, team_name) pairs from `monitored_teams`, else the legacy single keys."""
    teams = []
    for entry in cfg.get("monitored_teams") or []:
        if isinstance(entry, dict) and entry.get("team_id"):
            teams.append((entry["team_id"], entry.get("team_name") or "Team"))
    if not teams and cfg.get("team_id"):
        teams.append((cfg["team_id"], cfg.get("team_name") or "Team"))
    return teams


def _check_team_result(state: dict, cfg: dict, yesterday: _dt.date) -> dict:
    """Digest yesterday's final score for each followed team, once per gamePk."""
    teams = _monitored_teams(cfg)
    if not teams:
        return state

    seen = list(state.get(RESULTS_SEEN_KEY) or [])
    for team_id, team_name in teams:
        try:
            games = _schedule_games({
                "date": yesterday.isoformat(),
                "teamId": team_id,
                "hydrate": "team,linescore",
            })
        except Exception as exc:  # noqa: BLE001 - one team failing is non-fatal
            log.error("baseball team result fetch failed for %s: %s", team_name, exc)
            continue
        if not games:
            log.info("baseball: no %s game on %s (off day)", team_name, yesterday)
            continue

        for game in games:  # usually one; a doubleheader yields two lines
            pk = game.get("gamePk")
            if pk is None or pk in seen:
                continue
            line = _result_line(game, team_id, team_name)
            if line is None:
                continue
            state = events.emit(
                state,
                title=line,
                body=f"Final, {yesterday.isoformat()}",
                topic="baseball",
                severity="low",
                source="Baseball",
                tags="baseball",
                legacy_action="digest",
                score=4,
            )
            seen.append(pk)
    state[RESULTS_SEEN_KEY] = seen[-RESULTS_CAP:]
    return state


def _game_log_splits(person_id, season: int) -> list[dict]:
    """A player's per-game hitting splits for one season (newest data last)."""
    resp = requests.get(
        GAMELOG_API.format(person_id=person_id),
        params={"stats": "gameLog", "season": season, "group": "hitting"},
        headers=HEADERS, timeout=30,
    )
    resp.raise_for_status()
    stats = resp.json().get("stats") or []
    return (stats[0].get("splits") or []) if stats else []


def _milestone_parts(stat: dict) -> list[str]:
    """Pure: the milestone fragments of one game's hitting line, e.g. ["2 HR", "4 RBI"].

    Each stat appears only when it clears its own bar (any HR, 3+ hits,
    3+ RBI), so a routine 1-for-4 night returns [] and stays silent.
    """
    parts: list[str] = []
    hr = int(stat.get("homeRuns") or 0)
    hits = int(stat.get("hits") or 0)
    rbi = int(stat.get("rbi") or 0)
    if hr >= MIN_HR:
        parts.append(f"{hr} HR")
    if hits >= MIN_HITS:
        parts.append(f"{hits} hits")
    if rbi >= MIN_RBI:
        parts.append(f"{rbi} RBI")
    return parts


def _check_milestones(state: dict, cfg: dict, today: _dt.date) -> dict:
    """Push once per game when a followed Dominican player has a milestone night."""
    players = cfg.get("dominican_players") or []
    if not players:
        return state

    seen = list(state.get(MILESTONES_SEEN_KEY) or [])
    # The game log dates games by their US official date; against the runner's
    # UTC clock a night game finishing after midnight UTC lands on "yesterday",
    # so both days are checked (the seen-PK set keeps the overlap alert-free).
    days = {today.isoformat(), (today - _dt.timedelta(days=1)).isoformat()}

    for player in players:
        name = (player.get("name") if isinstance(player, dict) else None) or str(player)
        try:
            pid = player.get("person_id")
            if not pid:
                continue
            for split in _game_log_splits(pid, today.year):
                if split.get("date") not in days:
                    continue
                pk = (split.get("game") or {}).get("gamePk")
                if pk is None or pk in seen:
                    continue
                parts = _milestone_parts(split.get("stat") or {})
                if not parts:
                    continue  # pk not recorded: a live game can still grow into one
                opp = split.get("opponent") or {}
                opp_name = opp.get("teamName") or opp.get("name") or ""
                vs = f" vs {opp_name}" if opp_name else ""
                state = events.emit(
                    state,
                    title=f"Baseball: {name}",
                    body=f"🇩🇴 {name} — {', '.join(parts)}{vs}",
                    topic="baseball",
                    severity="high",
                    source="Baseball",
                    tags="baseball",
                    legacy_action="push",
                )
                seen.append(pk)
        except Exception as exc:  # noqa: BLE001 - isolate each player
            log.error("baseball player %r check failed: %s", name, exc)

    state[MILESTONES_SEEN_KEY] = seen[-MILESTONES_CAP:]
    return state


def run(state: dict) -> dict:
    """Player milestones every run; the team's previous-day result on the daily run."""
    cfg = config.section("baseball")
    if not cfg:
        log.info("no baseball section in monitors.json; nothing to do")
        return state

    today = _dt.date.today()
    try:
        if not _season_active(today):
            log.info("baseball: no MLB games in the last %d days (off-season); skipping",
                     OFFSEASON_WINDOW_DAYS)
            return state
    except Exception as exc:  # noqa: BLE001 - a dead season check skips the run
        log.warning("baseball season check failed: %s; skipping this run", exc)
        return state

    state = _check_milestones(state, cfg, today)
    if os.environ.get("NOTIFY_DAILY"):
        state = _check_team_result(state, cfg, today - _dt.timedelta(days=1))
    return state
