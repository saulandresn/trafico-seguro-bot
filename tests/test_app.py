import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

os.environ["DATABASE_URL"] = "sqlite:///./test_traffic.db"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["INIT_DATA_MAX_AGE_SECONDS"] = "86400"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app

client = TestClient(app)


def make_init_data(user_id: int, auth_date: int | None = None) -> str:
    payload = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": f"q-{user_id}",
        "user": json.dumps(
            {"id": user_id, "first_name": "Test"},
            separators=(",", ":"),
        ),
    }
    data_check_string = "\n".join(
        f"{key}={payload[key]}" for key in sorted(payload)
    )
    secret_key = hmac.new(
        b"WebAppData",
        b"test-token",
        hashlib.sha256,
    ).digest()
    payload["hash"] = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(payload)


def auth_headers(user_id: int) -> dict:
    return {
        "X-Telegram-Init-Data": make_init_data(user_id),
        "X-Client-Token": f"legacy-{user_id}",
    }


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def create_report(user_id: int = 1, **overrides):
    payload = {
        "kind": "accidente",
        "latitude": -2.17,
        "longitude": -79.9,
        "description": "Carril bloqueado",
    }
    payload.update(overrides)
    return client.post("/api/incidents", json=payload, headers=auth_headers(user_id))


def test_invalid_telegram_init_data_is_rejected():
    response = client.post(
        "/api/incidents",
        json={
            "kind": "accidente",
            "latitude": -2.17,
            "longitude": -79.9,
            "description": "Prueba",
        },
        headers={"X-Telegram-Init-Data": "auth_date=1&hash=bad"},
    )
    assert response.status_code == 401


def test_create_edit_share_and_delete_own_report():
    created = create_report()
    assert created.status_code == 200
    incident_id = created.json()["id"]

    edited = client.put(
        f"/api/incidents/{incident_id}",
        headers=auth_headers(1),
        json={
            "kind": "alarma",
            "latitude": -2.171,
            "longitude": -79.901,
            "description": "Obstáculo en la vía",
        },
    )
    assert edited.status_code == 200
    assert edited.json()["status"] == "updated"

    detail = client.get(f"/incident/{incident_id}")
    assert detail.status_code == 200
    assert "Compartir" in detail.text

    other_user_edit = client.put(
        f"/api/incidents/{incident_id}",
        headers=auth_headers(2),
        json={
            "kind": "alarma",
            "latitude": -2.171,
            "longitude": -79.901,
            "description": "Cambio ajeno",
        },
    )
    assert other_user_edit.status_code == 403

    deleted = client.delete(
        f"/api/incidents/{incident_id}",
        headers=auth_headers(1),
    )
    assert deleted.status_code == 200

    missing = client.get(f"/incident/{incident_id}")
    assert missing.status_code == 404


def test_votes_affect_reputation_and_gone_hides_report():
    created = create_report(user_id=10)
    assert created.status_code == 200
    incident_id = created.json()["id"]

    confirmed = client.post(
        f"/api/incidents/{incident_id}/vote",
        headers={**auth_headers(20), "Content-Type": "application/json"},
        json={"action": "confirm"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["active"] is True

    reputation = client.get("/api/me/reputation", headers=auth_headers(10))
    assert reputation.status_code == 200
    assert reputation.json()["score"] > 50

    gone = client.post(
        f"/api/incidents/{incident_id}/vote",
        headers={**auth_headers(20), "Content-Type": "application/json"},
        json={"action": "gone"},
    )
    assert gone.status_code == 200
    assert gone.json()["active"] is False

    nearby = client.get(
        "/api/incidents/nearby?lat=-2.17&lng=-79.9&radius_km=3",
        headers=auth_headers(10),
    )
    assert nearby.status_code == 200
    assert nearby.json()["incidents"] == []


@pytest.mark.parametrize(
    "description",
    [
        "Mira https://example.com",
        "idiota en la vía",
        "aaaaaaaaaaaa",
    ],
)
def test_text_moderation_rejects_unsafe_or_spammy_descriptions(description):
    response = create_report(description=description)
    assert response.status_code == 400


def test_navigation_and_edit_form_are_present():
    mini = client.get("/mini-app")
    assert mini.status_code == 200
    for element_id in ["navHome", "navMap", "navReport", "navMine"]:
        assert f'id="{element_id}"' in mini.text
    assert "editIncidentId" in mini.text
    assert "X-Telegram-Init-Data" in mini.text

    map_page = client.get("/map?lat=-2.17&lng=-79.9&radius_km=3")
    assert map_page.status_code == 200
    assert "shareIncident" in map_page.text
    assert "X-Telegram-Init-Data" in map_page.text
