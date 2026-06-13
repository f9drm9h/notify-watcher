"""Test package setup.

Runtime drop audits write to a real repo-root audit.json in production so
GitHub Actions can commit it between ephemeral runners. Unit tests redirect
that file to a temp path at import time so normal routing tests never dirty the
working tree.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from notify_watcher import audit

_AUDIT_DIR = tempfile.TemporaryDirectory()
audit.AUDIT_PATH = Path(_AUDIT_DIR.name) / "audit.json"
