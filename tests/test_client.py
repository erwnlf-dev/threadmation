import pytest

from threadmation.client import ThreadsAPIError, ThreadsClient

from .conftest import FakeResponse, FakeSession


def test_post_text_creates_container_then_publishes(client, session):
    media_id = client.post(text="Halo Threads", topic_tag="cloud")
    assert media_id == "m1"
    create = session.calls_to("POST", "111/threads")[0][2]
    assert create["media_type"] == "TEXT"
    assert create["text"] == "Halo Threads"
    assert create["topic_tag"] == "cloud"
    assert create["access_token"] == "TOKEN"
    assert session.calls_to("POST", "111/threads_publish")[0][2]["creation_id"] == "c1"


def test_reply_sets_reply_to_id(client, session):
    client.reply("999", "makasih!")
    assert session.calls_to("POST", "111/threads")[0][2]["reply_to_id"] == "999"


def test_text_too_long_rejected(client):
    with pytest.raises(ValueError):
        client.post(text="x" * 501)


def test_waits_for_media_container(routes, session, client):
    routes[("GET", "c1")] = [{"status": "IN_PROGRESS"}, {"status": "FINISHED"}]
    client.post(text="foto", image_url="https://example.com/a.jpg")
    assert len(session.calls_to("GET", "c1")) == 2
    assert session.calls_to("POST", "111/threads")[0][2]["media_type"] == "IMAGE"


def test_container_error_raises(routes, client):
    routes[("GET", "c1")] = {"status": "ERROR", "error_message": "bad image"}
    with pytest.raises(ThreadsAPIError, match="bad image"):
        client.post(text="x", image_url="https://example.com/a.jpg")


def test_carousel_builds_children(routes, session, client):
    client.post(text="album", carousel=[{"image_url": "a"}, {"image_url": "b"}])
    creates = [c[2] for c in session.calls_to("POST", "111/threads")]
    assert [c["media_type"] for c in creates] == ["IMAGE", "IMAGE", "CAROUSEL"]
    assert creates[0]["is_carousel_item"] == "true"
    assert creates[2]["children"] == "c1,c1"


def test_api_error_message_surfaced(routes, client):
    routes[("GET", "me")] = FakeResponse(400, {"error": {"message": "Invalid OAuth access token", "code": 190}})
    with pytest.raises(ThreadsAPIError) as exc:
        client.me()
    assert "Invalid OAuth" in str(exc.value)
    assert exc.value.code == 190


def test_get_retries_on_5xx_but_post_does_not(routes):
    routes[("GET", "me")] = [FakeResponse(500, {}), {"id": "111", "username": "u"}]
    s = FakeSession(routes)
    c = ThreadsClient("T", session=s, sleep=lambda x: None)
    assert c.me()["id"] == "111"

    routes[("POST", "111/threads")] = FakeResponse(500, {})
    with pytest.raises(ThreadsAPIError):
        c.create_container(text="hi")
    assert len(s.calls_to("POST", "111/threads")) == 1


def test_paginate_follows_cursor(routes, client):
    routes[("GET", "keyword_search")] = lambda p: (
        {"data": [{"id": "a"}, {"id": "b"}], "paging": {"cursors": {"after": "CUR"}, "next": "n"}}
        if "after" not in p
        else {"data": [{"id": "c"}], "paging": {}}
    )
    assert [r["id"] for r in client.keyword_search("vendor", limit=10)] == ["a", "b", "c"]
    assert [r["id"] for r in client.keyword_search("vendor", limit=2)] == ["a", "b"]


def test_refresh_token_updates_client(routes, session, client):
    routes[("GET", "refresh_access_token")] = {"access_token": "NEW", "expires_in": 5184000}
    client.refresh_token()
    assert client.access_token == "NEW"
    assert session.calls_to("GET", "refresh_access_token")[0][2]["grant_type"] == "th_refresh_token"
