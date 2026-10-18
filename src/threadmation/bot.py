"""Wiring: satu objek Bot yang dipakai CLI, server HTTP, dan pipeline Python Anda."""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
from datetime import datetime, timedelta, timezone

from .client import ThreadsAPIError, ThreadsClient
from .config import Rules, Settings
from .leads import CrawlReport, LeadCrawler
from .notify import Notifier
from .responder import RespondReport, Responder
from .social import SocialBridge, SocialBridgeError, media_from_post_kwargs
from .store import Store, now_iso

log = logging.getLogger(__name__)

REFRESH_EVERY = timedelta(days=7)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Bot:
    def __init__(self, settings: Settings | None = None, client: ThreadsClient | None = None, store: Store | None = None):
        self.settings = settings or Settings.from_env()
        self.store = store or Store(self.settings.db_path)
        self.rules = Rules.load(self.settings.rules_path)
        self.notifier = Notifier(self.settings.event_webhook_url)
        self.client = client or ThreadsClient(
            self._resolve_token(), user_id=self.settings.user_id or None, base_url=self.settings.api_base
        )
        self.social = SocialBridge(self.settings.social_bridge_dir or None)
        self.crawler = LeadCrawler(self.client, self.store, self.rules, self.notifier, social=self.social)
        self.responder = Responder(self.client, self.store, self.rules, self.notifier)
        self._respond_lock = threading.Lock()

    # ------------------------------------------------------------ actions
    def post(self, text: str | None = None, origin: str = "cli", crosspost: list[str] | None = None, **kwargs) -> dict:
        media_id = self.client.post(text=text, **kwargs)
        kind = "reply" if kwargs.get("reply_to_id") else "post"
        self.store.log_post(media_id, kind, text, reply_to=kwargs.get("reply_to_id"), origin=origin)
        info = {"id": media_id, "kind": kind, "text": text}
        try:
            info["permalink"] = self.client.get_media(media_id, "id,permalink").get("permalink")
        except ThreadsAPIError:
            pass
        self.notifier.emit("post.published", info)
        targets = self.settings.crosspost if crosspost is None else crosspost
        if targets and kind == "post":
            info["crosspost"] = self.crosspost(
                targets, text, media=media_from_post_kwargs(kwargs),
                link=kwargs.get("link_attachment"), idempotency_key=f"threads-{media_id}",
            )
        return info

    def crosspost(self, platforms: list[str], text: str | None, media: list[dict] | None = None,
                  link: str | None = None, idempotency_key: str | None = None) -> dict:
        """Publish ke platform lain via social-sdk. Gagal tidak melempar error (post Threads sudah terkirim)."""
        try:
            result = self.social.publish(platforms, text, media=media, link=link, idempotency_key=idempotency_key)
        except SocialBridgeError as exc:
            log.warning("Cross-post gagal: %s", exc)
            return {"status": "failed", "error": str(exc), "results": []}
        for r in result["results"]:
            if r.get("state") not in ("published", "accepted", "processing"):
                log.warning("Cross-post %s: %s", r.get("platform"), r)
        self.notifier.emit("social.published", {"text": text, **result})
        return result

    def reply(self, reply_to_id: str, text: str, origin: str = "cli", **kwargs) -> dict:
        return self.post(text=text, reply_to_id=reply_to_id, origin=origin, **kwargs)

    def crawl(self, **kwargs) -> CrawlReport:
        return self.crawler.run(**kwargs)

    def respond(self, dry_run: bool = True) -> RespondReport:
        # Satu responder dalam satu waktu (cron + webhook) supaya tidak double-reply.
        with self._respond_lock:
            return self.responder.run(dry_run=dry_run)

    def cycle(self, dry_run: bool = True) -> tuple[CrawlReport, RespondReport]:
        """Satu putaran penuh: refresh token bila perlu -> crawl -> respond."""
        if not dry_run:
            self.maybe_refresh_token()
        return self.crawl(), self.respond(dry_run=dry_run)

    # ------------------------------------------------------------ webhook
    def verify_signature(self, body: bytes, signature_header: str | None) -> bool:
        if not self.settings.app_secret:
            log.warning("THREADS_APP_SECRET kosong: signature webhook TIDAK diverifikasi")
            return True
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(self.settings.app_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))

    def ingest_webhook(self, payload: dict) -> CrawlReport:
        report = CrawlReport()
        for field_name, post in extract_webhook_posts(payload):
            source = "mention" if "mention" in field_name else "reply"
            self.crawler.ingest(post, source, report)
        return report

    # -------------------------------------------------------------- token
    def _resolve_token(self) -> str:
        """Pakai token hasil refresh (di DB) selama token di .env belum diganti."""
        env_token = self.settings.access_token
        if not env_token:
            raise ValueError("THREADS_ACCESS_TOKEN belum diisi (lihat .env.example)")
        stored = self.store.get("access_token")
        if stored and self.store.get("access_token_origin") == _sha(env_token):
            return stored
        return env_token

    def maybe_refresh_token(self, force: bool = False) -> dict | None:
        last = self.store.get("access_token_refreshed_at")
        if not force and last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - REFRESH_EVERY:
            return None
        try:
            data = self.client.refresh_token()
        except ThreadsAPIError as exc:
            log.warning("Refresh token gagal (token harus berumur >= 24 jam): %s", exc)
            if force:
                raise
            return None
        self.store.set("access_token", data["access_token"])
        self.store.set("access_token_origin", _sha(self.settings.access_token))
        self.store.set("access_token_refreshed_at", now_iso())
        if data.get("expires_in"):
            expires = datetime.now(timezone.utc) + timedelta(seconds=int(data["expires_in"]))
            self.store.set("access_token_expires_at", expires.isoformat(timespec="seconds"))
        return {k: v for k, v in data.items() if k != "access_token"}


def extract_webhook_posts(payload: dict) -> list[tuple[str, dict]]:
    """Ambil post dari payload webhook Threads (mendukung 2 format yang dipakai Meta)."""
    out: list[tuple[str, dict]] = []
    values = payload.get("values")
    if isinstance(values, dict):
        values = [values]
    for item in values or []:
        if isinstance(item, dict) and isinstance(item.get("value"), dict):
            out.append((item.get("field", ""), item["value"]))
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if isinstance(change.get("value"), dict):
                out.append((change.get("field", ""), change["value"]))
    return out
