from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api import demand
from services.api.demand import DemandMiddleware


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_client(monkeypatch, clock):
    sent: list[str] = []
    monkeypatch.setattr(demand, "_post", sent.append)
    app = FastAPI(root_path="/api")

    @app.get("/health")
    def health():
        return {}

    @app.get("/adsb/aircraft")
    def aircraft():
        return {}

    app.add_middleware(DemandMiddleware, url="http://nexrad/demand", clock=clock)
    return TestClient(app), sent


def test_traffic_pings_the_producer_once_per_window(monkeypatch) -> None:
    clock = Clock()
    client, sent = make_client(monkeypatch, clock)
    client.get("/adsb/aircraft")
    client.get("/adsb/aircraft")
    assert sent == ["http://nexrad/demand"]
    clock.now += 31
    client.get("/adsb/aircraft")
    assert len(sent) == 2


def test_kubernetes_probes_are_not_demand(monkeypatch) -> None:
    client, sent = make_client(monkeypatch, Clock())
    client.get("/health", headers={"User-Agent": "kube-probe/1.33"})
    assert sent == []


def test_a_browser_health_check_is_demand(monkeypatch) -> None:
    client, sent = make_client(monkeypatch, Clock())
    client.get("/health", headers={"User-Agent": "Mozilla/5.0"})
    assert sent == ["http://nexrad/demand"]
