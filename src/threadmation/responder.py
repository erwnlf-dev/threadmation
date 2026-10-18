"""Auto-responder: balas publik, hide reply, dan antre DM berdasarkan rules."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from .client import ThreadsAPIError, ThreadsClient
from .config import ResponseRule, Rules
from .leads import _contains
from .notify import Notifier
from .store import Store, profile_url

log = logging.getLogger(__name__)


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, lead: dict) -> str:
    author = lead.get("author") or {}
    values = {**lead, "name": author.get("name") or lead.get("username"), "bio": author.get("bio")}
    values["first_name"] = (values["name"] or "").split(" ")[0]
    return template.format_map(_SafeDict({k: (v if v is not None else "") for k, v in values.items()})).strip()


def match_rule(lead: dict, rules: list[ResponseRule]) -> ResponseRule | None:
    text = (lead.get("text") or "").lower()
    for rule in rules:
        if lead.get("source") not in rule.sources and "*" not in rule.sources:
            continue
        if (lead.get("platform") or "threads") not in rule.platforms and "*" not in rule.platforms:
            continue
        if not rule.match_any or any(_contains(text, kw.lower()) for kw in rule.match_any):
            return rule
    return None


@dataclass
class Action:
    lead_id: str
    username: str | None
    rule: str
    kind: str  # reply | dm | hide
    detail: str
    ok: bool = True
    error: str | None = None


@dataclass
class RespondReport:
    actions: list[Action] = field(default_factory=list)
    dry_run: bool = True
    stopped_reason: str | None = None


class Responder:
    def __init__(
        self,
        client: ThreadsClient,
        store: Store,
        rules: Rules,
        notifier: Notifier | None = None,
        sleep=time.sleep,
    ):
        self.client = client
        self.store = store
        self.rules = rules
        self.notifier = notifier or Notifier()
        self._sleep = sleep

    def run(self, dry_run: bool = True, limit: int = 100) -> RespondReport:
        report = RespondReport(dry_run=dry_run)
        replies_left = self.rules.max_replies_per_run
        if not dry_run and replies_left:
            replies_left = min(replies_left, self._reply_quota_left())

        for lead in self.store.unhandled_leads(limit=limit):
            rule = match_rule(lead, self.rules.responses)
            if rule is None:
                if not dry_run:
                    self.store.mark_handled(lead["id"])
                continue

            if rule.reply and replies_left <= 0 and not dry_run:
                report.stopped_reason = "batas balasan per run / kuota balasan harian tercapai"
                break

            status = None
            # Balas/hide publik hanya untuk Threads; lead platform lain cukup masuk outbox DM.
            on_threads = (lead.get("platform") or "threads") == "threads"
            if rule.hide and on_threads and lead.get("source") == "reply":
                if self._do(report, lead, rule, "hide", "hide reply", dry_run, self._hide):
                    status = "hidden"
            if rule.reply and on_threads and not self.store.has_replied_to(lead["id"]):
                text = render(rule.reply, lead)
                if self._do(report, lead, rule, "reply", text, dry_run, self._reply):
                    status = "replied"
                    replies_left -= 1
                    if not dry_run and replies_left > 0:
                        self._sleep(self.rules.reply_delay_seconds * random.uniform(0.8, 1.5))
            if rule.dm and lead.get("username"):
                text = render(rule.dm, lead)
                if self._do(report, lead, rule, "dm", text, dry_run, self._queue_dm):
                    status = status or "queued"

            if not dry_run:
                failed = any(a.lead_id == lead["id"] and not a.ok for a in report.actions)
                if not failed:
                    self.store.mark_handled(lead["id"], status)
        return report

    # ---------------------------------------------------------------------
    def _do(self, report, lead, rule, kind, detail, dry_run, fn) -> bool:
        action = Action(lead["id"], lead.get("username"), rule.name, kind, detail)
        report.actions.append(action)
        if dry_run:
            return True
        try:
            fn(lead, detail)
            return True
        except ThreadsAPIError as exc:
            log.error("%s ke %s gagal: %s", kind, lead["id"], exc)
            action.ok, action.error = False, str(exc)
            return False

    def _reply(self, lead: dict, text: str) -> None:
        media_id = self.client.reply(lead["id"], text)
        self.store.log_post(media_id, "reply", text, reply_to=lead["id"], origin="responder")
        self.notifier.emit("reply.published", {"id": media_id, "reply_to": lead["id"], "text": text})

    def _hide(self, lead: dict, _detail: str) -> None:
        self.client.hide_reply(lead["id"], True)

    def _queue_dm(self, lead: dict, text: str) -> None:
        platform = lead.get("platform") or "threads"
        item_id = self.store.queue_dm(lead["username"], text, lead_id=lead["id"], platform=platform)
        if item_id:
            self.notifier.emit(
                "dm.queued",
                {
                    "id": item_id,
                    "lead_id": lead["id"],
                    "platform": platform,
                    "username": lead["username"],
                    "profile_url": profile_url(platform, lead["username"]),
                    "message": text,
                    "lead_text": lead.get("text"),
                    "permalink": lead.get("permalink"),
                },
            )

    def _reply_quota_left(self) -> int:
        try:
            info = self.client.publishing_limit()
            total = (info.get("reply_config") or {}).get("quota_total", 1000)
            return max(0, int(total) - int(info.get("reply_quota_usage", 0)))
        except (ThreadsAPIError, TypeError, ValueError) as exc:
            log.warning("Tidak bisa cek kuota balasan: %s", exc)
            return self.rules.max_replies_per_run
