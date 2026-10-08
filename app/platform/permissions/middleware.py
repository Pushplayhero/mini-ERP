"""Resource limits for the public credential-verification endpoint."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from math import ceil
from typing import Any

from starlette.responses import JSONResponse

LOGIN_BODY_MAX_BYTES = 16 * 1024
LOGIN_ATTEMPTS_PER_WINDOW = 10
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60.0
MAX_CONCURRENT_LOGIN_VERIFICATIONS = 4
_MAX_TRACKED_PEERS = 4096


class LoginRateLimiter:
    """Process-local sliding-window limit keyed by the direct ASGI peer."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}

    def allow(self, peer: str) -> tuple[bool, int]:
        now = self._clock()
        attempts = self._attempts.get(peer)
        if attempts is None:
            if len(self._attempts) >= _MAX_TRACKED_PEERS:
                self._prune(now)
            if len(self._attempts) >= _MAX_TRACKED_PEERS:
                oldest_peer = min(self._attempts, key=lambda key: self._attempts[key][0])
                del self._attempts[oldest_peer]
            attempts = self._attempts[peer] = deque()

        while attempts and now - attempts[0] >= LOGIN_RATE_LIMIT_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= LOGIN_ATTEMPTS_PER_WINDOW:
            retry_after = max(1, ceil(LOGIN_RATE_LIMIT_WINDOW_SECONDS - (now - attempts[0])))
            return False, retry_after

        attempts.append(now)
        return True, 0

    def _prune(self, now: float) -> None:
        for peer, attempts in tuple(self._attempts.items()):
            while attempts and now - attempts[0] >= LOGIN_RATE_LIMIT_WINDOW_SECONDS:
                attempts.popleft()
            if not attempts:
                del self._attempts[peer]


class LoginProtectionMiddleware:
    """Bound login request bytes and attempts before FastAPI parses credentials."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path", "").rstrip("/") != "/auth/login"
        ):
            await self.app(scope, receive, send)
            return

        content_lengths = [
            value for name, value in scope.get("headers", []) if name.lower() == b"content-length"
        ]
        if len(content_lengths) == 1:
            try:
                declared_length = int(content_lengths[0])
            except ValueError:
                declared_length = -1
            if declared_length > LOGIN_BODY_MAX_BYTES:
                await self._body_too_large(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > LOGIN_BODY_MAX_BYTES:
                await self._body_too_large(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive() -> Any:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _body_too_large(scope: dict[str, Any], receive: Any, send: Any) -> None:
        response = JSONResponse({"detail": "Login request body is too large."}, status_code=413)
        await response(scope, receive, send)
