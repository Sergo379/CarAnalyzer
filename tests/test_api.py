from fastapi.testclient import TestClient

from backend.api.search import get_search_service
from backend.main import app
from backend.services.search_service import SearchService


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_search_api_uses_typed_end_to_end_schema_without_fake_listings() -> None:
    app.dependency_overrides[get_search_service] = lambda: SearchService(scrapers=[])
    try:
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
    finally:
        app.dependency_overrides.clear()
    payload = response.json()
    assert response.status_code == 200
    assert payload["source_vehicle"]["brand"] == "BMW"
    assert payload["source_vehicle"]["model"] == "5 Series"
    assert payload["source_vehicle"]["modification"] == "520i"
    assert payload["source_vehicle"]["segment"] == "passenger_premium"
    assert payload["source_status"] == {}
    assert payload["source_listings"] == {"listings": [], "model_groups": []}
    assert payload["source_model_group"] is None
    assert payload["direct"] == {"listings": [], "model_groups": []}


def test_search_api_rejects_invalid_body_and_price() -> None:
    app.dependency_overrides[get_search_service] = lambda: SearchService(scrapers=[])
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/search",
                json={
                    "brand": "BMW",
                    "model": "520i",
                    "year": 2022,
                    "body_type": "spaceship",
                    "price": 0,
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422


def test_vehicle_catalog_models_are_dependent_on_brand() -> None:
    with TestClient(app) as client:
        payload = client.get("/api/catalog/vehicles").json()
    by_brand = {entry["name"]: entry["models"] for entry in payload["brands"]}
    assert "X5" in by_brand["BMW"]
    assert "Camry" not in by_brand["BMW"]
    assert "Captur" in by_brand["Renault"]
