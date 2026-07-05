"""Topic: /research — on-demand article summary.

A pure no-op on scheduled runs: it only acts when NOTIFY_RESEARCH_URL is set,
which happens when the Discord /research command dispatches the watch workflow
with its research_url input. The article is fetched and stripped to readable
text, summarized in a fixed personal style by summarize.brief (same Gemini ->
Claude chain the learn/knowledge pushes use), and posted through the same emit
path as the learn topic. Its own priority rule (monitors.json, score 62)
clears the push threshold, and like learn it has no discord_delivery mapping,
so it lands in the same default CHANNEL_GENERAL — no new channel or secret.

Failure policy: run() never raises. A fetch/parse error or an unavailable
summarizer still posts a short "couldn't summarize that link" reply (the
command was user-initiated, so silence would read as a hang), and an article
too thin to summarize — a paywall shell or a JS-only page — posts an explicit
"couldn't get the full article" message instead of a garbage summary.

Input that is not a valid http(s) URL is rejected before any fetch, with a
reply that carries NO click_url: Discord rejects an embed whose url field is
not a well-formed http(s) URL with a 400, so junk input must never reach the
embed. The Worker already refuses to dispatch non-URL input; this is the
backstop for manual workflow_dispatch runs with a bad research_url.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .. import events, summarize

log = logging.getLogger(__name__)

ENV_KEY = "NOTIFY_RESEARCH_URL"

HEADERS = {
    "User-Agent": "notify-watcher/1.0 (personal article summarizer; +https://github.com/)"
}

# Extracted text is clipped before summarization; under MIN_WORDS the page is
# assumed to be a paywall shell / JS-only app and is not worth summarizing.
MAX_CHARS = 6000
MIN_WORDS = 200

# Elements that are navigation/boilerplate, never article prose.
_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside",
               "noscript", "form", "button")

STYLE = (
    "You summarize articles for a single reader. Follow these rules exactly:\n"
    "- Write in clear, plain English a smart non-expert can follow.\n"
    "- First sentence: the single most important point of the article.\n"
    "- Then 3 to 5 short sentences with the key supporting facts, in plain prose.\n"
    "- No hype, no marketing words, no filler, no em dashes.\n"
    "- Stay neutral and factual. If the piece is opinion or a press release, say so.\n"
    "- Optionally end with one short line on why it matters, only if it adds something.\n"
    "- Aim for 150 to 180 words total."
)

PAYWALL_MSG = "couldn't get the full article from that link (paywall or JS-only page)"
FAILURE_MSG = "couldn't summarize that link"
INVALID_URL_MSG = ("that doesn't look like a URL — give me a full http(s) link, "
                   "e.g. https://example.com/story")


def _valid_url(url: str) -> bool:
    """True only for an absolute http(s) URL with a host."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _extract(url: str) -> tuple[str, str]:
    """Fetch ``url`` and return (page title, readable text clipped to MAX_CHARS).

    Same fetch+parse shape as the other scraping topics: requests + Beautiful
    Soup, boilerplate elements dropped, <article> preferred over <body> when
    the page marks one up.
    """
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    root = soup.find("article") or soup.body or soup
    text = " ".join(root.get_text(" ", strip=True).split())
    return title, text[:MAX_CHARS]


def _post(state: dict, title: str, body: str, url: str | None) -> dict:
    """Send one reply through the engine's emit path (see module doc).

    ``url`` becomes the Discord embed's click link, so callers must only pass
    a validated http(s) URL — or None for the invalid-input rejection reply.
    """
    return events.emit(
        state,
        title=f"Research: {title}" if title else "Research",
        body=body,
        topic="research",
        severity="low",
        source="Research",
        click_url=url,
        tags="mag",
        legacy_priority="low",
        legacy_action="push",
    )


def run(state: dict) -> dict:
    url = os.environ.get(ENV_KEY, "").strip()
    if not url:
        return state

    try:
        if not _valid_url(url):
            log.warning("research: rejecting non-URL input %r", url)
            return _post(state, "", INVALID_URL_MSG, None)

        try:
            title, text = _extract(url)
        except Exception as exc:  # noqa: BLE001 - a bad link still gets a reply
            log.error("research: fetch/extract failed for %s: %s", url, exc)
            return _post(state, "", FAILURE_MSG, url)

        if len(text.split()) < MIN_WORDS:
            log.info("research: only %d words extracted from %s; skipping summary",
                     len(text.split()), url)
            return _post(state, title, PAYWALL_MSG, url)

        summary = summarize.brief(STYLE, text)
        if not summary:
            log.warning("research: no summarizer available for %s", url)
            return _post(state, title, FAILURE_MSG, url)
        return _post(state, title, summary, url)
    except Exception as exc:  # noqa: BLE001 - run() must never raise
        log.error("research failed for %s: %s", url, exc)
        return state
