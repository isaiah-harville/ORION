import os

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from services.adsb.api import router as adsb_router
from services.airports.api import router as airports_router
from services.api.demand import DemandMiddleware, demand_url
from services.nexrad.api import router as radar_router
from services.quakes.api import router as quakes_router
from services.scans.get_stations import get_radars

app = FastAPI(
    root_path="/api",
)
# A zoomed-out flight view is thousands of aircraft: megabytes of repetitive
# JSON that compresses roughly tenfold.
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.include_router(radar_router)
app.include_router(quakes_router)
app.include_router(adsb_router)
app.include_router(airports_router)

_cors_origins = [
    origin.strip()
    for origin in os.environ.get("ORION_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Unset (local dev, tests) means the producer is not listening for demand and
# runs at its normal cadence, so there is nobody to tell.
if demand_url():
    app.add_middleware(DemandMiddleware, url=demand_url())


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/health", status_code=status.HTTP_200_OK)
async def healthcheck():
    """Returns a 200 OK and JSON with status: healthy"""
    return {"status": "healthy"}


@app.get("/radars_near/{lat}/{lon}/{radius_km}")
async def radars_nearby(lat, lon, radius_km):
    """Returns all radars within a specified radius of a given latitude and longitude"""
    radars = get_radars(
        float(lat), float(lon), radius_km=int(radius_km), output_format="json"
    )
    if not len(radars):
        return {"Error": f"Could not find radars within {radius_km}km radius"}, 500
    return radars


@app.get("/radars/{lat}/{lon}")
async def radars(lat, lon):
    """Returns all radars and distances"""
    radars = get_radars(float(lat), float(lon), radius_km=1000000, output_format="json")
    if not len(radars):
        return {"Error": "Could not find any radars"}, 500
    return radars
