import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.adsb import feed
from services.adsb.api import router

AIRCRAFT = {
    "hex": "a78d6b",
    "flight": "JIA5024 ",
    "r": "N586NN",
    "t": "CRJ9",
    "alt_baro": 32000,
    "gs": 478.1,
    "track": 243.01,
    "baro_rate": 64,
    "squawk": "1677",
    "emergency": "none",
    "lat": 36.67424,
    "lon": -88.362579,
    "seen_pos": 0.409,
    "dst": 89.886,
}


@pytest.fixture
def client():
    feed.reset_cache()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def stub_feed(monkeypatch, aircraft, calls=None):
    def request(url, timeout):
        if calls is not None:
            calls.append(url)
        return {"now": 1788150001000, "ac": aircraft}

    monkeypatch.setattr(feed, "_request", request)


def test_returns_normalised_aircraft(client, monkeypatch) -> None:
    stub_feed(monkeypatch, [AIRCRAFT])
    response = client.get("/adsb?lat=36.0&lon=-86.7")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    contact = body["aircraft"][0]
    assert contact["id"] == "a78d6b"
    # The feed pads the callsign to eight characters.
    assert contact["callsign"] == "JIA5024"
    assert (contact["latitude"], contact["longitude"]) == (36.67424, -88.362579)
    assert contact["altitude_ft"] == 32000
    assert contact["on_ground"] is False
    # "none" is the feed's way of saying there is no emergency.
    assert contact["emergency"] is None


def test_ground_altitude_is_not_a_number(client, monkeypatch) -> None:
    stub_feed(monkeypatch, [{**AIRCRAFT, "alt_baro": "ground"}])
    contact = client.get("/adsb?lat=36.0&lon=-86.7").json()["aircraft"][0]
    assert contact["altitude_ft"] is None
    assert contact["on_ground"] is True


def test_positionless_contacts_are_dropped(client, monkeypatch) -> None:
    # A contact heard on the radio but not yet located cannot be drawn.
    stub_feed(monkeypatch, [{**AIRCRAFT, "lat": None, "lon": None}])
    assert client.get("/adsb?lat=36.0&lon=-86.7").json()["count"] == 0


def test_aircraft_are_nearest_first(client, monkeypatch) -> None:
    far = {**AIRCRAFT, "hex": "beef01", "dst": 200.0}
    stub_feed(monkeypatch, [far, AIRCRAFT])
    ids = [c["id"] for c in client.get("/adsb?lat=36.0&lon=-86.7").json()["aircraft"]]
    assert ids == ["a78d6b", "beef01"]


def test_nearby_viewports_share_one_upstream_fetch(client, monkeypatch) -> None:
    calls: list[str] = []
    stub_feed(monkeypatch, [AIRCRAFT], calls)
    # A pan of a few hundredths of a degree is the same sky.
    for lat in (36.0, 36.02, 35.99, 36.04):
        client.get(f"/adsb?lat={lat}&lon=-86.7")
    assert len(calls) == 1


def test_distant_viewports_do_not_share_a_fetch(client, monkeypatch) -> None:
    calls: list[str] = []
    stub_feed(monkeypatch, [AIRCRAFT], calls)
    client.get("/adsb?lat=36.0&lon=-86.7")
    client.get("/adsb?lat=47.6&lon=-122.3")
    assert len(calls) == 2


def test_a_stale_feed_is_served_when_upstream_fails(client, monkeypatch) -> None:
    stub_feed(monkeypatch, [AIRCRAFT])
    assert client.get("/adsb?lat=36.0&lon=-86.7").status_code == 200

    monkeypatch.setattr(feed, "cache_ttl_seconds", lambda: 0.0)

    def boom(url, timeout):
        raise TimeoutError("adsb is down")

    monkeypatch.setattr(feed, "_request", boom)
    response = client.get("/adsb?lat=36.0&lon=-86.7")
    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_unavailable_when_nothing_cached(client, monkeypatch) -> None:
    def boom(url, timeout):
        raise TimeoutError("adsb is down")

    monkeypatch.setattr(feed, "_request", boom)
    assert client.get("/adsb?lat=36.0&lon=-86.7").status_code == 503


