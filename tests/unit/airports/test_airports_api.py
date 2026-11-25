from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.airports import api


def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_serves_the_committed_airport_list() -> None:
    api.load_airports.cache_clear()
    api._bodies.cache_clear()
    response = client().get("/airports")
    assert response.status_code == 200
    assert "max-age=86400" in response.headers["cache-control"]
    body = response.json()
    assert body["count"] > 1000
    nashville = next(a for a in body["airports"] if a["ident"] == "KBNA")
    assert nashville["iata"] == "BNA"
    assert abs(nashville["latitude"] - 36.12) < 0.1
    assert abs(nashville["longitude"] + 86.68) < 0.1


def test_missing_codes_and_numbers_become_null(tmp_path, monkeypatch) -> None:
    path = tmp_path / "airports.csv"
    path.write_text(
        "ident,iata,name,latitude,longitude,elevation_ft,municipality,country\n"
        "XXXX,,Nowhere Field,10.5,20.25,,,ZZ\n"
        "YYYY,,Broken,not-a-number,1,,,ZZ\n"
    )
    monkeypatch.setenv("ORION_AIRPORTS_FILE", str(path))
    api.load_airports.cache_clear()
    api._bodies.cache_clear()
    body = client().get("/airports").json()
    assert body["count"] == 1
    assert body["airports"][0]["iata"] is None
    assert body["airports"][0]["elevation_ft"] is None
    api.load_airports.cache_clear()
    api._bodies.cache_clear()
