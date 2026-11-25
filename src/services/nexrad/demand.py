"""Whether anyone is using ORION, so the producer only works at full rate then.

The API is a read-only consumer of the frame volume, so it cannot leave a
marker there. Instead it POSTs to a tiny HTTP listener in the producer whenever
the app sees traffic, and the producer keeps the five-minute cadence while that
traffic is recent and falls back to a slow background cadence otherwise.
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOGGER = logging.getLogger("orion.nexrad.demand")


class DemandTracker:
    def __init__(self, idle_after_seconds: float, clock=time.monotonic) -> None:
        self._idle_after = idle_after_seconds
        self._clock = clock
        self._last_seen: float | None = None
        self._lock = threading.Lock()
        self._woken = threading.Event()

    def record(self) -> None:
        with self._lock:
            was_active = self._is_active_locked()
            self._last_seen = self._clock()
        if not was_active:
            # Someone arrived while the producer was idling: cut the slow sleep
            # short so a fresh frame is on its way now rather than in 15 min.
            LOGGER.info("Demand resumed; switching to the active cadence")
            self._woken.set()

    def is_active(self) -> bool:
        with self._lock:
            return self._is_active_locked()

    def _is_active_locked(self) -> bool:
        return (
            self._last_seen is not None
            and self._clock() - self._last_seen < self._idle_after
        )

    def reset(self) -> None:
        self._woken.clear()

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; True if demand resumed and cut it short."""
        woken = self._woken.wait(max(0.0, seconds))
        self._woken.clear()
        return woken


def serve(tracker: DemandTracker, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/demand":
                self.send_error(404)
                return
            tracker.record()
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:
            if self.path.rstrip("/") != "/health":
                self.send_error(404)
                return
            body = b'{"active": true}' if tracker.is_active() else b'{"active": false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="demand", daemon=True).start()
    LOGGER.info("Listening for demand on :%s", port)
    return server
