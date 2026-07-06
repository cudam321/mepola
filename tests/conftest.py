"""Shared test fixtures.

Keep tests hermetic against the developer's real .env: Settings.load() (invoked inside some
API routes) loads .env into os.environ, and a real DASHBOARD_PASSWORD there would flip the
auth middleware on mid-suite and 401 unrelated tests. Auth tests that WANT a password set it
explicitly with monkeypatch.setenv inside the test body (which runs after this autouse clear).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_dashboard_password(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