@pytest.mark.parametrize(
    "query",
    ["lat=91&lon=0", "lat=0&lon=181", "lat=0&lon=0&radius_nm=400", "lon=0"],
)
def test_unusable_queries_are_rejected(client, query) -> None:
    assert client.get(f"/adsb?{query}").status_code == 422


def test_requested_area_is_the_one_fetched(client, monkeypatch) -> None:
    calls: list[str] = []
    stub_feed(monkeypatch, [AIRCRAFT], calls)
    client.get("/adsb?lat=36.01&lon=-86.76&radius_nm=100")
    # Snapped to the shared grid, not passed through verbatim.
    assert calls == ["https://api.adsb.lol/v2/lat/36.0/lon/-86.75/dist/100"]


def test_a_cached_answer_says_how_old_it_is(client, monkeypatch) -> None:
    stub_feed(monkeypatch, [AIRCRAFT])
    fresh = client.get("/adsb?lat=36.0&lon=-86.7").json()
    # A fetch just made is current; the client needs this to be told, not
    # assumed, or every cached position it draws is silently a TTL behind.
    assert fresh["feed_age_seconds"] < 1

    clock = {"t": 100.0}
    monkeypatch.setattr(feed.time, "monotonic", lambda: clock["t"])
    feed.reset_cache()
    client.get("/adsb?lat=36.0&lon=-86.7")
    clock["t"] = 105.0
    assert client.get("/adsb?lat=36.0&lon=-86.7").json()["feed_age_seconds"] == 5.0


def _cell_covered(key, lat, lon) -> bool:
    import math

    dlat = (lat - key.lat) * 60
    dlon = (lon - key.lon) * 60 * math.cos(math.radians(lat))
    return math.hypot(dlat, dlon) <= key.radius_nm


def test_area_circles_leave_no_gaps_over_the_viewport() -> None:
    # A regional viewport around Tennessee, sampled on a fine lattice.
    west, south, east, north = -92.0, 33.0, -82.0, 39.5
    keys, truncated = feed.area_tiles(west, south, east, north, limit=100)
    assert not truncated
    for i in range(41):
        for j in range(41):
            lat = south + (north - south) * i / 40
            lon = west + (east - west) * j / 40
            assert any(_cell_covered(k, lat, lon) for k in keys), (lat, lon)


def test_area_circles_sit_on_a_shared_grid() -> None:
    # Panning slightly must reuse the same upstream circles, or every client
    # pan would be a fresh set of upstream fetches.
    a, _ = feed.area_tiles(-90.0, 34.0, -84.0, 38.0)
    b, _ = feed.area_tiles(-89.7, 34.2, -83.6, 38.1)
    assert set(a) == set(b)


def test_a_continental_view_is_capped_nearest_the_centre() -> None:
    keys, truncated = feed.area_tiles(-125.0, 24.0, -66.5, 49.5)
    assert truncated
    assert len(keys) == feed.MAX_AREA_TILES
    centre_lat, centre_lon = 36.75, -95.75
    assert all(
        abs(k.lat - centre_lat) < 10 and abs(k.lon - centre_lon) < 15 for k in keys
    )


def test_area_merges_overlapping_circles_keeping_the_freshest(
    client, monkeypatch
) -> None:
    def request(url, timeout):
        # Every circle reports the same aircraft; one heard it most recently.
        fresh = "/lat/37.5/" in url
        return {
            "now": 1788150001000,
            "ac": [
                {
                    **AIRCRAFT,
                    "lat": 36.0,
                    "lon": -87.0,
                    "seen_pos": 0.5 if fresh else 9.0,
                }
            ],
        }

    monkeypatch.setattr(feed, "_request", request)
    response = client.get("/adsb/area?west=-92&south=33&east=-82&north=39.5")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["aircraft"][0]["seen_pos_s"] == 0.5
    assert body["feed_age_seconds"] == 0.0


