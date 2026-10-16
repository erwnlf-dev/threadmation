"""Kirim event ke pipeline lain (n8n/Make/Zapier/CRM/service internal) via HTTP POST."""

from __future__ import annotations

import logging

import requests

from .store import now_iso

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, url: str = "", session: requests.Session | None = None, timeout: float = 10):
        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout

    def emit(self, event: str, data: dict) -> None:
        """Kirim {"event", "at", "data"}. Error hanya di-log supaya bot tetap jalan."""
        if not self.url:
            return
        try:
            resp = self.session.post(
                self.url, json={"event": event, "at": now_iso(), "data": data}, timeout=self.timeout
            )
            if resp.status_code >= 400:
                log.warning("Event webhook %s -> HTTP %s", event, resp.status_code)
        except requests.RequestException as exc:
            log.warning("Event webhook %s gagal: %s", event, exc)
