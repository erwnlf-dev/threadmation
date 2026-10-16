import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from threadmation.server import create_app

H = {"X-API-Key": "secret-key"}


@pytest.fixture
def api(bot):
    return TestClient(create_app(bot))


def test_requires_api_key(api):
    assert api.post("/v1/posts", json={"text": "x"}).status_code == 401
    assert api.get("/healthz").status_code == 200


def test_create_post(api, session):
    r = api.post("/v1/posts", json={"text": "Update dari pipeline", "topic_tag": "AI"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "m1"
    create = session.calls_to("POST", "111/threads")[0][2]
    assert create["text"] == "Update dari pipeline"
    assert create["topic_tag"] == "AI"


def test_post_validation(api):
    assert api.post("/v1/posts", json={"text": "x" * 501}, headers=H).status_code == 422


def test_threads_error_maps_to_502(api, routes):
    from .conftest import FakeResponse

    routes[("POST", "111/threads")] = FakeResponse(400, {"error": {"message": "bad"}})
    r = api.post("/v1/replies", json={"reply_to_id": "9", "text": "hi"}, headers=H)
    assert r.status_code == 502


def test_webhook_verify(api):
    q = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "123"}
    assert api.get("/webhooks/threads", params=q).text == "123"
    q["hub.verify_token"] = "salah"
    assert api.get("/webhooks/threads", params=q).status_code == 403


def test_webhook_receive_checks_signature_and_stores_lead(api, bot):
    body = json.dumps(
        {"values": {"field": "mentions", "value": {"id": "w1", "username": "rina", "text": "harga?"}}}
    ).encode()
    assert api.post("/webhooks/threads", content=body, headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 401

    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    r = api.post("/webhooks/threads", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.json() == {"received": True, "new_leads": 1}
    assert bot.store.get_lead("w1")["source"] == "mention"


def test_leads_and_outbox_endpoints(api, bot):
    bot.store.add_lead({"id": "L1", "username": "u"}, "search")
    bot.store.queue_dm("u", "hai", lead_id="L1")
    assert api.get("/v1/leads", headers=H).json()["data"][0]["id"] == "L1"
    assert api.patch("/v1/leads/L1", json={"status": "contacted"}, headers=H).json()["status"] == "contacted"
    item = api.get("/v1/outbox", headers=H).json()["data"][0]
    assert api.patch(f"/v1/outbox/{item['id']}", json={"status": "sent"}, headers=H).status_code == 200
    assert api.get("/v1/outbox", headers=H).json()["data"] == []