def test_area_survives_a_failing_circle(client, monkeypatch) -> None:
    def request(url, timeout):
        if "/lat/32.5/" in url:
            raise feed.urllib.error.URLError("down")
        return {"now": 1788150001000, "ac": [{**AIRCRAFT, "lat": 36.0, "lon": -87.0}]}

    monkeypatch.setattr(feed, "_request", request)
    body = client.get("/adsb/area?west=-92&south=33&east=-82&north=39.5").json()
    assert body["count"] == 1
    assert body["circles_answered"] < body["circles"]


def test_area_rejects_an_empty_box(client) -> None:
    assert (
        client.get("/adsb/area?west=-80&south=35&east=-90&north=36").status_code == 422
    )


def test_a_wide_view_fills_in_from_the_background(client, monkeypatch) -> None:
    started = []
    monkeypatch.setattr(feed.FILLER, "start", lambda: started.append(True))
    calls: list[str] = []
    stub_feed(monkeypatch, [{**AIRCRAFT, "lat": 36.0, "lon": -87.0}], calls)

    query = "/adsb/area?west=-125&south=24&east=-66.5&north=49.5"
    first = client.get(query).json()
    # Only the live circles are fetched up front; the rest are queued.
    assert len(calls) == feed.MAX_AREA_TILES
    assert first["pending"] == first["circles"] - feed.MAX_AREA_TILES
    assert first["truncated"] is False
    assert started

    # The worker fills one circle per step, nearest the centre first.
    while feed.FILLER.run_once():
        pass
    second = client.get(query).json()
    assert second["pending"] == 0
    assert len(calls) == second["circles"]


def test_the_background_worker_forgets_unwatched_circles(monkeypatch) -> None:
    feed.reset_cache()
    monkeypatch.setattr(feed.FILLER, "start", lambda: None)
    feed.FILLER.want([feed.FeedKey(37.5, -87.5, 250)])
    clock = [feed.time.monotonic() + feed.WANTED_FOR_SECONDS + 1]
    monkeypatch.setattr(feed.time, "monotonic", lambda: clock[0])
    assert feed.FILLER.next_key() is None


def test_a_failing_circle_is_not_retried_on_every_request(client, monkeypatch) -> None:
    calls: list[str] = []

    def request(url, timeout):
        calls.append(url)
        raise feed.urllib.error.URLError("down")

    monkeypatch.setattr(feed, "_request", request)
    for _ in range(5):
        assert client.get("/adsb?lat=36&lon=-86&radius_nm=100").status_code == 503
    assert len(calls) == 1


def test_a_429_pauses_every_upstream_call(client, monkeypatch) -> None:
    calls: list[str] = []

    def request(url, timeout):
        calls.append(url)
        raise feed.urllib.error.HTTPError(
            url, 429, "Too Many Requests", {"Retry-After": "120"}, None
        )

    monkeypatch.setattr(feed, "_request", request)
    client.get("/adsb?lat=36&lon=-86&radius_nm=100")
    # A different circle is not tried either while the upstream is cooling off.
    client.get("/adsb?lat=45&lon=-100&radius_nm=100")
    assert len(calls) == 1
    assert feed.upstream_paused()
    assert feed.FILLER.next_key() is None


def test_concurrent_requests_share_one_upstream_fetch(monkeypatch) -> None:
    import threading
    import time

    feed.reset_cache()
    calls: list[str] = []

    def request(url, timeout):
        calls.append(url)
        time.sleep(0.2)
        return {"now": 1788150001000, "ac": [AIRCRAFT]}

    monkeypatch.setattr(feed, "_request", request)
    key = feed.cache_key(36.0, -86.0, 100)
    threads = [
        threading.Thread(target=feed.fetch_aircraft, args=(key,)) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
