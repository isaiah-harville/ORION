from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger("orion.adsb")

# adsb.lol aggregates a volunteer receiver network and serves it without an API
# key or account, which is what makes it usable from a self-hosted ORION that
# has no credentials to hand out. Overridable because the same route shape is
# served by airplanes.live and by a private readsb instance.
BASE_URL = os.environ.get("ORION_ADSB_BASE_URL", "https://api.adsb.lol/v2")
USER_AGENT = "ORION/1.0 (+https://orion.harville.dev)"

# The upstream caps a query at 250 nautical miles, so asking for more is an
# error rather than a wider answer.
MAX_RADIUS_NM = 250
DEFAULT_RADIUS_NM = 150

# Aircraft positions are quantised before they become a cache key. Two browsers
# panned a few hundred metres apart are looking at the same sky, and without
# this every pixel of map movement would be a fresh upstream request.
GRID_DEGREES = 0.25


@dataclass(frozen=True)
class FeedKey:
    lat: float
    lon: float
    radius_nm: int


class FeedError(RuntimeError):
    """The upstream feed could not be read and nothing cached can stand in."""


class _Entry:
    __slots__ = ("fetched_at", "payload")

    def __init__(self, payload: dict[str, Any], fetched_at: float) -> None:
        self.payload = payload
        self.fetched_at = fetched_at


_CACHE: dict[FeedKey, _Entry] = {}
_LOCK = threading.Lock()


def cache_ttl_seconds() -> float:
    """How long one upstream answer is reused.

    Shorter than the earthquake feed because a jet at 500 knots moves about two
    nautical miles in fifteen seconds -- roughly a marker's width on a regional
    view, so a longer TTL would show visibly lagging traffic.
    """
    return float(os.environ.get("ORION_ADSB_CACHE_SECONDS", "15"))


def cache_key(lat: float, lon: float, radius_nm: int) -> FeedKey:
    """Snap a viewport to the shared grid so nearby clients share one fetch."""
    return FeedKey(
        lat=round(lat / GRID_DEGREES) * GRID_DEGREES,
        lon=round(lon / GRID_DEGREES) * GRID_DEGREES,
        radius_nm=radius_nm,
    )


