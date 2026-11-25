"""Tell the NEXRAD producer the app is in use.

Any request to the API -- page loads fetch aircraft, quakes and radar through
it -- counts as traffic. Pings are throttled per process and sent off the
request path, so a slow or absent producer never delays a response.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.error
import urllib.request

from starlette.types import ASGIApp, Receive, Scope, Send

LOGGER = logging.getLogger("orion.api.demand")

# Kubernetes probes hit /health too, but the GUI's own health check on page load
# is exactly the "someone is here" signal, so probes are told apart by agent.
PROBE_AGENTS = (b"kube-probe/",)
DEFAULT_THROTTLE_SECONDS = 30.0


def _post(url: str) -> None:
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=2):
            pass
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        LOGGER.debug("Demand ping to %s failed: %s", url, error)


class DemandMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        url: str,
        throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.app = app
        self.url = url
        self.throttle_seconds = throttle_seconds
        self.clock = clock
        self._last_sent: float | None = None
        self._tasks: set[asyncio.Future[None]] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._counts(scope):
            now = self.clock()
            if (
                self._last_sent is None
                or now - self._last_sent >= self.throttle_seconds
            ):
                self._last_sent = now
                task = asyncio.ensure_future(asyncio.to_thread(_post, self.url))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        await self.app(scope, receive, send)

    @staticmethod
    def _counts(scope: Scope) -> bool:
        if scope.get("method") == "OPTIONS":
            return False
        for name, value in scope.get("headers", []):
            if name == b"user-agent":
                return not value.startswith(PROBE_AGENTS)
        return True


def demand_url() -> str:
    return os.environ.get("ORION_NEXRAD_DEMAND_URL", "").strip()
