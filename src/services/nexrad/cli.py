"""Command line entrypoint for the ORION NEXRAD mosaic service.

Installed as ``orion-nexrad``. Run with no arguments it publishes a frame every
``ORION_NEXRAD_INTERVAL_SECONDS``; ``--once`` produces a single frame, and
``--analysis-time`` rebuilds a frame for a past moment.

With ``ORION_NEXRAD_DEMAND_PORT`` set it listens for demand pings from the API
and slows to ``ORION_NEXRAD_IDLE_INTERVAL_SECONDS`` while nobody is using the
app, waking immediately when someone comes back.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import UTC, datetime

from services.nexrad.demand import DemandTracker, serve
from services.nexrad.frames import produce_frame
from services.nexrad.retention import prune_raw_scans
from services.nexrad.settings import ProducerSettings

LOGGER = logging.getLogger("orion.nexrad")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="orion-nexrad", description="Publish ORION NEXRAD mosaic frames"
    )
    parser.add_argument(
        "--once", action="store_true", help="Produce one frame and exit"
    )
    parser.add_argument("--analysis-time", type=_parse_time)
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = ProducerSettings.from_environment()
    tracker: DemandTracker | None = None
    if settings.demand_port and not (args.once or args.analysis_time):
        tracker = DemandTracker(settings.idle_after_seconds)
        serve(tracker, settings.demand_port)

    while True:
        started = time.monotonic()
        try:
            produce_frame(settings, args.analysis_time)
        except Exception:
            LOGGER.exception("NEXRAD frame production failed")
            if args.once:
                raise
        finally:
            # Reclaim on every cycle, not just successful ones. A producer that
            # keeps failing, or that keeps finding the frame already built, still
            # downloads a volume per station and would otherwise fill the disk.
            try:
                prune_raw_scans(settings)
            except Exception:
                LOGGER.exception("Could not prune cached scans")
        if args.once or args.analysis_time:
            return
        # Sleep only what is left of the interval. A flat sleep after the work
        # made the real period `interval + cycle`, so the cadence drifted by a
        # cycle's runtime every pass and frames arrived progressively later.
        # An overrunning cycle starts the next one immediately -- but never
        # sooner than the interval: volumes only land every few minutes, and
        # this is an unsigned public bucket, so polling harder buys no fresher
        # data and just spends NOAA's bandwidth.
        if tracker is None or tracker.is_active():
            remaining = settings.interval_seconds - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
            continue
        remaining = settings.idle_interval_seconds - (time.monotonic() - started)
        LOGGER.info("No recent demand; next frame in %.0fs", max(0, remaining))
        # A demand ping during the idle sleep starts the next cycle at once. If
        # the newest scans are already in the last frame, that cycle costs one
        # listing and finds the frame already published.
        tracker.reset()
        tracker.wait(remaining)


if __name__ == "__main__":
    main()
