from fastapi.testclient import TestClient

from backend.main import app


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_search_foundation_has_no_fake_listings() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/search",
            json={
                "brand": " bmw ",
                "model": "520i",
                "year": 2022,
                "body_type": "sedan",
                "price": 4_100_000,
            },
        )
    payload = response.json()
    assert response.status_code == 200
    assert payload["query"]["brand"] == "BMW"
    assert payload["competitors"] == {"direct": [], "expensive": [], "cheaper": []}
    assert payload["source_status"] == "foundation_ready_auto_ru_not_connected"