def _request(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _number(value: Any) -> float | None:
    """Coerce a numeric field, tolerating the feed's string sentinels.

    Barometric altitude in particular arrives as the string "ground" for an
    aircraft that is not flying.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _normalise(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten to the fields the map draws.

    The raw records carry forty-odd signal-integrity fields per aircraft; a busy
    250 nm query is megabytes of them and kilobytes of what is actually shown.
    """
    aircraft: list[dict[str, Any]] = []
    for entry in document.get("ac") or []:
        lat = _number(entry.get("lat"))
        lon = _number(entry.get("lon"))
        if lat is None or lon is None:
            continue
        # The ICAO 24-bit address is the only identifier every aircraft has;
        # callsign and registration are both frequently absent.
        icao = (entry.get("hex") or "").strip()
        if not icao:
            continue
        callsign = (entry.get("flight") or "").strip() or None
        altitude = entry.get("alt_baro")
        aircraft.append(
            {
                "id": icao,
                "callsign": callsign,
                "registration": (entry.get("r") or "").strip() or None,
                "aircraft_type": (entry.get("t") or "").strip() or None,
                "latitude": lat,
                "longitude": lon,
                # "ground" rather than a number means the aircraft is on the
                # surface; the map draws that differently from an unknown level.
                "altitude_ft": _number(altitude),
                "on_ground": altitude == "ground",
                "ground_speed_kt": _number(entry.get("gs")),
                "track_deg": _number(entry.get("track")),
                "vertical_rate_fpm": _number(
                    entry.get("baro_rate") or entry.get("geom_rate")
                ),
                "squawk": entry.get("squawk"),
                "emergency": (
                    entry.get("emergency")
                    if entry.get("emergency") not in (None, "none")
                    else None
                ),
                # Seconds since this position was last heard, so the client can
                # fade contacts that have gone quiet.
                "seen_pos_s": _number(entry.get("seen_pos")),
                "distance_nm": _number(entry.get("dst")),
            }
        )
    aircraft.sort(key=lambda item: item["distance_nm"] or math.inf)
    return aircraft


def _with_age(entry: _Entry, now: float) -> dict[str, Any]:
    """Stamp how stale this answer is at the moment it is served.

    Without it a client cannot tell a fresh fetch from one served from cache,
    and every position it draws is silently up to a TTL behind. The client adds
    this to each aircraft's own seen_pos to get the real age of a position.
    """
    return {**entry.payload, "feed_age_seconds": round(now - entry.fetched_at, 1)}


# After a failed fetch a circle is not retried upstream for this long; the last
# good copy (or nothing) is served instead. Without it every request for a
# failing circle went straight back to the upstream, which under load turns
# one refusal into hundreds.
FAILURE_BACKOFF_SECONDS = float(
    os.environ.get("ORION_ADSB_FAILURE_BACKOFF_SECONDS", "30")
)
# A 429 pauses every upstream call for the Retry-After it names, or this long.
RATE_LIMIT_BACKOFF_SECONDS = 60.0

_FAILED_AT: dict[FeedKey, float] = {}
_KEY_LOCKS: dict[FeedKey, threading.Lock] = {}
_rate_limited_until = 0.0


def upstream_paused() -> bool:
    """Whether the upstream asked us to back off and that window is open."""
    return time.monotonic() < _rate_limited_until


def _key_lock(key: FeedKey) -> threading.Lock:
    with _LOCK:
        return _KEY_LOCKS.setdefault(key, threading.Lock())


def _note_failure(key: FeedKey, error: Exception) -> None:
    global _rate_limited_until
    now = time.monotonic()
    with _LOCK:
        _FAILED_AT[key] = now
    if isinstance(error, urllib.error.HTTPError) and error.code == 429:
        retry_after = RATE_LIMIT_BACKOFF_SECONDS
        try:
            retry_after = max(1.0, float(error.headers.get("Retry-After", retry_after)))
        except (TypeError, ValueError):
            pass
        _rate_limited_until = max(_rate_limited_until, now + retry_after)
        LOGGER.warning("ADS-B upstream rate limited us; pausing for %.0fs", retry_after)


def _stale_or_raise(key: FeedKey, reason: str) -> dict[str, Any]:
    with _LOCK:
        stale = _CACHE.get(key)
    if stale is not None:
        return _with_age(stale, time.monotonic())
    raise FeedError(reason)


def fetch_aircraft(key: FeedKey, timeout: float = 10.0) -> dict[str, Any]:
    """Aircraft near a point, cached for the TTL.

    One upstream request per circle at a time: concurrent callers wait for it
    and share the result. A stale entry is served if the upstream fails, so a
    feed blip leaves slightly old traffic on the map rather than clearing it,
    and a failing or rate-limiting upstream is left alone for a while rather
    than retried on every request.
    """
    now = time.monotonic()
    with _LOCK:
        entry = _CACHE.get(key)
        if entry and now - entry.fetched_at < cache_ttl_seconds():
            return _with_age(entry, now)

    with _key_lock(key):
        # Whoever held the lock may just have refreshed this circle.
        now = time.monotonic()
        with _LOCK:
            entry = _CACHE.get(key)
            failed_at = _FAILED_AT.get(key)
        if entry and now - entry.fetched_at < cache_ttl_seconds():
            return _with_age(entry, now)
        if upstream_paused():
            return _stale_or_raise(key, "ADS-B upstream asked us to back off")
        if failed_at is not None and now - failed_at < FAILURE_BACKOFF_SECONDS:
            return _stale_or_raise(key, "ADS-B circle failed recently")

        url = f"{BASE_URL}/lat/{key.lat}/lon/{key.lon}/dist/{key.radius_nm}"
        try:
            document = _request(url, timeout)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            _note_failure(key, error)
            LOGGER.warning("ADS-B fetch failed (%s)", error)
            return _stale_or_raise(key, str(error))
        with _LOCK:
            _FAILED_AT.pop(key, None)
        return _store(key, document)


def _store(key: FeedKey, document: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "latitude": key.lat,
        "longitude": key.lon,
        "radius_nm": key.radius_nm,
        # The feed timestamps itself in milliseconds since the epoch.
        "generated_at": document.get("now"),
        "attribution": {
            "text": "Flight data: adsb.lol community receiver network",
            "url": "https://adsb.lol/",
        },
        "aircraft": _normalise(document),
    }
    payload["count"] = len(payload["aircraft"])

    entry = _Entry(payload, time.monotonic())
    with _LOCK:
        _CACHE[key] = entry
    return _with_age(entry, entry.fetched_at)


# Viewport coverage. The upstream only answers circles of up to 250 nm, so a
# viewport is covered by several of them, centred on a fixed global grid so that
# every client panning across the same sky reuses the same cached fetches.
TILE_RADIUS_NM = MAX_RADIUS_NM
# Five degrees of latitude is 300 nm. Each row's longitude step is widened by
# 1/cos(latitude) of its poleward edge so the cells stay about 300 nm wide; a
# cell's half-diagonal then stays under the 250 nm radius up to about 75 deg,
# so the circles leave no gaps.
TILE_LAT_STEP = 5.0
# Circles fetched live for a viewport. Six covers a multi-state view.
MAX_AREA_TILES = 6
# A wider view -- up to the whole continental US, about 45 circles -- is not
# fetched live: the circles past the first six are filled in by a background
# worker at a polite fixed rate and served from cache, so a zoomed-out map
# fills in over a minute rather than firing dozens of upstream requests per
# refresh. Beyond this many circles the outermost go uncovered.
WIDE_MAX_TILES = 80
# How old a background-filled circle may be and still be drawn. Clients dead
# reckon from each report's age, so older reports are still placed sensibly.
WIDE_MAX_AGE_SECONDS = float(os.environ.get("ORION_ADSB_WIDE_MAX_AGE_SECONDS", "150"))
# The background worker re-fetches a wanted circle once it is this old...
WIDE_REFRESH_SECONDS = float(os.environ.get("ORION_ADSB_WIDE_REFRESH_SECONDS", "60"))
# ...one upstream request at a time, this far apart...
BACKGROUND_INTERVAL_SECONDS = float(
    os.environ.get("ORION_ADSB_BACKGROUND_INTERVAL_SECONDS", "1.0")
)
# ...and stops wanting a circle once no viewport has asked for it this long.
WANTED_FOR_SECONDS = 60.0
# Aircraft just outside the viewport are kept so a small pan is not empty at
# the edges until the next refresh.
AREA_MARGIN = 0.25


def _row_lon_step(lat_centre: float) -> float:
    edge = min(abs(lat_centre) + TILE_LAT_STEP / 2, 80.0)
    return TILE_LAT_STEP / math.cos(math.radians(edge))


def area_tiles(
    west: float,
    south: float,
    east: float,
    north: float,
    limit: int = MAX_AREA_TILES,
) -> tuple[list[FeedKey], bool]:
    """Grid circles covering a viewport, nearest its centre first.

    Returns the circles to fetch and whether the viewport needed more than
    `limit` of them, in which case its outer parts go uncovered.
    """
    west, east = max(west, -180.0), min(east, 180.0)
    south, north = max(south, -85.0), min(north, 85.0)
    centre_lat, centre_lon = (south + north) / 2, (west + east) / 2

    keys: list[FeedKey] = []
    first_row = math.floor((south + 90) / TILE_LAT_STEP)
    last_row = math.floor((north + 90) / TILE_LAT_STEP)
    for row in range(first_row, last_row + 1):
        lat = -90 + TILE_LAT_STEP * (row + 0.5)
        step = _row_lon_step(lat)
        for column in range(
            math.floor((west + 180) / step), math.floor((east + 180) / step) + 1
        ):
            lon = -180 + step * (column + 0.5)
            keys.append(FeedKey(round(lat, 4), round(lon, 4), TILE_RADIUS_NM))

    def distance(key: FeedKey) -> float:
        dx = (key.lon - centre_lon) * math.cos(math.radians(centre_lat))
        return math.hypot(key.lat - centre_lat, dx)

    keys.sort(key=distance)
    return keys[:limit], len(keys) > limit


def fetch_area(west: float, south: float, east: float, north: float) -> dict[str, Any]:
    """Every aircraft over a viewport, merged from the covering grid circles.

    Circles overlap, so an aircraft can come back from several; the freshest
    report wins. Each circle may have been served from cache at a different
    age, so that age is folded into each aircraft's own seen_pos_s and the
    merged answer reports a feed age of zero.
    """
    keys, truncated = area_tiles(west, south, east, north, limit=WIDE_MAX_TILES)
    live, background = keys[:MAX_AREA_TILES], keys[MAX_AREA_TILES:]
    with ThreadPoolExecutor(max_workers=len(live)) as executor:
        results = list(executor.map(_fetch_or_none, live))
    if background:
        FILLER.want(background)
        results += [_cached(key, WIDE_MAX_AGE_SECONDS) for key in background]
    answered = [result for result in results if result is not None]
    if not answered:
        raise FeedError("No part of the area could be fetched")

    pad_lat = (north - south) * AREA_MARGIN
    pad_lon = (east - west) * AREA_MARGIN
    best: dict[str, dict[str, Any]] = {}
    for payload in answered:
        feed_age = float(payload.get("feed_age_seconds") or 0.0)
        for aircraft in payload["aircraft"]:
            if not (
                south - pad_lat <= aircraft["latitude"] <= north + pad_lat
                and west - pad_lon <= aircraft["longitude"] <= east + pad_lon
            ):
                continue
            age = feed_age + (aircraft["seen_pos_s"] or 0.0)
            current = best.get(aircraft["id"])
            if current is None or age < current["seen_pos_s"]:
                # Distance is relative to whichever circle answered, which
                # means nothing to the client once circles are merged.
                best[aircraft["id"]] = {
                    **aircraft,
                    "seen_pos_s": round(age, 1),
                    "distance_nm": None,
                }

    return {
        "bounds": [west, south, east, north],
        "generated_at": max((p.get("generated_at") or 0) for p in answered),
        "attribution": answered[0]["attribution"],
        "aircraft": list(best.values()),
        "count": len(best),
        "feed_age_seconds": 0.0,
        "circles": len(keys),
        "circles_answered": len(answered),
        # Circles of this view the background worker has not filled yet; the
        # client polls sooner while this is non-zero.
        "pending": len(keys) - len(answered),
        "truncated": truncated,
    }


def _cached(key: FeedKey, max_age: float) -> dict[str, Any] | None:
    now = time.monotonic()
    with _LOCK:
        entry = _CACHE.get(key)
    if entry is None or now - entry.fetched_at > max_age:
        return None
    return _with_age(entry, now)


def _cache_age(key: FeedKey) -> float:
    with _LOCK:
        entry = _CACHE.get(key)
    return math.inf if entry is None else time.monotonic() - entry.fetched_at


class BackgroundFiller:
    """Keeps wide-view circles fresh, one upstream request at a time.

    Viewports register the circles they need; the worker repeatedly fetches
    whichever wanted circle is missing or oldest, nearest a viewport's centre
    first, and forgets circles nobody has asked for recently. With nobody
    looking at a wide view it makes no requests at all.
    """

    def __init__(self) -> None:
        self._wanted: dict[FeedKey, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def want(self, keys: list[FeedKey]) -> None:
        now = time.monotonic()
        with self._lock:
            for rank, key in enumerate(keys):
                previous = self._wanted.get(key)
                best_rank = rank if previous is None else min(rank, previous[0])
                self._wanted[key] = (best_rank, now)
        self.start()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="adsb-filler", daemon=True
            )
            self._thread.start()

    def next_key(self) -> FeedKey | None:
        if upstream_paused():
            return None
        now = time.monotonic()
        with self._lock:
            for key, (_, asked_at) in list(self._wanted.items()):
                if now - asked_at > WANTED_FOR_SECONDS:
                    del self._wanted[key]
            wanted = dict(self._wanted)
        candidates = [
            (rank, key)
            for key, (rank, _) in wanted.items()
            if _cache_age(key) > WIDE_REFRESH_SECONDS
        ]
        if not candidates:
            return None
        # Circles never fetched come before ones merely due a refresh; within
        # each, the one nearest a viewport's centre goes first.
        missing = [item for item in candidates if _cache_age(item[1]) == math.inf]
        return min(missing or candidates)[1]

    def run_once(self) -> bool:
        key = self.next_key()
        if key is None:
            return False
        _fetch_or_none(key)
        return True

    def _run(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("ADS-B background fill failed")
            time.sleep(BACKGROUND_INTERVAL_SECONDS)

    def clear(self) -> None:
        with self._lock:
            self._wanted.clear()


FILLER = BackgroundFiller()


def _fetch_or_none(key: FeedKey) -> dict[str, Any] | None:
    try:
        return fetch_aircraft(key)
    except FeedError as error:
        LOGGER.warning("ADS-B circle %s unavailable: %s", key, error)
        return None


def reset_cache() -> None:
    global _rate_limited_until
    with _LOCK:
        _CACHE.clear()
        _FAILED_AT.clear()
    _rate_limited_until = 0.0
    FILLER.clear()
