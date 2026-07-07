"""Pure-ASGI HTTP Basic auth for the public dashboard deploy.

Why raw ASGI (not BaseHTTPMiddleware): BaseHTTPMiddleware only sees `http` scopes,
so /ws would stay wide open on a public URL. This class gates BOTH `http` and
`websocket` scopes; everything else (lifespan) passes through untouched.

Password semantics:
- falsy password        -> passthrough entirely (local dev unchanged)
- password=None (default) -> re-read os.environ["DASHBOARD_PASSWORD"] on every
  request, so tests (and late env injection) can toggle auth after app import
- explicit non-empty str -> that fixed password

Auth: `Authorization: Basic base64(user:pass)` — the username is ignored, only
the password part is compared (constant-time). Failures get 401 +
`WWW-Authenticate: Basic realm="memebot"` so browsers prompt natively; failed
websocket upgrades are closed with code 4401 without being accepted.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import os

_ENV_VAR = "DASHBOARD_PASSWORD"


class BasicAuthMiddleware:
    def __init__(self, app, password: str | None = None,
                 exempt_paths: tuple[str, ...] = ("/api/health",)) -> None:
        self.app = app
        self._password = password
        self.exempt_paths = tuple(exempt_paths)

    def _current_password(self) -> str:
        if self._password is None:
            return os.environ.get(_ENV_VAR, "")
        return self._password

    @staticmethod
    def _authorized(scope, password: str) -> bool:
        auth = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value
                break
        if auth is None:
            return False
        scheme, _, b64 = auth.partition(b" ")
        if scheme.lower() != b"basic":
            return False
        try:
            decoded = base64.b64decode(b64.strip(), validate=True).decode("utf-8")
        except Exception:
            return False
        _username, _, candidate = decoded.partition(":")
        return hmac.compare_digest(candidate.encode("utf-8"), password.encode("utf-8"))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        password = self._current_password()
        if not password:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            if scope.get("path") in self.exempt_paths or self._authorized(scope, password):
                await self.app(scope, receive, send)
                return
            # re-audit: throttle online password guessing against a public real-money control
            # plane — a flat 1s delay on every failed attempt caps brute force at ~86k/day
            # without any per-IP state (single-operator tool; the delay is invisible to humans).
            await asyncio.sleep(1.0)
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b'Basic realm="memebot"'),
                    (b"content-type", b"text/plain; charset=utf-8"),
                ],
            })
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        # websocket: browsers reuse the page's Basic credentials on the same-origin upgrade
        if self._authorized(scope, password):
            await self.app(scope, receive, send)
            return
        await asyncio.sleep(1.0)                 # same brute-force throttle as http
        await send({"type": "websocket.close", "code": 4401})
