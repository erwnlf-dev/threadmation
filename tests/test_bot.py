from threadmation.bot import extract_webhook_posts
from threadmation.leads import score_text


def _search_routes(routes):
    routes[("GET", "keyword_search")] = {
        "data": [
            {"id": "s1", "username": "budi", "text": "Lagi butuh vendor cloud nih", "permalink": "p1"},
            {"id": "s2", "username": "ani", "text": "butuh makan", "permalink": "p2"},
            {"id": "s3", "username": "hr", "text": "loker vendor butuh cepat", "permalink": "p3"},
            {"id": "s4", "username": "erwinalif", "text": "butuh vendor? kami siap", "permalink": "p4"},
        ]
    }
    routes[("GET", "111/mentions")] = {
        "data": [{"id": "mn1", "username": "sari", "text": "@erwinalif harga berapa?", "permalink": "p5"}]
    }
    routes[("GET", "111/threads")] = {"data": [{"id": "own1", "has_replies": True}]}
    routes[("GET", "own1/conversation")] = {
        "data": [
            {"id": "r1", "username": "joko", "text": "slot gacor disini", "permalink": "p6"},
            {"id": "r2", "username": "erwinalif", "text": "makasih", "permalink": "p7"},
        ]
    }


def test_score_uses_word_boundaries(rules):
    assert score_text("Butuh VENDOR", rules) == (3.0, ["butuh", "vendor"])
    assert score_text("kebutuhan vendoran", rules)[0] == 0
    assert score_text("loker vendor", rules)[0] == -1


def test_crawl_collects_leads_and_skips_noise(bot, routes):
    _search_routes(routes)
    report = bot.crawl()
    ids = {l["id"] for l in report.new_leads}
    # s2 skor rendah, s3 kena exclude, s4 & r2 post sendiri
    assert ids == {"s1", "mn1", "r1"}
    assert bot.crawl().new_leads == []  # idempotent


def test_respond_dry_run_sends_nothing(bot, routes, session):
    _search_routes(routes)
    bot.crawl()
    session.calls.clear()
    report = bot.respond(dry_run=True)
    kinds = sorted((a.lead_id, a.kind) for a in report.actions)
    assert kinds == [("mn1", "dm"), ("mn1", "reply"), ("r1", "hide"), ("s1", "dm")]
    assert not [c for c in session.calls if c[0] == "POST"]
    assert len(bot.store.unhandled_leads()) == 3


def test_respond_live(bot, routes, session):
    _search_routes(routes)
    routes[("POST", "r1/manage_reply")] = {"success": True}
    bot.crawl()
    report = bot.respond(dry_run=False)
    assert all(a.ok for a in report.actions)

    reply_create = session.calls_to("POST", "111/threads")[0][2]
    assert reply_create["reply_to_id"] == "mn1"
    assert reply_create["text"] == "Halo @sari, cek DM ya"
    assert session.calls_to("POST", "r1/manage_reply")[0][2]["hide"] == "true"

    outbox = bot.store.list_outbox()
    assert {o["username"]: o["message"] for o in outbox} == {
        "sari": "Halo sari, soal p5",
        "budi": "Halo budi, soal butuh vendor",
    }
    assert bot.store.get_lead("mn1")["status"] == "replied"
    assert bot.store.get_lead("s1")["status"] == "queued"
    assert bot.store.get_lead("r1")["status"] == "hidden"
    assert bot.store.unhandled_leads() == []

    # Jalan lagi: tidak ada aksi ganda
    session.calls.clear()
    assert bot.respond(dry_run=False).actions == []


def test_failed_reply_leaves_lead_unhandled(bot, routes):
    from .conftest import FakeResponse

    _search_routes(routes)
    bot.crawl(search=False, replies=False)
    routes[("POST", "111/threads")] = FakeResponse(400, {"error": {"message": "nope"}})
    report = bot.respond(dry_run=False)
    assert any(not a.ok for a in report.actions)
    assert [l["id"] for l in bot.store.unhandled_leads()] == ["mn1"]


def test_bot_post_logs_and_emits_event(bot, session):
    bot.settings.event_webhook_url = "https://hooks.example/x"
    bot.notifier.url = "https://hooks.example/x"
    info = bot.post("Halo dari pipeline", origin="api")
    assert info["permalink"].endswith("/post/x")
    events = [c[2]["event"] for c in session.calls if c[0] == "WEBHOOK"]
    assert events == ["post.published"]


def test_token_refresh_persisted_and_reused(bot, routes, client):
    routes[("GET", "refresh_access_token")] = {"access_token": "NEW", "expires_in": 100}
    bot.maybe_refresh_token(force=True)
    assert bot._resolve_token() == "NEW"
    assert bot.maybe_refresh_token() is None  # belum 7 hari
    bot.settings.access_token = "OTHER"  # user ganti token di .env
    assert bot._resolve_token() == "OTHER"


def test_extract_webhook_posts_both_shapes():
    a = {"values": {"field": "mentions", "value": {"id": "1", "text": "hi"}}}
    b = {"entry": [{"changes": [{"field": "replies", "value": {"id": "2"}}]}]}
    assert extract_webhook_posts(a) == [("mentions", {"id": "1", "text": "hi"})]
    assert extract_webhook_posts(b) == [("replies", {"id": "2"})]
