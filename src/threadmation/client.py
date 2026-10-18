"""Klien tipis untuk Threads Graph API (graph.threads.net).

Endpoint yang dipakai (semua resmi dari Meta):
  - POST /{user}/threads, /{user}/threads_publish      -> buat & publish post/reply
  - GET  /{container}?fields=status                    -> status container media
  - GET  /{user}/threads, /{media}                     -> ambil post
  - GET  /{media}/replies, /{media}/conversation       -> balasan
  - GET  /{user}/mentions                              -> mention ke akun Anda
  - GET  /keyword_search                               -> cari post publik (crawl leads)
  - POST /{reply}/manage_reply                         -> hide/unhide balasan
  - DELETE /{media}                                    -> hapus post
  - GET  /{user}/threads_publishing_limit              -> sisa kuota
  - GET  /profile_lookup                               -> profil publik by username
  - GET  /refresh_access_token                         -> perpanjang long-lived token

Catatan: Threads API TIDAK punya endpoint direct message (DM).
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import requests

DEFAULT_BASE_URL = "https://graph.threads.net/v1.0"
MAX_TEXT_LENGTH = 500

POST_FIELDS = (
    "id,text,username,permalink,timestamp,media_type,media_url,"
    "shortcode,is_quote_post,has_replies"
)
REPLY_FIELDS = POST_FIELDS + ",root_post,replied_to,is_reply,hide_status"
ME_FIELDS = "id,username,name,threads_biography,threads_profile_picture_url"


class ThreadsAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload

    @property
    def code(self) -> int | None:
        if isinstance(self.payload, dict):
            return (self.payload.get("error") or {}).get("code")
        return None


class ThreadsClient:
    def __init__(
        self,
        access_token: str,
        user_id: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        poll_interval: float = 2.0,
        publish_timeout: float = 300.0,
        sleep=time.sleep,
    ):
        # Token boleh kosong (mode X saja); setiap panggilan Threads lalu gagal dengan pesan jelas.
        self.access_token = access_token
        self._user_id = user_id or None
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.poll_interval = poll_interval
        self.publish_timeout = publish_timeout
        self._sleep = sleep
        self._me: dict | None = None

    # ------------------------------------------------------------------ core
    def request(self, method: str, path: str, params: dict | None = None) -> dict:
        if not self.access_token:
            raise ThreadsAPIError("THREADS_ACCESS_TOKEN belum diisi (lihat .env.example)")
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params.setdefault("access_token", self.access_token)

        # Request non-GET (publish/reply) hanya di-retry untuk 429, supaya tidak double-post
        # bila server sebenarnya sudah memproses request sebelum error 5xx/timeout.
        idempotent = method == "GET"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                self._sleep(2**attempt)
            try:
                if idempotent:
                    resp = self.session.request(method, url, params=params, timeout=self.timeout)
                else:
                    resp = self.session.request(method, url, data=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                if idempotent:
                    continue
                raise ThreadsAPIError(f"Gagal menghubungi Threads API: {exc}") from exc

            data = _safe_json(resp)
            if resp.status_code == 429 or (idempotent and resp.status_code >= 500):
                last_exc = ThreadsAPIError(f"HTTP {resp.status_code}", resp.status_code, data)
                continue

            if resp.status_code >= 400 or (isinstance(data, dict) and "error" in data):
                err = (data or {}).get("error", {}) if isinstance(data, dict) else {}
                msg = err.get("error_user_msg") or err.get("message") or resp.text
                raise ThreadsAPIError(f"Threads API error: {msg}", resp.status_code, data)
            return data if isinstance(data, dict) else {"data": data}

        if isinstance(last_exc, ThreadsAPIError):
            raise last_exc
        raise ThreadsAPIError(f"Gagal menghubungi Threads API: {last_exc}") from last_exc

    def paginate(self, path: str, params: dict | None = None, max_items: int = 100) -> Iterator[dict]:
        params = dict(params or {})
        count = 0
        while True:
            page = self.request("GET", path, params)
            for item in page.get("data", []):
                yield item
                count += 1
                if count >= max_items:
                    return
            after = ((page.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not page.get("data") or not (page.get("paging") or {}).get("next"):
                return
            params["after"] = after

    # --------------------------------------------------------------- profile
    def me(self, refresh: bool = False) -> dict:
        if self._me is None or refresh:
            self._me = self.request("GET", "me", {"fields": ME_FIELDS})
        return self._me

    @property
    def user_id(self) -> str:
        if not self._user_id:
            self._user_id = self.me()["id"]
        return self._user_id

    @property
    def username(self) -> str:
        return self.me()["username"]

    def profile_lookup(self, username: str) -> dict:
        """Profil publik akun lain (permission: threads_profile_discovery)."""
        return self.request(
            "GET",
            "profile_lookup",
            {
                "username": username.lstrip("@"),
                "fields": "username,name,biography,is_verified,profile_picture_url,"
                "follower_count,likes_count,quotes_count,replies_count,reposts_count,views_count",
            },
        )

    # ------------------------------------------------------------ publishing
    def create_container(
        self,
        media_type: str = "TEXT",
        text: str | None = None,
        image_url: str | None = None,
        video_url: str | None = None,
        reply_to_id: str | None = None,
        reply_control: str | None = None,
        quote_post_id: str | None = None,
        topic_tag: str | None = None,
        link_attachment: str | None = None,
        alt_text: str | None = None,
        is_carousel_item: bool = False,
        children: list[str] | None = None,
    ) -> str:
        if text and len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"Teks maksimal {MAX_TEXT_LENGTH} karakter (sekarang {len(text)})")
        params = {
            "media_type": media_type,
            "text": text,
            "image_url": image_url,
            "video_url": video_url,
            "reply_to_id": reply_to_id,
            "reply_control": reply_control,
            "quote_post_id": quote_post_id,
            "topic_tag": topic_tag,
            "link_attachment": link_attachment,
            "alt_text": alt_text,
            "is_carousel_item": "true" if is_carousel_item else None,
            "children": ",".join(children) if children else None,
        }
        return self.request("POST", f"{self.user_id}/threads", params)["id"]

    def container_status(self, container_id: str) -> dict:
        return self.request("GET", container_id, {"fields": "id,status,error_message"})

    def wait_until_ready(self, container_id: str) -> None:
        deadline = time.monotonic() + self.publish_timeout
        while True:
            info = self.container_status(container_id)
            status = info.get("status")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise ThreadsAPIError(
                    f"Container {container_id} {status}: {info.get('error_message')}", payload=info
                )
            if time.monotonic() > deadline:
                raise ThreadsAPIError(f"Timeout menunggu container {container_id} (status={status})")
            self._sleep(self.poll_interval)

    def publish_container(self, container_id: str) -> str:
        self.wait_until_ready(container_id)
        return self.request("POST", f"{self.user_id}/threads_publish", {"creation_id": container_id})["id"]

    def post(
        self,
        text: str | None = None,
        image_url: str | None = None,
        video_url: str | None = None,
        carousel: list[dict] | None = None,
        **opts,
    ) -> str:
        """Buat & publish post. Return media id post yang sudah terbit.

        carousel: list of {"image_url": ...} atau {"video_url": ...} (2-20 item).
        opts: reply_to_id, reply_control, quote_post_id, topic_tag, link_attachment, alt_text.
        """
        if carousel:
            if not 2 <= len(carousel) <= 20:
                raise ValueError("Carousel harus berisi 2-20 item")
            children = []
            for item in carousel:
                child = self.create_container(
                    media_type="VIDEO" if item.get("video_url") else "IMAGE",
                    image_url=item.get("image_url"),
                    video_url=item.get("video_url"),
                    alt_text=item.get("alt_text"),
                    is_carousel_item=True,
                )
                self.wait_until_ready(child)
                children.append(child)
            container = self.create_container("CAROUSEL", text=text, children=children, **opts)
        elif video_url:
            container = self.create_container("VIDEO", text=text, video_url=video_url, **opts)
        elif image_url:
            container = self.create_container("IMAGE", text=text, image_url=image_url, **opts)
        else:
            if not text:
                raise ValueError("Post teks tidak boleh kosong")
            container = self.create_container("TEXT", text=text, **opts)
        return self.publish_container(container)

    def reply(self, reply_to_id: str, text: str, image_url: str | None = None, **opts) -> str:
        return self.post(text=text, image_url=image_url, reply_to_id=reply_to_id, **opts)

    def delete(self, media_id: str) -> dict:
        """Hapus post (permission: threads_delete)."""
        return self.request("DELETE", media_id)

    # ------------------------------------------------------------- retrieval
    def get_media(self, media_id: str, fields: str = POST_FIELDS) -> dict:
        return self.request("GET", media_id, {"fields": fields})

    def user_threads(self, limit: int = 25, since: str | None = None, until: str | None = None) -> list[dict]:
        params = {"fields": POST_FIELDS, "limit": min(limit, 100), "since": since, "until": until}
        return list(self.paginate(f"{self.user_id}/threads", params, max_items=limit))

    def replies(self, media_id: str, limit: int = 100) -> list[dict]:
        """Balasan level-1 untuk sebuah post."""
        params = {"fields": REPLY_FIELDS, "reverse": "false"}
        return list(self.paginate(f"{media_id}/replies", params, max_items=limit))

    def conversation(self, media_id: str, limit: int = 200) -> list[dict]:
        """Semua balasan (semua level) di bawah sebuah post."""
        params = {"fields": REPLY_FIELDS, "reverse": "false"}
        return list(self.paginate(f"{media_id}/conversation", params, max_items=limit))

    def mentions(self, limit: int = 50) -> list[dict]:
        """Post yang me-mention akun Anda (permission: threads_manage_mentions)."""
        params = {"fields": POST_FIELDS, "limit": min(limit, 100)}
        return list(self.paginate(f"{self.user_id}/mentions", params, max_items=limit))

    def keyword_search(
        self,
        q: str,
        search_type: str = "RECENT",
        search_mode: str = "KEYWORD",
        media_type: str | None = None,
        since: int | str | None = None,
        until: int | str | None = None,
        author_username: str | None = None,
        limit: int = 25,
    ) -> list[dict]:
        """Cari post publik (permission: threads_keyword_search).

        search_type: TOP | RECENT, search_mode: KEYWORD | TAG,
        media_type: TEXT | IMAGE | VIDEO.
        """
        params = {
            "q": q,
            "search_type": search_type,
            "search_mode": search_mode,
            "media_type": media_type,
            "since": since,
            "until": until,
            "author_username": author_username,
            "limit": min(limit, 100),
            "fields": POST_FIELDS,
        }
        return list(self.paginate("keyword_search", params, max_items=limit))

    # ------------------------------------------------------------ moderation
    def hide_reply(self, reply_id: str, hide: bool = True) -> dict:
        return self.request("POST", f"{reply_id}/manage_reply", {"hide": "true" if hide else "false"})

    # ------------------------------------------------------------- utilities
    def publishing_limit(self) -> dict:
        data = self.request(
            "GET",
            f"{self.user_id}/threads_publishing_limit",
            {"fields": "quota_usage,config,reply_quota_usage,reply_config"},
        )
        rows = data.get("data") or [{}]
        return rows[0]

    def refresh_token(self) -> dict:
        """Perpanjang long-lived token (berlaku 60 hari lagi).

        Token harus berumur >= 24 jam dan belum kedaluwarsa.
        Return {"access_token", "token_type", "expires_in"}.
        """
        data = self.request(
            "GET",
            "https://graph.threads.net/refresh_access_token",
            {"grant_type": "th_refresh_token", "access_token": self.access_token},
        )
        if data.get("access_token"):
            self.access_token = data["access_token"]
        return data


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None
