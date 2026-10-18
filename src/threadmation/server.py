"""HTTP API untuk integrasi pipeline lain + penerima webhook Threads.

Jalankan:  threadmation serve --port 8080
Semua endpoint /v1/* butuh header  X-API-Key: <THREADMATION_API_KEY>.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .bot import Bot
from .client import MAX_TEXT_LENGTH, ThreadsAPIError
from .social import SocialBridgeError

log = logging.getLogger(__name__)


class CarouselItem(BaseModel):
    image_url: str | None = None
    video_url: str | None = None
    alt_text: str | None = None


class PostIn(BaseModel):
    text: str | None = Field(None, max_length=MAX_TEXT_LENGTH)
    image_url: str | None = None
    video_url: str | None = None
    carousel: list[CarouselItem] | None = None
    reply_control: Literal["everyone", "accounts_you_follow", "mentioned_only", "parent_post_author_only", "followers_only"] | None = None
    topic_tag: str | None = None
    link_attachment: str | None = None
    quote_post_id: str | None = None
    alt_text: str | None = None
    crosspost: list[str] | None = None


class ReplyIn(BaseModel):
    reply_to_id: str
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_LENGTH)
    image_url: str | None = None


class SocialMediaIn(BaseModel):
    kind: Literal["image", "video"] = "image"
    url: str
    alt_text: str | None = None


class SocialPostIn(BaseModel):
    platforms: list[str] | None = None
    text: str | None = None
    media: list[SocialMediaIn] = []
    link: str | None = None
    idempotency_key: str | None = None


class StatusIn(BaseModel):
    status: str


class CrawlIn(BaseModel):
    search: bool = True
    mentions: bool | None = None
    replies: bool | None = None
    respond: bool = False
    dry_run: bool = True


def create_app(bot: Bot | None = None) -> FastAPI:
    app = FastAPI(title="Threadmation", version="0.1.0")
    state: dict = {"bot": bot}

    def get_bot() -> Bot:
        if state["bot"] is None:
            state["bot"] = Bot()
        return state["bot"]

    def require_key(x_api_key: str | None = Header(None), b: Bot = Depends(get_bot)) -> Bot:
        expected = b.settings.api_key
        if not expected:
            raise HTTPException(503, "THREADMATION_API_KEY belum di-set di server")
        if not x_api_key or not hmac.compare_digest(x_api_key, expected):
            raise HTTPException(401, "API key salah")
        return b

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ThreadsAPIError as exc:
            raise HTTPException(502, {"error": str(exc), "threads": exc.payload}) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SocialBridgeError as exc:
            raise HTTPException(503, f"social-sdk: {exc}") from exc

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ------------------------------------------------------------ pipeline
    @app.post("/v1/posts")
    def create_post(body: PostIn, b: Bot = Depends(require_key)):
        return call(b.post, origin="api", **body.model_dump(exclude_none=True))

    @app.post("/v1/replies")
    def create_reply(body: ReplyIn, b: Bot = Depends(require_key)):
        return call(b.reply, body.reply_to_id, body.text, origin="api", image_url=body.image_url)

    @app.get("/v1/social/platforms")
    def social_platforms(b: Bot = Depends(require_key)):
        return {"configured": call(b.social.platforms), "default_crosspost": b.settings.crosspost}

    @app.post("/v1/social/posts")
    def social_post(body: SocialPostIn, b: Bot = Depends(require_key)):
        targets = body.platforms or b.settings.crosspost
        if not targets:
            raise HTTPException(422, "platforms kosong (atau isi THREADMATION_CROSSPOST)")
        media = [m.model_dump(exclude_none=True) for m in body.media]
        result = call(b.social.publish, targets, body.text, media=media, link=body.link,
                      idempotency_key=body.idempotency_key)
        b.notifier.emit("social.published", {"text": body.text, **result})
        return result

    @app.delete("/v1/posts/{media_id}")
    def delete_post(media_id: str, b: Bot = Depends(require_key)):
        return call(b.client.delete, media_id)

    @app.post("/v1/replies/{reply_id}/hide")
    def hide_reply(reply_id: str, hide: bool = True, b: Bot = Depends(require_key)):
        return call(b.client.hide_reply, reply_id, hide)

    @app.get("/v1/me")
    def me(b: Bot = Depends(require_key)):
        return call(b.client.me)

    @app.get("/v1/quota")
    def quota(b: Bot = Depends(require_key)):
        return call(b.client.publishing_limit)

    @app.get("/v1/search")
    def search(
        q: str,
        search_type: Literal["TOP", "RECENT"] = "RECENT",
        search_mode: Literal["KEYWORD", "TAG"] = "KEYWORD",
        limit: int = Query(25, ge=1, le=100),
        b: Bot = Depends(require_key),
    ):
        return {"data": call(b.client.keyword_search, q, search_type=search_type, search_mode=search_mode, limit=limit)}

    @app.post("/v1/crawl")
    def crawl(body: CrawlIn, b: Bot = Depends(require_key)):
        report = b.crawl(search=body.search, mentions=body.mentions, replies=body.replies)
        out = {"scanned": report.scanned, "new_leads": report.new_leads, "errors": report.errors}
        if body.respond:
            r = b.respond(dry_run=body.dry_run)
            out["respond"] = {"dry_run": r.dry_run, "actions": [a.__dict__ for a in r.actions], "stopped_reason": r.stopped_reason}
        return out

    @app.get("/v1/leads")
    def leads(status: str | None = None, source: str | None = None, limit: int = Query(100, le=1000), b: Bot = Depends(require_key)):
        return {"data": b.store.list_leads(status=status, source=source, limit=limit)}

    @app.patch("/v1/leads/{lead_id}")
    def update_lead(lead_id: str, body: StatusIn, b: Bot = Depends(require_key)):
        if not b.store.set_lead_status(lead_id, body.status):
            raise HTTPException(404, "lead tidak ditemukan")
        return b.store.get_lead(lead_id)

    @app.get("/v1/outbox")
    def outbox(status: str | None = "pending", b: Bot = Depends(require_key)):
        return {"data": b.store.list_outbox(status=status or None)}

    @app.patch("/v1/outbox/{item_id}")
    def update_outbox(item_id: int, body: StatusIn, b: Bot = Depends(require_key)):
        if body.status not in ("pending", "sent", "skipped"):
            raise HTTPException(422, "status harus pending | sent | skipped")
        if not b.store.set_outbox_status(item_id, body.status):
            raise HTTPException(404, "item outbox tidak ditemukan")
        return {"ok": True}

    # ------------------------------------------------------------- webhook
    @app.get("/webhooks/threads", response_class=PlainTextResponse)
    def webhook_verify(
        mode: str = Query(None, alias="hub.mode"),
        token: str = Query(None, alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
        b: Bot = Depends(get_bot),
    ):
        expected = b.settings.webhook_verify_token
        if mode == "subscribe" and expected and token and hmac.compare_digest(token, expected):
            return challenge
        raise HTTPException(403, "verify token salah")

    @app.post("/webhooks/threads")
    async def webhook_receive(request: Request, tasks: BackgroundTasks, b: Bot = Depends(get_bot)):
        body = await request.body()
        if not b.verify_signature(body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "signature tidak valid")
        try:
            payload = json.loads(body or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "payload bukan JSON") from exc
        report = b.ingest_webhook(payload)
        if report.new_leads and b.rules.responses and b.settings.webhook_auto_reply:
            # Balas di background supaya Meta langsung dapat 200 OK.
            tasks.add_task(b.respond, dry_run=False)
        return {"received": True, "new_leads": len(report.new_leads)}

    return app
