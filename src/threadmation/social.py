"""Cross-post (X, Bluesky, LinkedIn) dan search X lewat @opencoredev/social-sdk.

SDK-nya TypeScript, jadi dipanggil lewat proses Node kecil di folder ``social-bridge/``
(``npm install`` sekali di sana). Kredensial tiap platform dibaca bridge dari environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

DEFAULT_BRIDGE_DIR = Path(__file__).resolve().parents[2] / "social-bridge"


class SocialBridgeError(RuntimeError):
    pass


class SocialBridge:
    def __init__(self, bridge_dir: str | os.PathLike | None = None, node: str = "node", timeout: float = 180, runner=None):
        self.bridge_dir = Path(bridge_dir) if bridge_dir else DEFAULT_BRIDGE_DIR
        self.node = node
        self.timeout = timeout
        self._run = runner or subprocess.run

    def _call(self, action: str, payload: dict | None = None) -> dict:
        script = self.bridge_dir / "bridge.mjs"
        if not script.is_file():
            raise SocialBridgeError(f"bridge tidak ditemukan: {script}")
        if not (self.bridge_dir / "node_modules").is_dir():
            raise SocialBridgeError(f"jalankan dulu: cd {self.bridge_dir} && npm install")
        if shutil.which(self.node) is None:
            raise SocialBridgeError("Node.js >= 22.12 belum terpasang")
        try:
            proc = self._run(
                [self.node, str(script), action],
                input=json.dumps(payload or {}),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.bridge_dir,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            raise SocialBridgeError(f"bridge timeout setelah {self.timeout}s") from exc
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise SocialBridgeError(f"output bridge tidak valid: {proc.stderr.strip() or proc.stdout!r}") from exc
        if proc.returncode != 0 or "error" in out:
            raise SocialBridgeError(out.get("error") or proc.stderr.strip() or f"exit {proc.returncode}")
        return out

    def platforms(self) -> list[str]:
        """Platform yang kredensialnya sudah diisi di environment."""
        return self._call("platforms")["platforms"]

    def publish(
        self,
        platforms: list[str],
        text: str | None = None,
        media: list[dict] | None = None,
        link: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Kembalikan {"status": complete|partial|failed, "results": [{platform, state, url, ...}]}."""
        payload = {"platforms": list(platforms), "text": text, "media": media or [], "link": link}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self._call("publish", payload)

    def search(self, platform: str, query: str, limit: int = 25, sort: str = "recency") -> list[dict]:
        """Post hasil search (format sama dengan post Threads) + field ``platform`` dan ``author``."""
        return self._call("search", {"platform": platform, "query": query, "limit": limit, "sort": sort})["items"]


def media_from_post_kwargs(kwargs: dict) -> list[dict]:
    """Ubah argumen Bot.post (image_url/video_url/carousel) ke format media bridge."""
    alt = kwargs.get("alt_text")
    media: list[dict] = []
    if kwargs.get("image_url"):
        media.append({"kind": "image", "url": kwargs["image_url"], "alt_text": alt})
    if kwargs.get("video_url"):
        media.append({"kind": "video", "url": kwargs["video_url"], "alt_text": alt})
    for item in kwargs.get("carousel") or []:
        if item.get("image_url"):
            media.append({"kind": "image", "url": item["image_url"], "alt_text": item.get("alt_text")})
        elif item.get("video_url"):
            media.append({"kind": "video", "url": item["video_url"], "alt_text": item.get("alt_text")})
    return [{k: v for k, v in m.items() if v} for m in media]
