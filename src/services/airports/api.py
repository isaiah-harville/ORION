"""Major airports, for the flight view's airport overlay.

The list is OurAirports' "large_airport" set (public domain, ~1,200 airports
worldwide), trimmed to the fields the map shows and committed as
data/airports.csv. It changes on the scale of years, so it is read once and
served with a day-long cache.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response

router = APIRouter(prefix="/airports", tags=["airports"])

# Resolved against the repository root rather than the working directory, so
# the API finds it whether it runs from /app in the image or from src/ locally.
DEFAULT_PATH = Path(__file__).resolve().parents[3] / "data" / "airports.csv"
CACHE_SECONDS = 86_400


def airports_path() -> Path:
    return Path(os.environ.get("ORION_AIRPORTS_FILE", str(DEFAULT_PATH)))


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


@lru_cache(maxsize=1)
def load_airports(path: Path) -> tuple[dict[str, Any], ...]:
    airports: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            latitude = _number(row["latitude"])
            longitude = _number(row["longitude"])
            if latitude is None or longitude is None:
                continue
            airports.append(
                {
                    "ident": row["ident"],
                    "iata": row["iata"] or None,
                    "name": row["name"],
                    "latitude": latitude,
                    "longitude": longitude,
                    "elevation_ft": _number(row["elevation_ft"]),
                    "municipality": row["municipality"] or None,
                    "country": row["country"],
                }
            )
    return tuple(airports)


@lru_cache(maxsize=1)
def _bodies(path: Path) -> tuple[bytes, bytes]:
    """The response, serialised and gzipped once: it never changes while the
    process runs, so there is no reason to re-encode it per request."""
    data = list(load_airports(path))
    body = json.dumps(
        {
            "attribution": {
                "text": "Airports: OurAirports (public domain)",
                "url": "https://ourairports.com/",
            },
            "airports": data,
            "count": len(data),
        },
        separators=(",", ":"),
    ).encode()
    return body, gzip.compress(body, compresslevel=9)


@router.get("")
def airports(request: Request) -> Response:
    body, compressed = _bodies(airports_path())
    headers = {
        "Cache-Control": f"public, max-age={CACHE_SECONDS}",
        "Vary": "Accept-Encoding",
    }
    if "gzip" in request.headers.get("accept-encoding", ""):
        # Already compressed, so the API's gzip middleware passes it through.
        headers["Content-Encoding"] = "gzip"
        return Response(compressed, media_type="application/json", headers=headers)
    return Response(body, media_type="application/json", headers=headers)
