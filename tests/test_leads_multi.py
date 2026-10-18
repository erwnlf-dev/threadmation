import sqlite3

import pytest

from threadmation.config import ProfileRules, ResponseRule
from threadmation.leads import score_profile
from threadmation.social import SocialBridgeError
from threadmation.store import Store

PROFILE = ProfileRules(
    include={"founder": 2, "building": 1.5},
    exclude=["crypto"],
    min_followers=30,
    max_followers=100_000,
    mid_followers=(200, 20_000),
    mid_bonus=1,
)


def x_post(pid, username, text, bio, followers):
    return {
        "id": f"x_{pid}", "platform": "x", "username": username, "text": text,
        "permalink": f"https://x.com/{username}/status/{pid}",
        "author": {"name": username.title(), "bio": bio, "followers": followers},
    }


class FakeSocial:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.queries = []

    def search(self, platform, query, limit=25, sort="recency"):
        self.queries.append((platform, query, limit))
        if self.error:
            raise SocialBridgeError(self.error)
        return list(self.results)


@pytest.mark.parametrize(
    "author, expected",
    [
        (None, 0),
        ({"bio": "Founder, building AI tools", "followers": 900}, 2 + 1.5 + 1),
        ({"bio": "founder", "followers": None}, 2),  # follower tidak diketahui: tidak dibuang
        ({"bio": "founder", "followers": 10}, -1),  # terlalu kecil
        ({"bio": "founder", "followers": 2_000_000}, -1),  # influencer
        ({"bio": "crypto founder", "followers": 900}, -1),
        ({"bio": "", "followers": 900, "verified": True}, 1),  # centang biru tidak menambah skor
    ],
)
def test_score_profile(author, expected):
    assert score_profile(author, PROFILE)[0] == expected


@pytest.fixture
def multi(bot, routes):
    bot.rules.platforms = ["threads", "x"]
    bot.rules.profile = PROFILE
    bot.rules.queries = [{"q": "claude"}]
    bot.rules.include = {"claude": 2, "startup": 1}
    bot.rules.min_score = 4
    routes[("GET", "keyword_search")] = {
        "data": [
            {"id": "t1", "username": "rina", "text": "Startup kami pakai Claude", "permalink": "tp1"},
            {"id": "t2", "username": "bigbrand", "text": "Claude startup news", "permalink": "tp2"},
        ]
    }
    routes[("GET", "profile_lookup")] = lambda p: {
        "rina": {"username": "rina", "name": "Rina S", "biography": "Founder @ kopi.ai", "follower_count": 1500},
        "bigbrand": {"username": "bigbrand", "name": "Big", "biography": "news", "follower_count": 5_000_000},
    }[p["username"]]
    bot.crawler.social = FakeSocial([
        x_post(1, "dev_andi", "Got into Claude for Startups! startup life", "Co-founder, building agents", 3000),
        x_post(2, "dev_andi", "Shipping our Claude startup MVP", "Co-founder, building agents", 3000),
        x_post(3, "random", "claude", "", 50),
        x_post(4, "shill", "claude startup", "crypto calls", 900),
    ])
    return bot


def test_crawl_threads_and_x_with_profile_scoring(multi, session):
    report = multi.crawl(mentions=False, replies=False)
    assert report.errors == []
    leads = {l["id"]: l for l in multi.store.list_leads()}
    assert set(leads) == {"t1", "x_1", "x_2"}  # bigbrand (influencer), random (skor rendah), shill (crypto) dibuang
    assert leads["t1"]["platform"] == "threads"
    assert leads["t1"]["author"]["bio"] == "Founder @ kopi.ai"
    assert "bio:founder" in leads["t1"]["matched"]
    assert leads["x_1"]["platform"] == "x"
    assert multi.crawler.social.queries == [("x", "claude -is:retweet", 25)]
    # profil tiap username hanya diambil sekali
    assert len(session.calls_to("GET", "profile_lookup")) == 2


def test_x_failure_does_not_stop_threads(multi):
    multi.crawler.social = FakeSocial(error="HTTP 401")
    report = multi.crawl(mentions=False, replies=False)
    assert [l["id"] for l in report.new_leads] == ["t1"]
    assert report.errors == ["search x 'claude': HTTP 401"]


def test_threads_profile_permission_missing_still_crawls(multi, routes, session):
    from .conftest import FakeResponse

    routes[("GET", "profile_lookup")] = FakeResponse(403, {"error": {"message": "no permission"}})
    multi.rules.min_score = 2
    multi.crawl(mentions=False, replies=False)
    assert multi.store.get_lead("t1")["author"] is None
    assert len(session.calls_to("GET", "profile_lookup")) == 1  # berhenti mencoba setelah gagal


def test_people_groups_by_person(multi):
    multi.crawl(mentions=False, replies=False)
    people = multi.store.list_people()
    assert [(p["platform"], p["username"], p["posts"]) for p in people] == [("x", "dev_andi", 2), ("threads", "rina", 1)]
    assert people[0]["profile_url"] == "https://x.com/dev_andi"
    assert people[0]["bio"] == "Co-founder, building agents"
    assert [p["username"] for p in multi.store.list_people(min_posts=2)] == ["dev_andi"]


def test_responder_x_leads_go_to_outbox_only(multi, session):
    multi.crawl(mentions=False, replies=False)
    rules = [
        ResponseRule(name="x", sources=["search"], platforms=["x"], reply="hai", dm="Hi {first_name}, re {permalink}"),
        ResponseRule(name="th", sources=["search"], platforms=["threads"], dm="Halo {name}"),
    ]
    multi.rules.responses = multi.responder.rules.responses = rules
    multi.respond(dry_run=False)
    assert session.calls_to("POST", "111/threads") == []  # tidak ada balasan publik ke lead X
    outbox = {(o["platform"], o["username"]): o["message"] for o in multi.store.list_outbox()}
    assert outbox == {
        ("x", "dev_andi"): "Hi Dev_Andi, re https://x.com/dev_andi/status/1",
        ("threads", "rina"): "Halo Rina S",
    }


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE leads (id TEXT PRIMARY KEY, source TEXT NOT NULL, query TEXT, username TEXT, text TEXT,
           permalink TEXT, posted_at TEXT, score REAL DEFAULT 0, matched TEXT, status TEXT NOT NULL DEFAULT 'new',
           raw TEXT, handled_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
           CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, username TEXT NOT NULL,
           message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, sent_at TEXT,
           UNIQUE(lead_id, username));
           INSERT INTO leads(id, source, username, created_at, updated_at) VALUES ('old1', 'search', 'budi', 't', 't');"""
    )
    conn.close()
    store = Store(str(path))
    assert store.get_lead("old1")["platform"] == "threads"
    assert store.add_lead(x_post(9, "a", "t", "b", 1), "search")
    assert store.get_lead("x_9")["author"]["bio"] == "b"
    assert store.queue_dm("a", "hi", lead_id="x_9", platform="x")
