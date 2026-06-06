"""Topic: FDA drug approvals via the openFDA API (free, no key).

openFDA exposes the Drugs@FDA dataset as JSON. We query approved submissions
newest-first and turn each approved submission into a monitor item, then hand
the batch to the shared collector engine for dedup/scoring/routing. openFDA
allows 240 requests/min without a key, and we make one request per run, so this
stays comfortably free.

Dedup id is "<application_number>:<submission_type><submission_number>", which
is stable per approval (an original approval and each later supplement get
distinct ids). Regulatory approvals carry the highest source weight, so a match
against a watch keyword typically lands them in the live-push tier; everything
else still flows through the deterministic scorer.

Config (monitors.json -> fda): `url` (the openFDA query) and `source_weight`.
"""
from __future__ import annotations

import logging

import requests

from .. import config, monitor

log = logging.getLogger(__name__)

STATE_KEY = "fda_seen_ids"
CAP = 200
DEFAULT_URL = (
    "https://api.fda.gov/drug/drugsfda.json"
    "?search=submissions.submission_status:AP"
    "&sort=submissions.submission_status_date:desc&limit=50"
)
HEADERS = {"User-Agent": "notify-watcher/1.0 (+https://github.com/) personal-use"}


def _brand(result: dict) -> str:
    """Best human label for an application: a product brand name, else sponsor."""
    for product in result.get("products") or []:
        name = (product.get("brand_name") or "").strip()
        if name:
            return name.title()
    return (result.get("sponsor_name") or "").strip().title() or "drug"


def _appl_digits(app_no: str) -> str:
    """The numeric part of an application number, for the Drugs@FDA URL."""
    return "".join(ch for ch in app_no if ch.isdigit())


def _items(payload: dict, allowed_prefixes: tuple[str, ...]) -> list[dict]:
    """One monitor item per application: its latest approved (AP) submission.

    Collapsing to the newest AP submission per application bounds the batch to
    the query's application limit (not its far larger submission count), so a
    single response can never overflow the dedup cap and re-alert old approvals.
    A brand-new approval is a new application number; a later supplement bumps
    the submission number, so each yields a distinct, stable id exactly once.

    `allowed_prefixes` filters by application type (e.g. NDA new drugs, BLA
    biologics). The default excludes ANDA generics, which are high-volume and
    low news value, so the live tier stays signal-rich.
    """
    out: list[dict] = []
    for result in payload.get("results") or []:
        app_no = result.get("application_number") or ""
        if not app_no or not app_no.upper().startswith(allowed_prefixes):
            continue
        approvals = [s for s in (result.get("submissions") or [])
                     if s.get("submission_status") == "AP"]
        if not approvals:
            continue
        latest = max(approvals, key=lambda s: (
            s.get("submission_status_date") or "",
            int(s.get("submission_number") or 0),
        ))
        stype = (latest.get("submission_type") or "").upper()
        snum = latest.get("submission_number") or ""
        brand = _brand(result)
        kind = "approves" if stype == "ORIG" else "updates approval for"
        out.append({
            "id": f"{app_no}:{stype}{snum}",
            "title": f"FDA {kind} {brand} ({app_no})",
            "url": "https://www.accessdata.fda.gov/scripts/cder/daf/"
                   f"index.cfm?event=overview.process&ApplNo={_appl_digits(app_no)}",
            "source": brand,
        })
    return out


def run(state: dict) -> dict:
    cfg = config.section("fda")
    url = cfg.get("url") or DEFAULT_URL
    weight = cfg.get("source_weight", "regulatory")
    allowed = tuple(t.upper() for t in (cfg.get("application_types") or ["NDA", "BLA"]))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        items = _items(resp.json(), allowed)
    except Exception as exc:  # noqa: BLE001 - a fetch/parse failure is non-fatal
        log.error("FDA fetch failed: %s", exc)
        return state

    log.info("FDA: %d approved submission(s) in response", len(items))
    return monitor.run_source(
        state,
        state_key=STATE_KEY,
        items=items,
        default_weight_key=weight,
        keywords=cfg.get("keywords") or [],
        scoring_cfg=config.section("scoring"),
        digest_cfg=config.section("digest"),
        cap=CAP,
        live_title_prefix="FDA",
    )
