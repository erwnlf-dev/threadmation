from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from threadmation.bot import Bot
from threadmation.client import ThreadsClient
from threadmation.config import Rules, ResponseRule, Settings
from threadmation.store import Store


class FakeResponse:
    def __init__(self, status_code: int, data):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeSession:
    """Rute palsu: key = (METHOD, path tanpa /v1.0), value = dict | callable(params) | list (antrean)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, params=None, data=None, timeout=None):
        path = urlparse(url).path.removeprefix("/v1.0").lstrip("/")
        args = dict(params or data or {})
        self.calls.append((method, path, args))
        handler = self.routes.get((method, path))
        if handler is None:
            return FakeResponse(404, {"error": {"message": f"no route {method} {path}"}})
        if isinstance(handler, list):
            handler = handler.pop(0) if len(handler) > 1 else handler[0]
        result = handler(args) if callable(handler) else handler
        if isinstance(result, FakeResponse):
            return result
        return FakeResponse(200, result)

    def post(self, url, json=None, timeout=None):  # dipakai Notifier
        self.calls.append(("WEBHOOK", url, json))
        return FakeResponse(200, {})

    def calls_to(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


ME = {"id": "111", "username": "erwinalif", "name": "Erwin"}


@pytest.fixture
def routes():
    return {
        ("GET", "me"): ME,
        ("POST", "111/threads"): {"id": "c1"},
        ("GET", "c1"): {"id": "c1", "status": "FINISHED"},
        ("POST", "111/threads_publish"): {"id": "m1"},
        ("GET", "m1"): {"id": "m1", "permalink": "https://www.threads.com/@erwinalif/post/x"},
        ("GET", "111/threads_publishing_limit"): {
            "data": [{"quota_usage": 1, "reply_quota_usage": 0, "reply_config": {"quota_total": 1000}}]
        },
    }


@pytest.fixture
def session(routes):
    return FakeSession(routes)


@pytest.fixture
def client(session):
    return ThreadsClient("TOKEN", session=session, sleep=lambda s: None)


@pytest.fixture
def rules():
    return Rules(
        queries=[{"q": "butuh vendor"}],
        include={"butuh": 1, "vendor": 2, "cloud": 1.5},
        exclude=["loker"],
        min_score=2.5,
        own_posts_lookback=5,
        responses=[
            ResponseRule(name="spam", sources=["reply"], match_any=["slot"], hide=True),
            ResponseRule(
                name="harga",
                sources=["mention", "reply"],
                match_any=["harga"],
                reply="Halo @{username}, cek DM ya",
                dm="Halo {username}, soal {permalink}",
            ),
            ResponseRule(name="search", sources=["search"], dm="Halo {username}, soal {query}"),
        ],
        reply_delay_seconds=0,
    )


@pytest.fixture
def bot(tmp_path, client, rules):
    settings = Settings(
        access_token="TOKEN",
        db_path=str(tmp_path / "t.db"),
        rules_path=str(tmp_path / "missing.yaml"),
        api_key="secret-key",
        app_secret="appsecret",
        webhook_verify_token="verify-me",
    )
    b = Bot(settings=settings, client=client, store=Store(settings.db_path))
    b.rules = rules
    b.crawler.rules = rules
    b.responder.rules = rules
    b.responder._sleep = lambda s: None
    b.notifier.session = client.session
    return b
