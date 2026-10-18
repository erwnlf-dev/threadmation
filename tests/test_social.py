import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from threadmation.server import create_app
from threadmation.social import DEFAULT_BRIDGE_DIR, SocialBridge, SocialBridgeError, media_from_post_kwargs

H = {"X-API-Key": "secret-key"}
PUBLISHED = {"status": "complete", "results": [{"platform": "x", "state": "published", "url": "https://x.com/i/1"}]}


class FakeRunner:
    def __init__(self, out=PUBLISHED, returncode=0):
        self.out = out
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, input=None, **kwargs):
        self.calls.append((cmd[-1], json.loads(input)))
        return subprocess.CompletedProcess(cmd, self.returncode, json.dumps(self.out) + "\n", "")


@pytest.fixture
def runner(bot, monkeypatch):
    r = FakeRunner()
    bot.social = SocialBridge(DEFAULT_BRIDGE_DIR, runner=r)
    monkeypatch.setattr(SocialBridge, "_call", _call_without_checks)
    return r


def _call_without_checks(self, action, payload=None):
    proc = self._run(["node", "bridge.mjs", action], input=json.dumps(payload or {}))
    out = json.loads(proc.stdout)
    if proc.returncode != 0 or "error" in out:
        raise SocialBridgeError(out.get("error"))
    return out


def test_media_from_post_kwargs():
    media = media_from_post_kwargs({"image_url": "https://a.jpg", "alt_text": "foto", "carousel": [{"video_url": "https://v.mp4"}]})
    assert media == [{"kind": "image", "url": "https://a.jpg", "alt_text": "foto"}, {"kind": "video", "url": "https://v.mp4"}]


def test_post_crossposts(bot, runner, session):
    bot.notifier.url = "https://hooks.example/n8n"
    info = bot.post("Halo semua", crosspost=["x", "bluesky"], image_url="https://a.jpg")
    assert info["crosspost"]["status"] == "complete"
    action, payload = runner.calls[0]
    assert action == "publish"
    assert payload["platforms"] == ["x", "bluesky"]
    assert payload["media"] == [{"kind": "image", "url": "https://a.jpg"}]
    assert payload["idempotency_key"] == "threads-m1"
    events = [c[2]["event"] for c in session.calls if c[0] == "WEBHOOK"]
    assert events == ["post.published", "social.published"]


def test_default_crosspost_skips_replies(bot, runner):
    bot.settings.crosspost = ["x"]
    assert "crosspost" in bot.post("post biasa")
    assert "crosspost" not in bot.reply("9", "balasan")
    assert "crosspost" not in bot.post("tanpa cross-post", crosspost=[])
    assert len(runner.calls) == 1


def test_crosspost_failure_does_not_raise(bot, runner):
    runner.out, runner.returncode = {"error": "Node.js belum terpasang"}, 1
    info = bot.post("Halo", crosspost=["x"])
    assert info["id"] == "m1"
    assert info["crosspost"] == {"status": "failed", "error": "Node.js belum terpasang", "results": []}


def test_api_social_endpoints(bot, runner):
    api = TestClient(create_app(bot))
    assert api.post("/v1/social/posts", json={"text": "x"}, headers=H).status_code == 422
    r = api.post("/v1/social/posts", json={"platforms": ["x"], "text": "Rilis v2", "link": "https://c.com"}, headers=H)
    assert r.status_code == 200, r.text
    assert runner.calls[-1][1]["link"] == "https://c.com"
    r = api.post("/v1/posts", json={"text": "dari pipeline", "crosspost": ["x"]}, headers=H)
    assert r.json()["crosspost"]["status"] == "complete"
    runner.out, runner.returncode = {"error": "boom"}, 1
    assert api.get("/v1/social/platforms", headers=H).status_code == 503


def test_missing_bridge_dir(tmp_path):
    with pytest.raises(SocialBridgeError, match="bridge tidak ditemukan"):
        SocialBridge(tmp_path).platforms()


@pytest.mark.skipif(
    shutil.which("node") is None or not (DEFAULT_BRIDGE_DIR / "node_modules").is_dir(),
    reason="butuh Node.js + npm install di social-bridge/",
)
def test_real_bridge_with_sdk_mock_backend(monkeypatch):
    monkeypatch.setenv("SOCIAL_SDK_MOCK", "1")
    for key in ("SOCIAL_X_USER_ID", "SOCIAL_BLUESKY_HANDLE", "SOCIAL_LINKEDIN_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    bridge = SocialBridge()
    assert bridge.platforms() == ["mock"]
    out = bridge.publish(["mock", "x"], "Halo dari test", idempotency_key="t-1")
    assert out["status"] == "partial"
    by_platform = {r["platform"]: r for r in out["results"]}
    assert by_platform["mock"]["state"] == "published"
    assert by_platform["x"]["reason"] == "not-configured"
