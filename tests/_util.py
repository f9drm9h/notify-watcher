"""Shared test helpers.

`capture_pushes` swaps notify_watcher.ntfy.push for a recorder so tests can
assert on what *would* be sent without hitting the network. The collectors call
``ntfy.push`` via the shared module object (``from . import ntfy``), so patching
the attribute on the module is seen by digest/news/monitor alike.
"""
from __future__ import annotations

import contextlib

from notify_watcher import ntfy


@contextlib.contextmanager
def capture_pushes():
    """Yield a list that collects each push() call's kwargs; restores on exit."""
    sent: list[dict] = []
    original = ntfy.push

    def _fake(**kwargs):
        sent.append(kwargs)

    ntfy.push = _fake  # type: ignore[assignment]
    try:
        yield sent
    finally:
        ntfy.push = original  # type: ignore[assignment]
