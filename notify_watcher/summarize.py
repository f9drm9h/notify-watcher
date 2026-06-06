"""Shared one-line AI summary helper used by topic modules.

Given a system instruction and a user text, return a single plain-text line,
or None to let the caller fall back to a non-AI body. Providers are tried in
preference order: Gemini (free tier) first, then Anthropic. If no provider key
is set or every call fails, returns None so a flaky/absent API never silences a
real alert.

Set GEMINI_API_KEY and/or ANTHROPIC_API_KEY as GitHub Actions secrets.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


def _gemini(system: str, user_text: str) -> str | None:
    """One-line summary via the free Gemini REST API. None on any failure."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user_text}]}],
        # Disable "thinking" so the small output budget isn't spent on reasoning.
        "generationConfig": {
            "maxOutputTokens": 256,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        resp = requests.post(url, params={"key": key}, json=payload, timeout=15.0)
        resp.raise_for_status()
        cands = resp.json().get("candidates") or []
        parts = (cands[0].get("content", {}).get("parts") if cands else None) or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - any failure → next provider
        log.warning("Gemini summary failed (%s); trying next provider", exc)
        return None


def _anthropic(system: str, user_text: str) -> str | None:
    """One-line summary via Claude. None on any failure."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.info("anthropic SDK not installed; skipping Claude summary")
        return None
    try:
        # Short timeout + single retry so a hung call falls back fast rather
        # than stalling the scheduled run (SDK default timeout is 10 minutes).
        client = anthropic.Anthropic(max_retries=1)
        resp = client.with_options(timeout=15.0).messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=256,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as exc:  # noqa: BLE001 - any failure → headline fallback
        log.warning("Claude summary failed (%s); using fallback body", exc)
        return None
    return next((b.text for b in resp.content if b.type == "text"), "").strip() or None


def one_line(system: str, user_text: str) -> str | None:
    """Return a one-line AI summary, or None to fall back. Never raises."""
    for provider in (_gemini, _anthropic):
        summary = provider(system, user_text)
        if summary:
            return summary
    return None
