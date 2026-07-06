"""Dashboard Basic-auth tests — DASHBOARD_PASSWORD gates everything except /api/health."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root: `dashboard` is not installed

import dashboard.server.app as appmod  # noqa: E402
from memebot.live.state import LiveState  # noqa: E402

PASSWORD = "hunter2"


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "live_state.db"
    LiveState(db).close()  # seed: DDL + mode=paper, kill_switch=off
    monkeypatch.setattr(appmod, "DB_PATH", str(db))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture()
def locked_client(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    return client


def test_no_password_local_dev_unchanged(client):
    r = client.get("/api/snapshot")
    assert r.status_code == 200


def test_password_set_no_auth_401_with_browser_prompt(locked_client):
    r = locked_client.get("/api/snapshot")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == 'Basic realm="memebot"'


def test_correct_password_any_username_ok(locked_client):
    assert locked_client.get("/api/snapshot", headers=_basic("alice", PASSWORD)).status_code == 200
    assert locked_client.get("/api/snapshot", headers=_basic("", PASSWORD)).status_code == 200


def test_wrong_password_401(locked_client):
    r = locked_client.get("/api/snapshot", headers=_basic("alice", "wrong"))
    assert r.status_code == 401


def test_health_exempt_for_railway_healthcheck(locked_client):
    r = locked_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_websocket_rejected_without_auth(locked_client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with locked_client.websocket_connect("/ws"):
            pass
    assert exc.value.code == 4401


def test_websocket_with_auth_receives_snapshot(locked_client):
    with locked_client.websocket_connect("/ws", headers=_basic("any", PASSWORD)) as ws:
        msg = ws.receive_json()
    assert msg["type"] == "snapshot"
    assert "payload" in msg
